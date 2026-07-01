"""Generate the official per-language "Top 100 Words" community decks.

One-time (idempotent, re-runnable) tool. For each language it asks Gemini for the
100 most common words, recomputes romanization with the offline oracle
(`tokenizer.romanize_text`) for consistency with the rest of the app, and stores
the result as a PUBLIC shared deck owned by the system user (`db.SYSTEM_USERNAME`).
These decks surface as onboarding suggestions and in the /browse Community tab.

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
import db
import tokenizer
import translation

WORDS_PER_DECK = 100
DEFAULT_MODEL = "gemini-2.5-flash"


def _deck_name(info: dict) -> str:
    return f"Top {WORDS_PER_DECK} {info['name']} Words"


def _build_prompt(info: dict) -> str:
    return (
        f"You are compiling a frequency-ordered starter vocabulary for learners of "
        f"{info.get('full_name', info['name'])}.\n"
        f"List the {WORDS_PER_DECK} MOST COMMON and MOST USEFUL individual words in "
        f"{info['name']}, ordered from most frequent to less frequent.\n\n"
        f"Language rules:\n{info.get('rules', '')}\n\n"
        "Requirements:\n"
        "- Each entry is a SINGLE common word (not a phrase or full sentence). "
        "Include high-frequency function words (pronouns, particles, common verbs, "
        "numbers) and everyday nouns/adjectives.\n"
        "- Prefer the base/dictionary form. No duplicates.\n"
        "- source_text = a concise English gloss (1-4 words).\n"
        "- notes = an optional very short usage note, or an empty string.\n\n"
        f"Return ONLY a JSON array of exactly {WORDS_PER_DECK} objects, no prose, no "
        "markdown fences:\n"
        '[{"source_text": "I / me", "target_text": "…", "notes": ""}, ...]'
    )


def _generate_items(lang: str, info: dict, api_key: str, model: str) -> list[dict]:
    raw = translation._call(_build_prompt(info), api_key, model=model)
    data = translation._parse_json(raw)
    if not isinstance(data, list):
        raise ValueError(f"{lang}: model did not return a JSON array")
    items: list[dict] = []
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        target = (entry.get("target_text") or "").strip()
        source = (entry.get("source_text") or "").strip()
        if not target or not source or target in seen:
            continue
        seen.add(target)
        # Never trust model romanization — recompute with the offline oracle so
        # tones/jyutping/pinyin match ruby everywhere else. Blank for Latin scripts.
        roman = tokenizer.romanize_text(target, lang) or ""
        items.append({
            "source_text": source,
            "target_text": target,
            "romanization": roman,
            "notes": (entry.get("notes") or "").strip() or None,
            "target_lang": lang,
        })
        if len(items) >= WORDS_PER_DECK:
            break
    return items


async def _existing_deck_id(system_id: int, lang: str, name: str) -> int | None:
    for d in await db.list_featured_decks(target_lang=lang):
        if d["name"] == name:
            return d["id"]
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--langs", nargs="*", help="Language codes (default: all)")
    ap.add_argument("--force", action="store_true", help="Regenerate existing decks")
    ap.add_argument("--model", default=DEFAULT_MODEL)
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
        info = translation.LANG_INFO.get(lang)
        if not info:
            print(f"skip {lang}: not in LANG_INFO")
            continue
        name = _deck_name(info)
        existing = await _existing_deck_id(system_id, lang, name)
        if existing and not args.force:
            print(f"skip {lang}: '{name}' already exists (deck #{existing}) — use --force")
            continue
        print(f"generating {lang}: {name} …", flush=True)
        try:
            items = await asyncio.to_thread(_generate_items, lang, info, api_key, args.model)
        except Exception as e:
            print(f"  FAILED {lang}: {e}", file=sys.stderr)
            continue
        if len(items) < 20:
            print(f"  skipped {lang}: only {len(items)} valid items", file=sys.stderr)
            continue
        if existing:
            await db.delete_shared_deck(system_id, existing)
        deck_id = await db.create_shared_deck(
            system_id, name,
            f"The {len(items)} most common words in {info['name']} — a fast start for beginners.",
            lang, "public", items,
        )
        print(f"  created deck #{deck_id} with {len(items)} words")


if __name__ == "__main__":
    asyncio.run(main())
