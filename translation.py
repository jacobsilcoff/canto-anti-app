import os
import json
import time
import asyncio
from google import genai
from google.genai.errors import ServerError

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


_MODEL = "gemini-2.5-flash-lite"


# Per-language config. Each entry describes the human-readable name shown in prompts,
# the romanization scheme (or None for Latin-script), and any rule reminders Gemini
# needs to produce authentic output.
LANG_INFO = {
    "yue": {
        "name": "Hong Kong Cantonese",
        "flag": "🇭🇰",
        "script": "Traditional Chinese characters",
        "romanization": "jyutping",
        "frequency_examples": (
            "5 = extremely common (particles like 係/唔/喺, numbers, greetings, basic verbs/nouns), "
            "4 = common (food, family, shopping, transport), "
            "3 = intermediate (work, hobbies, casual conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": (
            "- Use Traditional Chinese characters with authentic Cantonese vocabulary "
            "(食 not 吃, 唔 not 不, 係 not 是, 喺 not 在, 佢 not 他/她, etc.)\n"
            "- Provide jyutping romanisation (e.g. nei5 hou2 aa3)"
        ),
    },
    "cmn": {
        "name": "Mandarin Chinese",
        "flag": "🇨🇳",
        "script": "Simplified Chinese characters",
        "romanization": "pinyin",
        "frequency_examples": (
            "5 = extremely common (basic particles, numbers, greetings, daily verbs/nouns), "
            "4 = common (food, family, shopping, transport), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": (
            "- Use Simplified Chinese characters with standard Mandarin vocabulary\n"
            "- Provide pinyin romanisation with tone diacritics (e.g. nǐ hǎo, not ni3 hao3)"
        ),
    },
    "fr": {
        "name": "French",
        "flag": "🇫🇷",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (articles, pronouns, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": "- Use standard European French (France) with correct accents and grammar",
    },
    "es": {
        "name": "Spanish",
        "flag": "🇪🇸",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (articles, pronouns, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": "- Use neutral Latin American Spanish unless context says otherwise",
    },
    "de": {
        "name": "German",
        "flag": "🇩🇪",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (articles, pronouns, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": "- Use standard High German (Hochdeutsch) with correct grammar, umlauts, and ß",
    },
}


def _call(prompt: str) -> str:
    delays = [1, 3]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            time.sleep(delay)
        try:
            return _get_client().models.generate_content(model=_MODEL, contents=prompt).text.strip()
        except ServerError as e:
            if e.status_code == 503 and attempt < len(delays):
                continue
            raise


def _parse_json(text: str) -> dict:
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(text)


def _context_block(context: str) -> str:
    context = (context or "").strip()
    if not context:
        return ""
    return (
        "\nContext (use this to disambiguate the meaning, "
        "but do NOT include it in the translation output):\n"
        f"{context}\n"
    )



def _build_prompt(text: str, target_lang: str, source_is_target: bool, context: str) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    rom = info["romanization"]
    rules = info["rules"]
    freq = info["frequency_examples"]

    if source_is_target:
        direction = f"Translate the following {name} text into natural English."
        input_label = name
    else:
        direction = f"Translate the following English text into {name}."
        input_label = "English"

    candidate_obj = ['"target_text": "..."']
    if rom:
        candidate_obj.append(f'"romanization": "..."  // {rom}')
    candidate_obj.append('"english": "..."  // the English translation')
    candidate_obj.append('"notes": "..."  // 1-2 sentences: usage, register, cultural context, common pitfalls. Empty string if no useful note.')

    return (
        f"{direction}\n"
        "Rules:\n"
        f"{rules}\n"
        f"- For each candidate, provide a 1–2 sentence usage note when helpful (register, cultural context, "
        f"common collocations, common pitfalls). Empty string if not useful.\n"
        f"- If the input is ambiguous and could reasonably be translated more than one way, "
        f'include up to 3 "candidates" with brief disambiguation labels. If the input is unambiguous, '
        f'return a single candidate.\n'
        f'- Include "priority" 1–5 based on vocabulary frequency in everyday {name}: '
        f"{freq}\n"
        "Return ONLY valid JSON in this exact format, no other text:\n"
        "{\n"
        f'  "candidates": [\n'
        f"    {{ {', '.join(candidate_obj)}, \"label\": \"...\"  // short disambiguation hint, may be empty if single candidate }}\n"
        f"  ],\n"
        '  "priority": 3\n'
        "}\n"
        f"{_context_block(context)}\n"
        f"{input_label}: {text}"
    )


def _safe_priority(raw) -> int:
    try:
        return max(1, min(5, int(raw)))
    except (TypeError, ValueError):
        return 3


def _strip(s) -> str:
    return (s or "").strip() if isinstance(s, str) else ""


def _parse_response(raw: dict, text: str, source_is_target: bool) -> dict:
    candidates_raw = raw.get("candidates") or []
    candidates = []
    for c in candidates_raw:
        if not isinstance(c, dict):
            continue
        candidate = {
            "target_text": _strip(c.get("target_text")),
            "english": _strip(c.get("english")) or (text if source_is_target is False else ""),
            "romanization": _strip(c.get("romanization")),
            "label": _strip(c.get("label")),
            "notes": _strip(c.get("notes")),
        }
        # If translating from target → English, the user-provided text IS the target_text.
        if source_is_target:
            candidate["target_text"] = candidate["target_text"] or text
        else:
            candidate["english"] = candidate["english"] or text
        if candidate["target_text"] and candidate["english"]:
            candidates.append(candidate)

    if not candidates:
        # Fall back to legacy single-translation shape so we never produce zero candidates.
        single = {
            "target_text": _strip(raw.get("target_text")) or (text if source_is_target else ""),
            "english": _strip(raw.get("english")) or (text if not source_is_target else ""),
            "romanization": _strip(raw.get("romanization")),
            "label": "",
            "notes": _strip(raw.get("notes")),
        }
        candidates = [single]

    return {
        "candidates": candidates,
        "priority": _safe_priority(raw.get("priority", 3)),
    }


async def generate_reader_text(prompt: str, target_lang: str) -> dict:
    """Generate a short target-language text from an English description prompt.

    Returns: { title: str, content: str }
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    info = LANG_INFO[target_lang]
    name = info["name"]
    rules = info["rules"]

    full_prompt = (
        f"Write a short {name} text (around 80–150 words) based on the following description.\n"
        "Rules:\n"
        f"{rules}\n"
        "- Write naturally, as if for a native speaker audience.\n"
        "- Write ONLY the target-language text. Do NOT include romanisation, transliteration, "
        "pinyin, jyutping, or any English translation in the text body.\n"
        "- Also provide a short English title (3–6 words) summarising the text.\n"
        "Return ONLY valid JSON, no other text:\n"
        '{ "title": "...", "content": "..." }\n\n'
        f"Description: {prompt}"
    )
    raw = await asyncio.to_thread(lambda: _parse_json(_call(full_prompt)))
    title = (raw.get("title") or "").strip() or prompt[:40]
    content = (raw.get("content") or "").strip()
    if not content:
        raise ValueError("Gemini returned empty content")
    return {"title": title, "content": content}


async def translate(text: str, target_lang: str, source_is_target: bool, context: str = "") -> dict:
    """Translate text. source_is_target=True means the user typed in target_lang and
    wants an English translation; False means the user typed English and wants target_lang.

    Returns: { candidates: [{target_text, english, romanization, label, notes}], priority }
    Always at least one candidate. UI shows a picker if >1.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = _build_prompt(text, target_lang, source_is_target, context)
    raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt)))
    return _parse_response(raw, text, source_is_target)
