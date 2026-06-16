import os
import json
import time
import asyncio
from google import genai
from google.genai.errors import ServerError

_clients: dict[str, "genai.Client"] = {}


def _get_client(api_key: str):
    """Return a cached Gemini client for the given API key (one per distinct key)."""
    client = _clients.get(api_key)
    if client is None:
        client = genai.Client(api_key=api_key)
        _clients[api_key] = client
    return client


DEFAULT_MODEL = "gemini-2.5-flash-lite"
# Models users may pick when spending on their OWN key. Server validates against
# this so an arbitrary/expensive model id can't be injected via the API.
MODEL_ALLOWLIST = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]


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
            "- Provide jyutping romanisation (e.g. nei5 hou2 aa3)\n"
            "- CRITICAL — yes/no questions: ALWAYS use the A-唔-A (verb-not-verb) pattern. "
            "NEVER use 嗎 as a question particle — 嗎 is Mandarin, not Cantonese. "
            "Examples: 係唔係 (not 係嗎), 食唔食 (not 食嗎), 有冇 (contracted from 有唔有). "
            "The pattern is: positive verb + 唔 + same verb (e.g. 你係唔係香港人? = Are you from HK?). "
            "For stative verbs (係, 係, 好, etc.) always show the full A-唔-A form in examples.\n"
            "- Aspect markers: 咗 (completion 了-equivalent), 緊 (progressive), 過 (experiential), 喇 (changed state)\n"
            "- Sentence-final particles convey attitude — 囉 (obviousness), 喎 (hearsay/surprise), 㗎 (assertion/emphasis), 呀 (softener), 囉喎, etc.\n"
            "- Do NOT use Mandarin grammar structures, particles, or vocabulary"
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
    "it": {
        "name": "Italian",
        "flag": "🇮🇹",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (articles, pronouns, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": "- Use standard Italian with correct accents (à, è, é, ì, ò, ù) and grammar",
    },
    "pt": {
        "name": "Brazilian Portuguese",
        "flag": "🇧🇷",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (articles, pronouns, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": "- Use Brazilian Portuguese (Brazil) with correct accents and grammar",
    },
    "tl": {
        "name": "Tagalog",
        "flag": "🇵🇭",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (markers like ang/ng/sa, pronouns, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": (
            "- Use standard Tagalog/Filipino as spoken in the Philippines\n"
            "- Common English loanwords (Taglish) are acceptable where they are the natural everyday choice"
        ),
    },
    "ms": {
        "name": "Malay",
        "flag": "🇲🇾",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (pronouns, basic verbs/nouns, common particles), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": "- Use standard Malay (Bahasa Melayu) as used in Malaysia",
    },
    "id": {
        "name": "Indonesian",
        "flag": "🇮🇩",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (pronouns, basic verbs/nouns, common particles), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": "- Use standard Indonesian (Bahasa Indonesia)",
    },
    "ko": {
        "name": "Korean",
        "flag": "🇰🇷",
        "script": "Hangul",
        "romanization": "romaja",
        "frequency_examples": (
            "5 = extremely common (basic particles 은/는/이/가/을/를, pronouns, daily verbs/nouns), "
            "4 = common (food, family, shopping, transport), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon)"
        ),
        "rules": (
            "- Use standard Seoul Korean (표준어) in Hangul\n"
            "- Use natural politeness level (해요체 for everyday speech) unless context says otherwise\n"
            "- Provide romanisation using the Revised Romanization of Korean (e.g. annyeonghaseyo)"
        ),
    },
    "hi": {
        "name": "Hindi",
        "flag": "🇮🇳",
        "script": "Devanagari",
        "romanization": "IAST",
        "frequency_examples": (
            "5 = extremely common (postpositions, pronouns, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, Sanskritised/Urdu register)"
        ),
        "rules": (
            "- Use standard Modern Standard Hindi (मानक हिन्दी) in Devanagari script\n"
            "- Provide romanisation using IAST transliteration (e.g. namaste, pānī, kitāb)"
        ),
    },
    "te": {
        "name": "Telugu",
        "flag": "🇮🇳",
        "script": "Telugu",
        "romanization": "IAST",
        "frequency_examples": (
            "5 = extremely common (postpositions, pronouns, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, Sanskritised register)"
        ),
        "rules": (
            "- Use standard Modern Telugu (తెలుగు) in Telugu script\n"
            "- Provide romanisation using IAST/ISO transliteration (e.g. namaste, nīḻlu, tèlugu)"
        ),
    },
}

# Machine-readable script family per language, used by the frontend to pick the
# right font and (for the reader) how to render/segment text. Defaults to "latin".
SCRIPT_BY_LANG = {
    "yue": "chinese", "cmn": "chinese",
    "ko": "hangul", "hi": "devanagari", "te": "telugu",
}


def _call(prompt: str, api_key: str, model: str = DEFAULT_MODEL) -> str:
    delays = [1, 3]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            time.sleep(delay)
        try:
            return _get_client(api_key).models.generate_content(model=model, contents=prompt).text.strip()
        except ServerError as e:
            if e.status_code == 503 and attempt < len(delays):
                continue
            raise


def _call_with_image(prompt: str, image_bytes: bytes, api_key: str,
                     model: str = DEFAULT_MODEL) -> str:
    from google.genai import types as _types
    part_img = _types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    delays = [1, 3]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            time.sleep(delay)
        try:
            return _get_client(api_key).models.generate_content(
                model=model, contents=[part_img, prompt]
            ).text.strip()
        except ServerError as e:
            if e.status_code == 503 and attempt < len(delays):
                continue
            raise


def analyze_image(image_bytes: bytes, langs: list[str], api_key: str) -> dict:
    """Describe an image and suggest phrases for each target language.

    Returns: {
        "description": "A one-sentence English description",
        "suggestions": {"yue": ["phrase1", ...], "fr": [...], ...}
    }
    On any failure returns a minimal fallback so the upload still succeeds.
    """
    lang_names = ", ".join(
        f"{code} ({LANG_INFO[code]['name']})" for code in langs if code in LANG_INFO
    )
    lang_blocks = "\n".join(
        f'  "{code}": ["phrase1", "phrase2", "phrase3", "phrase4"]'
        for code in langs if code in LANG_INFO
    )
    prompt = (
        "You are a language-learning assistant. Look at this image and respond with JSON only.\n\n"
        "Tasks:\n"
        "1. Write a single short English sentence describing what you see (10–15 words).\n"
        f"2. For each target language ({lang_names}), provide 4–6 natural phrases "
        "a learner might want to say about this image. "
        "Each phrase should be in the target script (no romanization). "
        "Choose varied, conversational phrases spanning different vocabulary (objects, "
        "actions, feelings, context).\n\n"
        "Respond with ONLY this JSON (no markdown, no explanation):\n"
        "{\n"
        '  "description": "...",\n'
        '  "suggestions": {\n'
        f"{lang_blocks}\n"
        "  }\n"
        "}"
    )
    try:
        raw = _call_with_image(prompt, image_bytes, api_key)
        result = _parse_json(raw)
        description = str(result.get("description", "")).strip() or "Photo"
        suggestions: dict[str, list[str]] = {}
        raw_sugg = result.get("suggestions") or {}
        for code in langs:
            phrases = raw_sugg.get(code)
            if isinstance(phrases, list):
                suggestions[code] = [str(p).strip() for p in phrases if str(p).strip()][:6]
        return {"description": description, "suggestions": suggestions}
    except Exception:
        return {"description": "Photo", "suggestions": {}}


def _parse_json(text: str) -> dict:
    """Parse the first JSON object from the model response.

    Handles bare JSON, JSON inside ```...``` code fences, and responses
    with a preamble or suffix (e.g. 'Here is the lesson:\n{...}\nDone.').
    """
    if not text:
        raise ValueError("Empty response from model")
    # Strip markdown code fences
    if "```" in text:
        inner = text.split("```", 1)[1]
        # skip optional language tag line (```json)
        if "\n" in inner:
            inner = inner.split("\n", 1)[1]
        text = inner.rsplit("```", 1)[0]
    # Skip any preamble before the first { and trim trailing text after the last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end >= start:
        text = text[start:end + 1]
    return json.loads(text)


def _context_block(context: str) -> str:
    context = (context or "").strip()
    if not context:
        return ""
    return (
        "\nContext sentence (the word/phrase above appears in this sentence — "
        "you MUST use this to determine the correct meaning and translation; "
        "do NOT translate the context sentence itself):\n"
        f"{context}\n"
    )



_CLASSIFIER_HINT = {
    "yue": "measure word (量詞) for nouns, e.g. 個/本/張/條/杯. Empty string for non-nouns.",
    "cmn": "measure word (量词) for nouns, e.g. 个/本/张/条/杯. Empty string for non-nouns.",
    "fr": 'definite article for nouns: "le", "la", "l\'", or "les". Empty string for non-nouns.',
    "es": 'definite article for nouns: "el", "la", "los", or "las". Empty string for non-nouns.',
    "de": 'definite article for nouns: "der", "die", or "das". Empty string for non-nouns.',
    "it": 'definite article for nouns: "il", "lo", "la", "l\'", "i", "gli", or "le". Empty string for non-nouns.',
    "pt": 'definite article for nouns: "o", "a", "os", or "as". Empty string for non-nouns.',
    # Tagalog, Malay, and Indonesian have no gendered definite article system, so no
    # classifier is requested (they behave like English here).
}


def _build_prompt(text: str, target_lang: str, source_is_target: bool, context: str) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    rom = info["romanization"]
    rules = info["rules"]
    freq = info["frequency_examples"]
    classifier_hint = _CLASSIFIER_HINT.get(target_lang, "")

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
    candidate_obj.append('"notes": "..."  // see notes rules below')

    classifier_field = ""
    if classifier_hint:
        classifier_field = f'\n  "classifier": "..."  // {classifier_hint}'

    # Build language-specific etymology hint for the notes rule.
    if target_lang in ("yue", "cmn"):
        etymology_hint = (
            "If the word is a loanword from English (e.g. 巴士 from \"bus\", 的士 from \"taxi\"), "
            "or if an English word derives from it (e.g. 颱風 → \"typhoon\", 茶 → \"tea\"), "
            "mention the connection in one clause."
        )
    else:
        etymology_hint = (
            "If the word shares a Latin/Greek root with an English word, or is a recognisable cognate "
            "or false friend (e.g. French \"librairie\" ≠ \"library\"), note the connection in one clause."
        )

    classifier_rule = (
        f"- CLASSIFIER (for nouns): always provide the correct {classifier_hint} "
        f"Do not leave it blank for countable nouns.\n"
        if classifier_hint else ""
    )

    return (
        f"{direction}\n"
        "Rules:\n"
        f"{rules}\n"
        f"- NOTES: Write 1–2 sentences of genuine insight for a language learner. "
        f"Do NOT restate what the translation already makes obvious (e.g. don't explain that a verb "
        f"means 'to do X' when that is already the translation). "
        f"Focus on: register/formality, common collocations, pitfalls for English speakers, cultural context. "
        f"{etymology_hint} "
        f"Use an empty string if there is truly nothing non-obvious to add.\n"
        f"{classifier_rule}"
        f"- If the input is ambiguous and could reasonably be translated more than one way, "
        f'include up to 3 "candidates" with brief disambiguation labels. If the input is unambiguous, '
        f'return a single candidate.\n'
        f'- Include "priority" 1–5 based on vocabulary frequency in everyday {name}: '
        f"{freq}\n"
        f'- Include "suggested_labels": an array of 2–4 short English labels (lowercase) that would help '
        f'a learner organise their deck. Include: (1) part of speech (e.g. "verb", "noun", "adjective", '
        f'"adverb", "conjunction"); (2) grammatical function when relevant (e.g. "irregular verb", '
        f'"modal verb", "reflexive verb", "expressing obligation", "negation"); (3) topic labels only '
        f'when genuinely useful — nested specificity is fine and encouraged when both levels are '
        f'meaningful (e.g. both "food" and "vegetable", or both "drinks" and "coffee"). '
        f'Do NOT include multiple labels that are synonyms or near-synonyms for the same concept '
        f'(e.g. not both "obligation" and "necessity" — pick the most precise one). '
        f'Every label must add distinct organisational value.\n'
        f'- Include "cefr_level": the CEFR level of this vocabulary item (A1/A2/B1/B2/C1/C2) '
        f'for a learner of {name}. Base this on standard {name} learner corpora and frequency lists.\n'
        "Return ONLY valid JSON in this exact format, no other text:\n"
        "{\n"
        f'  "candidates": [\n'
        f"    {{ {', '.join(candidate_obj)}, \"label\": \"...\"  // short disambiguation hint, may be empty if single candidate }}\n"
        f"  ],\n"
        '  "priority": 3,\n'
        '  "cefr_level": "B1",\n'
        f'  "suggested_labels": ["...", "..."]'
        f"{classifier_field}\n"
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

    # Parse top-level fields.
    raw_labels = raw.get("suggested_labels")
    suggested_labels = [l.strip().lower() for l in raw_labels if isinstance(l, str) and l.strip()] \
        if isinstance(raw_labels, list) else []
    classifier = _strip(raw.get("classifier"))
    _VALID_CEFR = {"A1", "A2", "B1", "B2", "C1", "C2"}
    cefr_level = _strip(raw.get("cefr_level")).upper()
    cefr_level = cefr_level if cefr_level in _VALID_CEFR else None

    return {
        "candidates": candidates,
        "priority": _safe_priority(raw.get("priority", 3)),
        "suggested_labels": suggested_labels,
        "classifier": classifier,
        "cefr_level": cefr_level,
    }


_DIFFICULTY_INSTRUCTIONS: dict[str, str] = {
    "A1": (
        "- CEFR A1 (Beginner): very basic, high-frequency words only (colours, numbers, greetings, "
        "simple objects). Very short sentences (3–6 words). Present tense only. "
        "No idioms. Write around 50–70 words total."
    ),
    "A2": (
        "- CEFR A2 (Elementary): common everyday vocabulary. Simple sentences with basic connectors "
        "(and, but, because). Simple past and future allowed. Minimal idioms. "
        "Write around 70–100 words total."
    ),
    "B1": (
        "- CEFR B1 (Intermediate): mainstream vocabulary, some common idioms and fixed expressions. "
        "Mix of simple and compound sentences. Range of tenses. "
        "Write around 100–150 words total."
    ),
    "B2": (
        "- CEFR B2 (Upper Intermediate): broader vocabulary including less common words and "
        "idiomatic phrases. Varied sentence structure including subordinate clauses. "
        "Write around 130–180 words total."
    ),
    "C1": (
        "- CEFR C1 (Advanced): sophisticated vocabulary, nuanced expressions, culture-specific "
        "references, and complex grammar. Fluent, natural prose. "
        "Write around 150–200 words total."
    ),
    "C2": (
        "- CEFR C2 (Mastery): near-native register; may include literary devices, proverbs, "
        "regional expressions, and subtle pragmatic nuance. No simplification. "
        "Write around 170–220 words total."
    ),
}


async def generate_reader_text(
    prompt: str,
    target_lang: str,
    difficulty: str = "B1",
    num_paragraphs: int = 4,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Generate a short target-language text from an English description prompt.

    Returns: { title: str, content: str }
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    info = LANG_INFO[target_lang]
    name = info["name"]
    # Strip romanization-providing instructions — they conflict with the reader's
    # strict no-romanization requirement and cause the LLM to embed jyutping/pinyin
    # directly in the generated text body.
    _roman_kw = ("romanis", "transliterat", "pinyin", "jyutping", "romaja", "iast")
    rules = "\n".join(
        line for line in info["rules"].splitlines()
        if not any(kw in line.lower() for kw in _roman_kw)
    )
    difficulty_rule = _DIFFICULTY_INSTRUCTIONS.get(difficulty, _DIFFICULTY_INSTRUCTIONS["B1"])
    num_paragraphs = max(1, min(10, int(num_paragraphs)))

    full_prompt = (
        f"Write a {name} text based on the following description.\n"
        "IMPORTANT: Write ONLY native-script characters. Do NOT include any romanisation, "
        "transliteration, pinyin, jyutping, romaja, IAST, or Latin characters (except for "
        "proper nouns/brand names that are natively written in Latin script).\n"
        "Rules:\n"
        f"{rules}\n"
        f"{difficulty_rule}\n"
        f"- The text should be exactly {num_paragraphs} paragraph(s) long.\n"
        "- Write naturally, as if for a native speaker audience.\n"
        "- Do NOT include any romanisation or English translation anywhere in the content field.\n"
        "- Also provide a short English title (3–6 words) summarising the text.\n"
        "Return ONLY valid JSON, no other text:\n"
        '{ "title": "...", "content": "..." }\n\n'
        f"Description: {prompt}"
    )
    raw = await asyncio.to_thread(lambda: _parse_json(_call(full_prompt, api_key, model)))
    title = (raw.get("title") or "").strip() or prompt[:40]
    content = (raw.get("content") or "").strip()
    if not content:
        raise ValueError("Gemini returned empty content")
    return {"title": title, "content": content}


async def translate_sentence(
    text: str, target_lang: str, *, api_key: str, model: str = DEFAULT_MODEL,
) -> dict:
    """Translate a full sentence to English. Returns {english, romanization}.

    Uses a plain translation prompt rather than the vocabulary-card prompt so
    Gemini translates the whole sentence instead of just its first content word.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    info = LANG_INFO[target_lang]
    name = info["name"]
    rom = info["romanization"]

    rom_field = f'"romanization": "..."  // full sentence in {rom}\n' if rom else ""
    prompt = (
        f"Translate the following {name} sentence into natural English.\n"
        "Return ONLY valid JSON, no other text:\n"
        "{\n"
        f'  "english": "...",\n'
        f'  {rom_field}'
        "}\n\n"
        f"{name}: {text}"
    )
    try:
        raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
        return {
            "english": (raw.get("english") or "").strip(),
            "romanization": (raw.get("romanization") or "").strip() or None,
        }
    except Exception:
        return {"english": "", "romanization": None}


async def get_embedding(text: str, *, api_key: str) -> list[float] | None:
    """Return a float embedding vector for text, or None on error.

    Delegates to embeddings.embed (gemini-embedding-001, 768-dim) so there's a
    single embedding code path. Imported locally because embeddings.py imports
    from this module (avoids a circular import at load time).
    """
    try:
        import embeddings
        vecs = await embeddings.embed([text], api_key)
        return vecs[0] if vecs else None
    except Exception:
        return None


async def translate(
    text: str, target_lang: str, source_is_target: bool, context: str = "", *,
    api_key: str, model: str = DEFAULT_MODEL,
) -> dict:
    """Translate text. source_is_target=True means the user typed in target_lang and
    wants an English translation; False means the user typed English and wants target_lang.

    Returns: { candidates: [{target_text, english, romanization, label, notes}], priority }
    Always at least one candidate. UI shows a picker if >1.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = _build_prompt(text, target_lang, source_is_target, context)
    raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
    return _parse_response(raw, text, source_is_target)


async def translate_message(text: str, source_lang: str, target_lang: str, *, api_key: str) -> dict:
    """Translate a chat message and generate a brief nuance note.
    Returns {translated: str, nuance_note: str, reply_en: str}
    reply_en = English meaning (= text if source is English, else translate to en)."""
    if not text.strip():
        return {"translated": text, "nuance_note": "", "reply_en": text}
    src_name = LANG_INFO.get(source_lang, {}).get("name", source_lang) if source_lang != "en" else "English"
    tgt_name = LANG_INFO.get(target_lang, {}).get("name", target_lang) if target_lang != "en" else "English"
    rules = LANG_INFO.get(target_lang, {}).get("rules", "")
    rules_block = f"\nFollow these language rules strictly:\n{rules}" if rules else ""
    reply_en = text if source_lang == "en" else None

    if source_lang == target_lang:
        return {"translated": text, "nuance_note": "", "reply_en": reply_en or text}

    prompt = (
        f"Translate this {src_name} chat message to natural, conversational {tgt_name} "
        f"— the kind you'd text a friend.{rules_block}\n"
        "Also write a brief English nuance note (1 sentence max) explaining tone, register, "
        "idiom, or cultural meaning lost or changed in translation — empty string if nothing significant.\n"
        "Return ONLY valid JSON, nothing else:\n"
        '{"translated": "...", "nuance_note": "..."}\n\n'
        f"Message: {text}"
    )
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(None, lambda: _call(prompt, api_key, DEFAULT_MODEL))
    try:
        data = _parse_json(raw)
        translated = data.get("translated", text).strip()
        nuance_note = data.get("nuance_note", "").strip()
    except Exception:
        translated = raw.strip()
        nuance_note = ""
    if reply_en is None:
        reply_en = text  # source is already target lang; en not needed here
    return {"translated": translated, "nuance_note": nuance_note, "reply_en": reply_en}


async def analyze_message(text: str, lang: str, *, api_key: str) -> dict:
    """Grammar-check a message written in `lang` + translate it to English.
    Returns {corrections: [...], reply_en: str}
    corrections items: {original, corrected, explanation, construction}"""
    if not text.strip():
        return {"corrections": [], "reply_en": text}
    lang_name = LANG_INFO.get(lang, {}).get("name", lang)
    rules = LANG_INFO.get(lang, {}).get("rules", "")
    rules_block = f"\nLanguage rules:\n{rules}" if rules else ""
    prompt = (
        f"You are a {lang_name} tutor reviewing a short chat message.{rules_block}\n\n"
        f"Message: {text}\n\n"
        "1. Check for grammar, vocabulary, or naturalness issues.\n"
        "2. Translate the message to English.\n"
        "Return ONLY valid JSON:\n"
        '{"corrections": [{"original": "...", "corrected": "...", "explanation": "...", "construction": "..."}], '
        '"reply_en": "..."}\n'
        "corrections is empty [] if the message is natural and correct. "
        "IMPORTANT: original = only the problematic word or phrase (NOT the full message). "
        "corrected = the fixed form of that fragment only. "
        "explanation ≤ 15 words. construction = the grammar pattern name (e.g. 'A-唔-A question')."
    )
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(None, lambda: _call(prompt, api_key, DEFAULT_MODEL))
    try:
        data = _parse_json(raw)
        corrections = [
            {k: str(c.get(k, "")) for k in ("original", "corrected", "explanation", "construction")}
            for c in (data.get("corrections") or [])
            if isinstance(c, dict)
        ][:3]
        reply_en = str(data.get("reply_en", "")).strip()
    except Exception:
        corrections = []
        reply_en = ""
    return {"corrections": corrections, "reply_en": reply_en}


async def translate_simple(text: str, source_lang: str, target_lang: str, *, api_key: str) -> str:
    """One-shot translation for messages. Returns the translated text string.
    source_lang / target_lang are lang codes ('en', 'yue', 'fr', …) or 'en' for English."""
    if source_lang == target_lang or not text.strip():
        return text
    src_name = LANG_INFO.get(source_lang, {}).get("name", source_lang) if source_lang != "en" else "English"
    tgt_name = LANG_INFO.get(target_lang, {}).get("name", target_lang) if target_lang != "en" else "English"
    rules = LANG_INFO.get(target_lang, {}).get("rules", "")
    rules_block = f"\nFollow these language rules strictly:\n{rules}\n" if rules else ""
    prompt = (
        f"Translate the following {src_name} message into natural, conversational {tgt_name} "
        f"— the kind you'd actually send to a friend in a text message.{rules_block}"
        "Return ONLY the translated text, no explanations, no romanisation.\n\n"
        f"{text}"
    )
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: _call(prompt, api_key, DEFAULT_MODEL))
    return result.strip()
