"""CEFR level tagging — assign A1–C2 to vocabulary words.

Translation already tags cards at creation time (translation.py); this is the
backfill for words that entered the deck another way (lesson/tutor "add to deck",
starter deck) and so have no level. One cheap LLM call per batch; the caller
persists the result onto the cards. Used to give the tutor's large-deck drills a
rough sense of the learner's vocabulary spread without dumping the whole deck.
"""
import asyncio

from translation import DEFAULT_MODEL, LANG_INFO, _call, _parse_json

CEFR_MODEL = DEFAULT_MODEL          # cheap — tagging is easy
LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
_CHUNK = 80


def _build_prompt(name: str, words: list[str]) -> str:
    listing = ", ".join(words)
    return (
        f"You are a {name} vocabulary expert. For each {name} word below, give its CEFR "
        f"level based on how early a typical learner meets it: A1/A2 = basic & "
        f"high-frequency, B1/B2 = intermediate, C1/C2 = advanced/rare.\n\n"
        f"Words: {listing}\n\n"
        f'Return ONLY a JSON object mapping each word EXACTLY as written to its level, '
        f'e.g. {{"{words[0]}":"A1"}}. No other text.'
    )


def _tag_sync(name: str, words: list[str], api_key: str, model: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(words), _CHUNK):
        chunk = words[i:i + _CHUNK]
        try:
            parsed = _parse_json(_call(_build_prompt(name, chunk), api_key, model))
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        for w, lv in parsed.items():
            lv = lv.strip().upper() if isinstance(lv, str) else ""
            if isinstance(w, str) and w.strip() and lv in LEVELS:
                out[w.strip()] = lv
    return out


async def tag(target_lang: str, words: list[str], api_key: str, *, model: str = CEFR_MODEL) -> dict[str, str]:
    """{word: CEFR level} for the words we could confidently tag (missing/invalid dropped)."""
    words = [w for w in {w.strip() for w in words if w and w.strip()}]
    if not words or target_lang not in LANG_INFO:
        return {}
    name = LANG_INFO[target_lang].get("full_name", LANG_INFO[target_lang]["name"])
    return await asyncio.to_thread(_tag_sync, name, words, api_key, model)
