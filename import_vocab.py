#!/usr/bin/env python3
"""
One-shot import of extracted_vocab.json into the local cards DB.

Usage:
    python import_vocab.py [--dry-run]
"""
import asyncio
import json
import sys
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

import audio as audio_mod
import db

DRY_RUN = "--dry-run" in sys.argv


async def main():
    await db.init()

    vocab = json.loads(Path("extracted_vocab.json").read_text())
    print(f"{'[DRY RUN] ' if DRY_RUN else ''}Found {len(vocab)} entries.\n")

    imported = skipped = errors = 0

    for i, entry in enumerate(vocab, 1):
        chinese  = (entry.get("foreign") or "").strip()
        jyutping = (entry.get("pronunciation") or "").strip()
        english  = (entry.get("english") or "").strip()

        if not chinese or not english:
            print(f"[{i:3}/{len(vocab)}] SKIP  (missing data) {entry}")
            skipped += 1
            continue

        # Deduplicate by Chinese text
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(
                "SELECT id FROM cards WHERE chinese = ?", (chinese,)
            ) as cur:
                existing = await cur.fetchone()

        if existing:
            print(f"[{i:3}/{len(vocab)}] SKIP  (exists)       {chinese}")
            skipped += 1
            continue

        if DRY_RUN:
            print(f"[{i:3}/{len(vocab)}] WOULD import        {chinese} — {english}")
            imported += 1
            continue

        try:
            audio_data = await audio_mod.generate(chinese)
            card_id = await db.create_card(
                english=english,
                chinese=chinese,
                jyutping=jyutping,
                audio_data=audio_data,
            )
            print(f"[{i:3}/{len(vocab)}] OK    id={card_id:<5}      {chinese} — {english}")
            imported += 1
        except Exception as exc:
            print(f"[{i:3}/{len(vocab)}] ERROR              {chinese}: {exc}")
            errors += 1

    print(f"\nDone — imported: {imported}, skipped: {skipped}, errors: {errors}")


if __name__ == "__main__":
    asyncio.run(main())
