#!/usr/bin/env python3
"""
Import extracted_vocab.json into the cards DB for a given user.

- Skips exact duplicates (same Chinese text already in that user's deck).
- Fills missing jyutping via Gemini (batched, 20 words per call).
- Updates existing cards that have blank romanization.

Usage:
    python import_vocab.py --user <username> [--dry-run]
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

import auth
import db
import translation as tl

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

async def main(username: str, dry_run: bool):
    await db.init()
    bootstrap_password = os.getenv("APP_PASSWORD")
    if bootstrap_password:
        await db.bootstrap_admin(
            os.getenv("APP_ADMIN_USERNAME", "jsilcoff"),
            auth.hash_password(bootstrap_password),
        )

    user = await db.get_user_by_username(username)
    if not user:
        print(f"User '{username}' not found. Create it via the admin UI first.")
        sys.exit(1)
    user_id = user["id"]

    vocab = json.loads(Path("extracted_vocab.json").read_text())
    print(f"{'[DRY RUN] ' if dry_run else ''}Loaded {len(vocab)} entries for user '{username}'.\n")

    needs_jyutping = [
        e["foreign"].strip()
        for e in vocab
        if e.get("foreign", "").strip() and not e.get("pronunciation", "").strip()
    ]
    jyutping_map: dict[str, str] = {}
    if needs_jyutping:
        print(f"Fetching jyutping for {len(needs_jyutping)} entries via Gemini…")
        if not dry_run:
            jyutping_map = build_jyutping_map(needs_jyutping)
        else:
            print("  (skipped in dry-run)")
        print()

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id, target_text, romanization FROM cards WHERE user_id=?",
            (user_id,),
        ) as cur:
            existing_rows = await cur.fetchall()

    existing: dict[str, dict] = {
        row["target_text"]: {"id": row["id"], "romanization": row["romanization"]}
        for row in existing_rows
    }

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
            existing_jy = (existing[chinese]["romanization"] or "").strip()
            if not existing_jy and jyutping:
                if not dry_run:
                    async with aiosqlite.connect(db.DB_PATH) as conn:
                        await conn.execute(
                            "UPDATE cards SET romanization=? WHERE id=?",
                            (jyutping, existing[chinese]["id"]),
                        )
                        await conn.commit()
                print(f"[{i:4}/{len(vocab)}] PATCH romanization   {chinese} → {jyutping}")
                updated_jy += 1
            else:
                skipped += 1
            continue

        if dry_run:
            print(f"[{i:4}/{len(vocab)}] WOULD import         {chinese} — {english}")
            imported += 1
            continue

        try:
            card_id = await db.create_card(
                user_id=user_id,
                source_text=english,
                target_text=chinese,
                romanization=jyutping,
                target_lang="yue",
                audio_data=None,
            )
            print(f"[{i:4}/{len(vocab)}] OK    id={card_id:<6}     {chinese} — {english}")
            imported += 1
        except Exception as exc:
            print(f"[{i:4}/{len(vocab)}] ERROR               {chinese}: {exc}")
            errors += 1

    print(f"\nDone — imported: {imported}, romanization patched: {updated_jy}, "
          f"skipped: {skipped}, errors: {errors}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True, help="Username to import cards into")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.user, args.dry_run))
