"""Generate the official per-language "Top 100 Words" seed files (dev tool).

LLM-generates each language's word list and writes a reviewable JSON file to
`seed_decks/<lang>.json` — COMMIT these files so every environment seeds the
identical cards (deterministically, no per-env regeneration; see common_decks.py).
By default it also seeds your LOCAL DB so you can eyeball the result immediately.

Usage:
    python scripts/generate_common_decks.py --langs fr es      # generate + seed locally
    python scripts/generate_common_decks.py                    # ALL languages
    python scripts/generate_common_decks.py --force            # regenerate existing files
    python scripts/generate_common_decks.py --seed-only        # skip LLM; seed DB from files
    python scripts/generate_common_decks.py --no-seed          # write files only
    python scripts/generate_common_decks.py --model gemini-2.5-pro

Requires GEMINI_API_KEY in the environment / .env (same as the app).
"""
import argparse
import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import auth
import common_decks
import db
import translation


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--langs", nargs="*", help="Language codes (default: all)")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate seed files (and force-reseed) even if present")
    ap.add_argument("--model", default=common_decks.DEFAULT_MODEL)
    ap.add_argument("--seed-only", action="store_true",
                    help="Skip LLM generation; just seed the local DB from existing files")
    ap.add_argument("--no-seed", action="store_true",
                    help="Write seed files only; don't touch the local DB")
    args = ap.parse_args()

    await db.init()
    system_id = await db.get_or_create_system_user(
        auth.hash_password(secrets.token_urlsafe(32))
    )
    langs = args.langs or list(translation.LANG_INFO.keys())

    if not args.seed_only:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("ERROR: GEMINI_API_KEY not set.", file=sys.stderr)
            sys.exit(1)
        for lang in langs:
            if not args.force and common_decks.load_seed(lang):
                print(f"  {lang}: seed file exists — use --force to regenerate")
                continue
            try:
                res = await asyncio.to_thread(
                    common_decks.generate_seed_file, lang, api_key, model=args.model
                )
                print(f"  {lang}: wrote {res['path']} ({res['count']} words)")
            except Exception as e:  # noqa: BLE001
                print(f"  {lang}: FAILED — {e}", file=sys.stderr)

    if not args.no_seed:
        results = await common_decks.seed_all(
            system_id, langs=(args.langs or None), force=True
        )
        seeded = [r for r in results if r["status"] in ("created", "updated")]
        print(f"Seeded local DB: {len(seeded)} decks "
              f"({sum(1 for r in results if r['status']=='error')} errors)")


if __name__ == "__main__":
    asyncio.run(main())
