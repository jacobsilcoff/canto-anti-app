"""Backfill script: add AI-suggested labels, CEFR levels, classifiers, and notes
to existing cards that are missing them.

- Labels:     skips cards that already have at least one label.
- CEFR:       skips cards that already have a cefr_level set.
- Classifier: skips cards that already have a non-empty classifier.
              Only attempted for yue/cmn cards.
- Notes:      skips cards that already have non-empty notes.
- All are fetched in a single Gemini call per card to minimise API usage.
- Cards that need none of the above are skipped entirely.

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
CLASSIFIER_LANGS = {"yue", "cmn"}


async def fetch_fields(
    source_text: str,
    target_text: str,
    target_lang: str,
    need_labels: bool,
    need_cefr: bool,
    need_classifier: bool,
    need_notes: bool,
) -> tuple[list[str], str | None, str | None, str | None]:
    info = translation.LANG_INFO.get(target_lang, {})
    lang_name = info.get("name", target_lang)
    classifier_hint = translation._CLASSIFIER_HINT.get(target_lang, "")

    fields = []
    if need_labels:
        fields.append(
            '"labels": ["...", "..."]'
            '  // 2–4 short English labels (lowercase): part of speech, grammatical function, '
            'topic when useful. No synonymous labels.'
        )
    if need_cefr:
        fields.append(f'"cefr_level": "..."  // A1/A2/B1/B2/C1/C2 for a learner of {lang_name}')
    if need_classifier:
        fields.append(f'"classifier": "..."  // {classifier_hint}')
    if need_notes:
        fields.append(
            '"notes": "..."'
            '  // 1–2 sentences: usage, register, cultural context, common pitfalls. '
            'Empty string if nothing useful.'
        )

    prompt = (
        f"Fill in the missing fields for this {lang_name} vocabulary flashcard.\n"
        f"{lang_name}: {target_text}\n"
        f"English: {source_text}\n\n"
        "Return ONLY valid JSON in this exact format, no other text:\n"
        "{\n"
        + ",\n".join(f"  {f}" for f in fields)
        + "\n}"
    )

    try:
        raw = await asyncio.to_thread(lambda: translation._parse_json(translation._call(prompt)))
        labels: list[str] = []
        if need_labels:
            raw_labels = raw.get("labels") or []
            labels = [la.strip().lower() for la in raw_labels if isinstance(la, str) and la.strip()]
        cefr: str | None = None
        if need_cefr:
            cefr = (raw.get("cefr_level") or "").strip().upper()
            cefr = cefr if cefr in VALID_CEFR else None
        classifier: str | None = None
        if need_classifier:
            classifier = (raw.get("classifier") or "").strip()
        notes: str | None = None
        if need_notes:
            notes = (raw.get("notes") or "").strip() or None
        return labels, cefr, classifier, notes
    except Exception as e:
        print(f"  Gemini error: {e}")
        return [], None, None, None


async def main():
    await db.init()

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT c.id, c.user_id, c.source_text, c.target_text, c.target_lang,
                   c.cefr_level, c.classifier, c.notes,
                   EXISTS (SELECT 1 FROM card_labels cl WHERE cl.card_id = c.id) AS has_labels
            FROM cards c
            WHERE (
                NOT EXISTS (SELECT 1 FROM card_labels cl WHERE cl.card_id = c.id)
                OR c.cefr_level IS NULL
                OR (c.classifier IS NULL OR c.classifier = '')
                OR c.notes IS NULL
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
        need_classifier = lang in CLASSIFIER_LANGS and not (card["classifier"] or "").strip()
        need_notes = card["notes"] is None

        if not any([need_labels, need_cefr, need_classifier, need_notes]):
            continue

        flags = []
        if need_labels:    flags.append("labels")
        if need_cefr:      flags.append("CEFR")
        if need_classifier: flags.append("classifier")
        if need_notes:     flags.append("notes")
        print(f"[{processed+skipped+1}/{len(cards)}] Card {cid} (user {uid}): {tgt} — {src}  [{', '.join(flags)}]")

        labels, cefr, classifier, notes = await fetch_fields(
            src, tgt, lang, need_labels, need_cefr, need_classifier, need_notes
        )

        if not labels and cefr is None and classifier is None and notes is None:
            print("  Nothing returned, skipping.")
            skipped += 1
            time.sleep(DELAY)
            continue

        if labels:     print(f"  Labels:     {labels}")
        if cefr:       print(f"  CEFR:       {cefr}")
        if classifier is not None: print(f"  Classifier: {classifier!r}")
        if notes:      print(f"  Notes:      {notes[:80]}{'…' if len(notes) > 80 else ''}")

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
                await conn.execute("UPDATE cards SET cefr_level=? WHERE id=?", (cefr, cid))
            if need_classifier and classifier is not None:
                await conn.execute("UPDATE cards SET classifier=? WHERE id=?", (classifier, cid))
            if need_notes and notes:
                await conn.execute("UPDATE cards SET notes=? WHERE id=?", (notes, cid))
            await conn.commit()

        processed += 1
        time.sleep(DELAY)

    print(f"\nDone. Updated {processed} cards, skipped {skipped}.")


if __name__ == "__main__":
    asyncio.run(main())
