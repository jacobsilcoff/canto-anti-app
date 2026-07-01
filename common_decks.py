"""Shared logic for the official per-language "Top 100 Words" community decks.

Used by both the one-off CLI (`scripts/generate_common_decks.py`) and the
admin-only endpoint (`POST /api/admin/generate-common-decks`). The decks are
public shared decks owned by the system user (`db.SYSTEM_USERNAME`); "official"
purely by `creator_id == system_user_id`.

Generation asks Gemini for the N most common words in a language, then recomputes
romanization with the offline oracle (`tokenizer.romanize_text`) for consistency
with the rest of the app. Idempotent per language (skips an existing deck unless
`force`).
"""
import asyncio

import db
import tokenizer
import translation

WORDS_PER_DECK = 100
DEFAULT_MODEL = "gemini-2.5-flash"
MIN_VALID_ITEMS = 20


def deck_name(info: dict) -> str:
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


def generate_items(lang: str, info: dict, api_key: str, model: str) -> list[dict]:
    """Blocking — call the LLM and build validated deck items. Run in a thread."""
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


async def _existing_deck_id(lang: str, name: str) -> int | None:
    for d in await db.list_featured_decks(target_lang=lang):
        if d["name"] == name:
            return d["id"]
    return None


async def generate_deck(
    system_id: int, lang: str, api_key: str, *,
    model: str = DEFAULT_MODEL, force: bool = False,
) -> dict:
    """Generate (or regenerate) one language's Top-100 deck.

    Returns {lang, status, deck_id?, count?, error?} where status is one of
    'created' | 'skipped' | 'error'.
    """
    info = translation.LANG_INFO.get(lang)
    if not info:
        return {"lang": lang, "status": "error", "error": "not in LANG_INFO"}
    name = deck_name(info)
    existing = await _existing_deck_id(lang, name)
    if existing and not force:
        return {"lang": lang, "status": "skipped", "deck_id": existing}
    try:
        items = await asyncio.to_thread(generate_items, lang, info, api_key, model)
    except Exception as e:  # noqa: BLE001 — surface any LLM/parse failure per lang
        return {"lang": lang, "status": "error", "error": str(e)}
    if len(items) < MIN_VALID_ITEMS:
        return {"lang": lang, "status": "error",
                "error": f"only {len(items)} valid items"}
    if existing:
        await db.delete_shared_deck(system_id, existing)
    deck_id = await db.create_shared_deck(
        system_id, name,
        f"The {len(items)} most common words in {info['name']} — a fast start for beginners.",
        lang, "public", items,
    )
    return {"lang": lang, "status": "created", "deck_id": deck_id, "count": len(items)}


async def generate_decks(
    langs: list[str], api_key: str, *, system_id: int,
    model: str = DEFAULT_MODEL, force: bool = False,
) -> list[dict]:
    """Generate decks for each language in turn. Best-effort — a failure on one
    language never aborts the rest."""
    results = []
    for lang in langs:
        results.append(await generate_deck(system_id, lang, api_key, model=model, force=force))
    return results
