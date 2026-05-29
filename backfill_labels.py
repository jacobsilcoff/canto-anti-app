"""One-time script to add AI-suggested labels to cards that have none.

Usage (from project root):
    source venv/bin/activate
    python backfill_labels.py

Processes all users. Skips cards that already have at least one label.
Rate-limits to ~1 req/sec to stay within Gemini free tier.
"""

import asyncio
import os
import time

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

import db
import translation

DELAY = 5.0  # seconds between Gemini calls (~12 req/min, under the 15 RPM free-tier limit)


async def suggest_labels_for_card(source_text: str, target_text: str, target_lang: str) -> list[str]:
    info = translation.LANG_INFO.get(target_lang, {})
    lang_name = info.get("name", target_lang)
    prompt = (
        f"Given this {lang_name} vocabulary card:\n"
        f"  {lang_name}: {target_text}\n"
        f"  English: {source_text}\n\n"
        "Return 2–5 short, broad English topic labels (lowercase, e.g. \"food\", \"cooking\", "
        "\"animal\", \"emotion\", \"travel\") that best categorise this word.\n"
        "Return ONLY valid JSON, no other text:\n"
        '{"labels": ["...", "..."]}'
    )
    try:
        raw = await asyncio.to_thread(lambda: translation._parse_json(translation._call(prompt)))
        labels = raw.get("labels") or []
        return [l.strip().lower() for l in labels if isinstance(l, str) and l.strip()]
    except Exception as e:
        print(f"  Gemini error: {e}")
        return []


async def main():
    await db.init()

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # Find all cards that have no labels, across all users.
        async with conn.execute("""
            SELECT c.id, c.user_id, c.source_text, c.target_text, c.target_lang
            FROM cards c
            WHERE NOT EXISTS (
                SELECT 1 FROM card_labels cl WHERE cl.card_id = c.id
            )
            AND c.source_text != '' AND c.target_text != ''
            ORDER BY c.user_id, c.id
        """) as cur:
            cards = [dict(r) for r in await cur.fetchall()]

    print(f"Found {len(cards)} unlabelled cards across all users.")
    if not cards:
        print("Nothing to do.")
        return

    processed = 0
    for card in cards:
        cid = card["id"]
        uid = card["user_id"]
        src = card["source_text"]
        tgt = card["target_text"]
        lang = card["target_lang"]

        print(f"[{processed+1}/{len(cards)}] Card {cid} (user {uid}): {tgt} — {src}")

        labels = await suggest_labels_for_card(src, tgt, lang)
        if not labels:
            print("  No labels returned, skipping.")
            processed += 1
            continue

        print(f"  Labels: {labels}")

        async with aiosqlite.connect(db.DB_PATH) as conn:
            for name in labels:
                await conn.execute(
                    "INSERT OR IGNORE INTO labels (user_id, name, is_story_label) VALUES (?, ?, 0)",
                    (uid, name),
                )
                async with conn.execute(
                    "SELECT id FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
                    (uid, name),
                ) as cur:
                    row = await cur.fetchone()
                if row:
                    await conn.execute(
                        "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
                        (cid, row[0]),
                    )
            await conn.commit()

        processed += 1
        time.sleep(DELAY)

    print(f"\nDone. Labelled {processed} cards.")


if __name__ == "__main__":
    asyncio.run(main())
