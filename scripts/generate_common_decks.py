"""Generate the official per-language "Top 100 Words" community decks (CLI).

Thin wrapper over `common_decks.py` (shared with the admin endpoint
`POST /api/admin/generate-common-decks`). Idempotent + re-runnable.

Usage:
    python scripts/generate_common_decks.py                 # all LANG_INFO languages
    python scripts/generate_common_decks.py --langs fr yue  # only these
    python scripts/generate_common_decks.py --force         # regenerate existing
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
    ap.add_argument("--force", action="store_true", help="Regenerate existing decks")
    ap.add_argument("--model", default=common_decks.DEFAULT_MODEL)
    args = ap.parse_args()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    await db.init()
    system_id = await db.get_or_create_system_user(
        auth.hash_password(secrets.token_urlsafe(32))
    )
    print(f"System user id: {system_id}")

    langs = args.langs or list(translation.LANG_INFO.keys())
    for lang in langs:
        res = await common_decks.generate_deck(
            system_id, lang, api_key, model=args.model, force=args.force
        )
        if res["status"] == "created":
            print(f"  {lang}: created deck #{res['deck_id']} with {res['count']} words")
        elif res["status"] == "skipped":
            print(f"  {lang}: skipped — deck #{res['deck_id']} exists (use --force)")
        else:
            print(f"  {lang}: FAILED — {res.get('error')}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
