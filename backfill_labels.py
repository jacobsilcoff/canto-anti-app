"""Backfill script: add AI-suggested labels and CEFR levels to existing cards.

- Labels: skips cards that already have at least one label.
- CEFR:   skips cards that already have a cefr_level set.
- Both are fetched in a single Gemini call per card to minimise API usage.
- Cards that need neither are skipped entirely.

Usage (from project root):
    source venv/bin/activate
    python backfill_labels.py

Processes all users. Rate-limits to ~1 req/5 sec to stay within the Gemini
free-tier limit (~15 RPM).
"""

import asyncio
import time

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

import db
import translation

DELAY = 5.0  # seconds between Gemini calls
VALID_CEFR = {"A1", "A2", "B1", "B2", "C1", "C2"}


async def fetch_labels_and_cefr(
    source_text: str, target_text: str, target_lang: str, need_labels: bool, need_cefr: bool
) -> tuple[list[str], str | None]:
    info = translation.LANG_INFO.get(target_lang, {})
    lang_name = info.get("name", target_lang)

    label_instruction = (
        'Return 2–4 short English labels (lowercase) that help a learner organise their deck. '
        'Include: (1) part of speech (e.g. "verb", "noun", "adjective"); '
        '(2) grammatical function when relevant (e.g. "irregular verb", "expressing obligation"); '
        '(3) topic labels only when genuinely useful — nested specificity is fine '
        '(e.g. both "food" and "vegetable"). Do NOT include synonymous labels. '
        'Return as "labels": [...]'
        if need_labels else
        'Omit "labels" from your response.'
    )
    cefr_instruction = (
        f'Return the CEFR level (A1/A2/B1/B2/C1/C2) of this word for a learner of {lang_name} '
        'based on standard learner corpora. Return as "cefr_level": "..."'
        if need_cefr else
        'Omit "cefr_level" from your response.'
    )

    prompt = (
        f"Given this {lang_name} vocabulary card:\n"
        f"  {lang_name}: {target_text}\n"
        f"  English: {source_text}\n\n"
        f"{label_instruction}\n"
        f"{cefr_instruction}\n"
        "Return ONLY valid JSON, no other text."
    )
    try:
        raw = await asyncio.to_thread(lambda: translation._parse_json(translation._call(prompt)))
        labels: list[str] = []
        if need_labels:
            raw_labels = raw.get("labels") or []
            labels = [l.strip().lower() for l in raw_labels if isinstance(l, str) and l.strip()]
        cefr: str | None = None
        if need_cefr:
            cefr = (raw.get("cefr_level") or "").strip().upper()
            cefr = cefr if cefr in VALID_CEFR else None
        return labels, cefr
    except Exception as e:
        print(f"  Gemini error: {e}")
        return [], None


async def main():
    await db.init()

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # Cards missing labels OR missing cefr_level.
        async with conn.execute("""
            SELECT c.id, c.user_id, c.source_text, c.target_text, c.target_lang,
                   c.cefr_level,
                   EXISTS (SELECT 1 FROM card_labels cl WHERE cl.card_id = c.id) AS has_labels
            FROM cards c
            WHERE (
                NOT EXISTS (SELECT 1 FROM card_labels cl WHERE cl.card_id = c.id)
                OR c.cefr_level IS NULL
            )
            AND c.source_text != '' AND c.target_text != ''
            ORDER BY c.user_id, c.id
        """) as cur:
            cards = [dict(r) for r in await cur.fetchall()]

    print(f"Found {len(cards)} cards needing backfill across all users.")
    if not cards:
        print("Nothing to do.")
        return

    processed = skipped = 0
    for card in cards:
        cid = card["id"]
        uid = card["user_id"]
        src = card["source_text"]
        tgt = card["target_text"]
        lang = card["target_lang"]
        need_labels = not card["has_labels"]
        need_cefr = card["cefr_level"] is None

        flags = []
        if need_labels: flags.append("labels")
        if need_cefr:   flags.append("CEFR")
        print(f"[{processed+skipped+1}/{len(cards)}] Card {cid} (user {uid}): {tgt} — {src}  [{', '.join(flags)}]")

        labels, cefr = await fetch_labels_and_cefr(src, tgt, lang, need_labels, need_cefr)

        if not labels and cefr is None:
            print("  Nothing returned, skipping.")
            skipped += 1
            time.sleep(DELAY)
            continue

        if labels: print(f"  Labels: {labels}")
        if cefr:   print(f"  CEFR:   {cefr}")

        async with aiosqlite.connect(db.DB_PATH) as conn:
            if need_labels and labels:
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
            if need_cefr and cefr:
                await conn.execute(
                    "UPDATE cards SET cefr_level=? WHERE id=?",
                    (cefr, cid),
                )
            await conn.commit()

        processed += 1
        time.sleep(DELAY)

    print(f"\nDone. Updated {processed} cards, skipped {skipped}.")


if __name__ == "__main__":
    asyncio.run(main())
