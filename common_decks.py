"""Official per-language "Top 100 Words" community decks.

Two distinct phases, deliberately decoupled so the SAME reviewed cards ship to
every environment (generate once, seed everywhere — no per-env LLM regeneration):

1. **Generation (LLM, dev-only)** — `generate_seed_file` asks Gemini for the N
   most common words in a language, recomputes romanization with the offline
   oracle (`tokenizer.romanize_text`), and writes a reviewable JSON file to
   `seed_decks/<lang>.json`. Run via `scripts/generate_common_decks.py`, then the
   file is committed to the repo. This is the ONLY step that calls an LLM.

2. **Seeding (deterministic)** — `seed_all` loads the committed `seed_decks/*.json`
   and upserts them into the DB as public decks owned by the system user
   (`db.SYSTEM_USERNAME`), IN PLACE (`db.upsert_featured_deck` preserves deck_id
   so importers' imports/ratings survive edits). Runs at startup (create-missing)
   and via the admin endpoint (with `force` to push edits). No LLM, so dev and
   prod converge on identical cards.

A deck is "official" purely by `creator_id == system_user_id`.
"""
import json
import os
import re

import db
import tokenizer
import translation

# Some models append romanization to the native word, e.g. "का (kā)" / "する (suru)".
# Strip a trailing parenthetical so target_text is the bare word (romanization is
# recomputed separately by the offline oracle).
_TRAILING_PAREN = re.compile(r"\s*[\(（][^)）]*[\)）]\s*$")


def _clean_target(text: str) -> str:
    return _TRAILING_PAREN.sub("", (text or "").strip()).strip()

WORDS_PER_DECK = 100
DEFAULT_MODEL = "gemini-2.5-flash"
MIN_VALID_ITEMS = 20

SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_decks")


def deck_name(info: dict) -> str:
    return f"Top {WORDS_PER_DECK} {info['name']} Words"


def deck_description(info: dict, count: int) -> str:
    return f"The {count} most common words in {info['name']} — a fast start for beginners."


def seed_path(lang: str) -> str:
    return os.path.join(SEED_DIR, f"{lang}.json")


def list_seed_langs() -> list[str]:
    if not os.path.isdir(SEED_DIR):
        return []
    return sorted(
        f[:-5] for f in os.listdir(SEED_DIR)
        if f.endswith(".json") and f[:-5] in translation.LANG_INFO
    )


def load_seed(lang: str) -> dict | None:
    path = seed_path(lang)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Phase 1: LLM generation → committed seed file ────────────────────────────

# Over-request so that after dedup/cleaning we can trim to exactly WORDS_PER_DECK.
_REQUEST_COUNT = 120
_VALID_CEFR = {"A1", "A2", "B1", "B2", "C1", "C2"}


def _build_prompt(info: dict, count: int, avoid: list[str] | None = None) -> str:
    avoid_block = ""
    if avoid:
        avoid_block = (
            "\nDo NOT include any of these already-covered words:\n"
            + ", ".join(avoid) + "\n"
        )
    return (
        f"You are compiling a frequency-ordered starter vocabulary for learners of "
        f"{info.get('full_name', info['name'])}.\n"
        f"List the {count} MOST COMMON and MOST USEFUL individual words in "
        f"{info['name']}, ordered from most frequent to less frequent.\n\n"
        f"Language rules:\n{info.get('rules', '')}\n"
        f"{avoid_block}\n"
        "For EACH word provide:\n"
        "- target_text: the SINGLE word in the native script ONLY (base/dictionary "
        "form, no phrases, no romanization in parentheses).\n"
        "- source_text: a concise English gloss (1-4 words).\n"
        "- notes: ONE short, genuinely useful learner note WRITTEN IN ENGLISH — a "
        "usage nuance, common collocation, gender/measure word, or a quick memory "
        "hook. 1 sentence, in English, no romanization. Empty string only if truly "
        "nothing useful.\n"
        "- cefr: the word's CEFR level, one of A1, A2, B1, B2, C1, C2.\n"
        "- labels: 2-3 short lowercase category tags for organising a deck "
        '(e.g. "pronoun", "food", "greeting", "verb", "time", "number"). '
        "Prefer grammatical class + a topic.\n\n"
        "No duplicates. Return ONLY a JSON array, no prose, no markdown fences:\n"
        '[{"source_text": "I / me", "target_text": "…", "notes": "…", '
        '"cefr": "A1", "labels": ["pronoun"]}, ...]'
    )


def _parse_entries(lang: str, raw: str, items: list[dict], seen: set[str]) -> None:
    """Parse one LLM response into `items`, deduping against `seen` in place."""
    data = translation._parse_json(raw)
    if not isinstance(data, list):
        raise ValueError(f"{lang}: model did not return a JSON array")
    for entry in data:
        if not isinstance(entry, dict):
            continue
        target = _clean_target(entry.get("target_text"))
        source = (entry.get("source_text") or "").strip()
        if not target or not source or target in seen:
            continue
        seen.add(target)
        # Never trust model romanization — recompute with the offline oracle so
        # tones/jyutping/pinyin match ruby everywhere else. Blank for Latin scripts.
        roman = tokenizer.romanize_text(target, lang) or ""
        cefr = (entry.get("cefr") or "").strip().upper()
        labels = entry.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        labels = [str(x).strip().lower() for x in labels if str(x).strip()][:4]
        items.append({
            "source_text": source,
            "target_text": target,
            "romanization": roman,
            "notes": (entry.get("notes") or "").strip() or None,
            "cefr_level": cefr if cefr in _VALID_CEFR else None,
            "labels": labels or None,
        })


def generate_seed_file(lang: str, api_key: str, *, model: str = DEFAULT_MODEL) -> dict:
    """LLM-generate a language's word list and write seed_decks/<lang>.json.
    Over-requests + tops up so the file has EXACTLY WORDS_PER_DECK words.
    Blocking (LLM call) — call via asyncio.to_thread. Returns {lang, count, path}."""
    info = translation.LANG_INFO.get(lang)
    if not info:
        raise ValueError(f"{lang}: not in LANG_INFO")
    items: list[dict] = []
    seen: set[str] = set()
    # One retry — the model occasionally returns malformed/empty JSON.
    for attempt in range(2):
        try:
            _parse_entries(
                lang, translation._call(_build_prompt(info, _REQUEST_COUNT), api_key, model=model),
                items, seen,
            )
            break
        except Exception:
            if attempt:
                raise
    # Top-up pass if dedup/cleaning left us short of the target.
    if len(items) < WORDS_PER_DECK:
        need = WORDS_PER_DECK - len(items)
        _parse_entries(
            lang,
            translation._call(
                _build_prompt(info, need + 20, avoid=[it["target_text"] for it in items]),
                api_key, model=model,
            ),
            items, seen,
        )
    items = items[:WORDS_PER_DECK]  # trim to exactly WORDS_PER_DECK
    if len(items) < MIN_VALID_ITEMS:
        raise ValueError(f"{lang}: only {len(items)} valid items")
    os.makedirs(SEED_DIR, exist_ok=True)
    payload = {
        "lang": lang,
        "name": deck_name(info),
        "description": deck_description(info, len(items)),
        "items": items,
    }
    path = seed_path(lang)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return {"lang": lang, "count": len(items), "path": path}


# ── Phase 2: deterministic seed (committed file → DB) ────────────────────────

async def seed_deck(system_id: int, lang: str, *, force: bool = False) -> dict:
    """Load seed_decks/<lang>.json and upsert it as the system deck for that lang.

    Without `force`, an already-present deck is left untouched (create-missing);
    with `force` its items/description are refreshed from the file in place.
    Returns {lang, status: created|updated|skipped|missing|error, deck_id?}.
    """
    data = load_seed(lang)
    if not data:
        return {"lang": lang, "status": "missing"}
    info = translation.LANG_INFO.get(lang)
    if not info:
        return {"lang": lang, "status": "error", "error": "not in LANG_INFO"}
    name = data.get("name") or deck_name(info)
    items = data.get("items") or []
    if len(items) < MIN_VALID_ITEMS:
        return {"lang": lang, "status": "error", "error": f"only {len(items)} items"}
    existing = None
    for d in await db.list_featured_decks(target_lang=lang):
        if d["name"] == name:
            existing = d["id"]
            break
    if existing and not force:
        return {"lang": lang, "status": "skipped", "deck_id": existing}
    deck_id, action = await db.upsert_featured_deck(
        system_id, name,
        data.get("description") or deck_description(info, len(items)),
        lang, items,
    )
    return {"lang": lang, "status": action, "deck_id": deck_id, "count": len(items)}


async def seed_all(
    system_id: int, *, langs: list[str] | None = None, force: bool = False,
) -> list[dict]:
    """Seed every committed seed file (or a subset). Best-effort — one bad file
    never aborts the rest."""
    codes = langs if langs is not None else list_seed_langs()
    results = []
    for lang in codes:
        try:
            results.append(await seed_deck(system_id, lang, force=force))
        except Exception as e:  # noqa: BLE001 — never let one file break seeding
            results.append({"lang": lang, "status": "error", "error": str(e)})
    return results
