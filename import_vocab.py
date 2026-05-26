#!/usr/bin/env python3
"""
Import extracted_vocab.json into the cards DB.

- Skips exact duplicates (same Chinese text already in DB).
- Fills missing jyutping via Gemini (batched, 20 words per call).
- Updates existing cards that have blank jyutping.

Usage:
    python import_vocab.py [--dry-run]
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

import db
import translation as tl

DRY_RUN = "--dry-run" in sys.argv
JYUTPING_BATCH = 20   # words per Gemini call


# ── Jyutping helpers ──────────────────────────────────────────────────────────

def _fetch_jyutping_batch(words: list[str]) -> dict[str, str]:
    """Ask Gemini for jyutping for a batch of Cantonese words. Returns {word: jyutping}."""
    numbered = "\n".join(f"{i+1}. {w}" for i, w in enumerate(words))
    prompt = (
        "Provide jyutping romanisation for each of the following Cantonese words or phrases.\n"
        "Return ONLY a valid JSON array of strings, one jyutping entry per input, in the same order.\n"
        "Example output for 3 inputs: [\"nei5 hou2\", \"gam1 jat6\", \"hou2 leng3\"]\n\n"
        f"{numbered}"
    )
    raw = tl._call(prompt)
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    results = json.loads(raw.strip())
    return {words[i]: str(results[i]).strip() for i in range(len(words))}


def build_jyutping_map(missing: list[str]) -> dict[str, str]:
    """Batch-fetch jyutping for all words missing it."""
    result = {}
    total = len(missing)
    for start in range(0, total, JYUTPING_BATCH):
        batch = missing[start:start + JYUTPING_BATCH]
        end = start + len(batch)
        print(f"  Fetching jyutping [{start+1}–{end}/{total}]…", end=" ", flush=True)
        for attempt in range(3):
            try:
                result.update(_fetch_jyutping_batch(batch))
                print("OK")
                break
            except Exception as exc:
                if attempt < 2:
                    print(f"retry ({exc})", end=" ", flush=True)
                    time.sleep(2)
                else:
                    print(f"FAILED ({exc}) — leaving blank")
                    for w in batch:
                        result[w] = ""
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    await db.init()

    vocab = json.loads(Path("extracted_vocab.json").read_text())
    print(f"{'[DRY RUN] ' if DRY_RUN else ''}Loaded {len(vocab)} entries.\n")

    # ── Step 1: Pre-fetch jyutping for all entries that need it ───────────────
    needs_jyutping = [
        e["foreign"].strip()
        for e in vocab
        if e.get("foreign", "").strip() and not e.get("pronunciation", "").strip()
    ]
    jyutping_map: dict[str, str] = {}
    if needs_jyutping:
        print(f"Fetching jyutping for {len(needs_jyutping)} entries via Gemini…")
        if not DRY_RUN:
            jyutping_map = build_jyutping_map(needs_jyutping)
        else:
            print("  (skipped in dry-run)")
        print()

    # ── Step 2: Load existing DB state ───────────────────────────────────────
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT id, chinese, jyutping FROM cards") as cur:
            existing_rows = await cur.fetchall()

    existing: dict[str, dict] = {
        row["chinese"]: {"id": row["id"], "jyutping": row["jyutping"]}
        for row in existing_rows
    }

    # ── Step 3: Import / update ───────────────────────────────────────────────
    imported = updated_jy = skipped = errors = 0

    for i, entry in enumerate(vocab, 1):
        chinese  = (entry.get("foreign") or "").strip()
        jyutping = (entry.get("pronunciation") or "").strip() or jyutping_map.get(chinese, "")
        english  = (entry.get("english") or "").strip()

        if not chinese or not english:
            print(f"[{i:4}/{len(vocab)}] SKIP  (missing data)   {entry}")
            skipped += 1
            continue

        if chinese in existing:
            existing_jy = (existing[chinese]["jyutping"] or "").strip()
            if not existing_jy and jyutping:
                # Patch blank jyutping on existing card
                if not DRY_RUN:
                    async with aiosqlite.connect(db.DB_PATH) as conn:
                        await conn.execute(
                            "UPDATE cards SET jyutping=? WHERE id=?",
                            (jyutping, existing[chinese]["id"]),
                        )
                        await conn.commit()
                print(f"[{i:4}/{len(vocab)}] PATCH jyutping       {chinese} → {jyutping}")
                updated_jy += 1
            else:
                skipped += 1
            continue

        # New card
        if DRY_RUN:
            print(f"[{i:4}/{len(vocab)}] WOULD import         {chinese} — {english}")
            imported += 1
            continue

        try:
            card_id = await db.create_card(
                english=english,
                chinese=chinese,
                jyutping=jyutping,
                audio_data=None,  # generated lazily on first play
            )
            print(f"[{i:4}/{len(vocab)}] OK    id={card_id:<6}     {chinese} — {english}")
            imported += 1
        except Exception as exc:
            print(f"[{i:4}/{len(vocab)}] ERROR               {chinese}: {exc}")
            errors += 1

    print(f"\nDone — imported: {imported}, jyutping patched: {updated_jy}, "
          f"skipped: {skipped}, errors: {errors}")


if __name__ == "__main__":
    asyncio.run(main())
