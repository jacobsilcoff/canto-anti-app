import os
import json
import re
import time
import asyncio
import logging
from google import genai
from google.genai.errors import APIError

logger = logging.getLogger(__name__)

_clients: dict[str, "genai.Client"] = {}

# HTTP statuses worth retrying inline with a short backoff: provider overload /
# transient 5xx, which clear within seconds. 429 is NOT here unconditionally —
# there are two kinds and they need opposite handling (see _retryable):
#   - QUOTA 429 (body carries a QuotaFailure metric): a per-minute/day limit;
#     retrying 1–3s later just adds load inside the same window. Fail fast.
#   - CAPACITY 429 (bare RESOURCE_EXHAUSTED, no metric): Google-side load
#     shedding on the model itself, independent of your quota (the dashboard
#     shows headroom). Clears like a 503 — a short delayed retry genuinely helps.
_RETRY_STATUS = frozenset({500, 502, 503, 504})


def _retryable(e: Exception) -> bool:
    status = _api_status(e)
    if status in _RETRY_STATUS:
        return True
    return status == 429 and not quota_info(e).get("metric")


def _api_status(e: Exception) -> int | None:
    """HTTP status of a genai APIError, tolerant of SDK attribute renames.

    Older google-genai exposed `.status_code`; current versions expose `.code`.
    A bare `e.status_code` access therefore raises AttributeError on the new SDK,
    which is exactly how a retryable 503/429 got turned into an opaque 500."""
    return getattr(e, "status_code", None) or getattr(e, "code", None)


def quota_info(e: Exception) -> dict:
    """Pull the quota dimension out of a Gemini 429 (RESOURCE_EXHAUSTED) body.

    A 429 on a paid/Tier-1 project is only explicable from *which* quota tripped:
    the metric names the model + window (requests-per-day/minute, tokens-per-minute)
    and, crucially, whether it's a `free_tier` metric — a free-tier metric on a paid
    project means the API key is metered against a *different* (unbilled) project
    than the one whose Tier-1 quotas you're reading. Returns
    {metric, quota_id, retry_delay, free_tier} (any field may be None/absent)."""
    out: dict = {}
    details = getattr(e, "details", None)
    # The SDK's APIError.details is the raw response body, whose shape varies by
    # call path: normal HTTP responses carry the full envelope {"error": {...}},
    # but streaming/segmented responses carry the INNER error object directly
    # ({"code", "status", "details": [...]}) — see errors.py raise_for_response.
    # Handle both (plus a JSON string / 1-element list, which the SDK also emits).
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            details = None
    if isinstance(details, list) and len(details) == 1:
        details = details[0]
    detail_list = None
    if isinstance(details, dict):
        inner = details.get("error", details)
        if isinstance(inner, dict):
            detail_list = inner.get("details")
    for d in detail_list or []:
        if not isinstance(d, dict):
            continue
        t = d.get("@type", "")
        if t.endswith("QuotaFailure"):
            v = (d.get("violations") or [{}])[0]
            metric = v.get("quotaMetric")
            out["metric"] = metric
            out["quota_id"] = v.get("quotaId")
            if metric:
                out["free_tier"] = "free_tier" in metric or "FreeTier" in (v.get("quotaId") or "")
        elif t.endswith("RetryInfo"):
            out["retry_delay"] = d.get("retryDelay")
    if not out.get("metric"):
        # Last resort: str(APIError) embeds the whole response body (the SDK's
        # __init__ formats it in), so fish the fields out of the text. Matches
        # both JSON double quotes and Python-dict single quotes.
        s = str(e)
        m = re.search(r'''["']quotaMetric["']:\s*["']([^"']+)["']''', s)
        qid = re.search(r'''["']quotaId["']:\s*["']([^"']+)["']''', s)
        rd = re.search(r'''["']retryDelay["']:\s*["']([^"']+)["']''', s)
        if m:
            out["metric"] = m.group(1)
            out["free_tier"] = ("free_tier" in m.group(1)
                                or "FreeTier" in (qid.group(1) if qid else ""))
        if qid:
            out["quota_id"] = qid.group(1)
        if rd and not out.get("retry_delay"):
            out["retry_delay"] = rd.group(1)
    # Which model the failing call targeted — attached by _call/_call_stream/
    # _call_with_image. A 429 on gemini-2.5-flash vs -lite vs -pro points at a
    # completely different quota row in the AI Studio dashboard.
    if getattr(e, "model", None):
        out["model"] = e.model
    return out


def _get_client(api_key: str):
    """Return a cached Gemini client for the given API key (one per distinct key)."""
    client = _clients.get(api_key)
    if client is None:
        client = genai.Client(api_key=api_key)
        _clients[api_key] = client
    return client


DEFAULT_MODEL = "gemini-2.5-flash-lite"
# Image phrase analysis asks for a heavy nested per-language sender/receiver JSON
# structure — the cheapest model produces it unreliably (empty/malformed
# suggestions). Use the mid-tier model for this quality-sensitive vision task.
VISION_MODEL = "gemini-2.5-flash"
# Models users may pick when spending on their OWN key. Server validates against
# this so an arbitrary/expensive model id can't be injected via the API.
MODEL_ALLOWLIST = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]


# Per-language config. Each entry describes the human-readable name shown in prompts,
# the romanization scheme (or None for Latin-script), and any rule reminders Gemini
# needs to produce authentic output.
LANG_INFO = {
    "yue": {
        "name": "Cantonese",
        "full_name": "Hong Kong Cantonese",
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
        "name": "Mandarin",
        "full_name": "Mandarin Chinese",
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
        "name": "Portuguese",
        "full_name": "Brazilian Portuguese",
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
    "ja": {
        "name": "Japanese",
        "flag": "🇯🇵",
        "script": "Japanese (kanji + kana)",
        "romanization": "romaji",
        "frequency_examples": (
            "5 = extremely common (particles は/が/を/に, pronouns, copula, basic verbs/nouns), "
            "4 = common (food, family, daily life, greetings), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, uncommon kanji)"
        ),
        "rules": (
            "- Use standard Japanese with the natural kanji/hiragana/katakana mix\n"
            "- Provide romaji romanisation (e.g. konnichiwa, arigatou)\n"
            "- Use polite form (です/ます) unless context says otherwise\n"
            "- Katakana for loanwords (コーヒー, パソコン, etc.)"
        ),
    },
    "bn": {
        "name": "Bengali",
        "flag": "🇧🇩",
        "script": "Bengali (Bangla)",
        "romanization": "IAST",
        "frequency_examples": (
            "5 = extremely common (postpositions, pronouns, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, Sanskritised register)"
        ),
        "rules": (
            "- Use standard Modern Bengali (বাংলা) in Bengali script\n"
            "- Provide romanisation using IAST transliteration (e.g. āmi, tumi, dhanyabād)"
        ),
    },
    "ur": {
        "name": "Urdu",
        "flag": "🇵🇰",
        "script": "Nastaliq (Arabic script)",
        "romanization": "romanized Urdu",
        "frequency_examples": (
            "5 = extremely common (postpositions, pronouns, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, Persianised/Arabic register)"
        ),
        "rules": (
            "- Use standard Modern Urdu (اردو) in Nastaliq/Arabic script\n"
            "- Write right-to-left\n"
            "- Provide romanisation (e.g. shukriya, mera naam, kitaab)"
        ),
    },
    "ar": {
        "name": "Arabic",
        "flag": "🇸🇦",
        "script": "Arabic script",
        "romanization": "romanized Arabic",
        "frequency_examples": (
            "5 = extremely common (articles, pronouns, basic verbs/nouns, prepositions), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, classical, specialised)"
        ),
        "rules": (
            "- Use Modern Standard Arabic (العربية الفصحى) in Arabic script\n"
            "- Write right-to-left\n"
            "- Provide romanisation (e.g. marhaba, shukran, kitaab)\n"
            "- Include short vowel diacritics (tashkeel) on the Arabic text for learners"
        ),
    },
    "sw": {
        "name": "Swahili",
        "flag": "🇹🇿",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (pronouns, basic verbs/nouns, common prefixes), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, archaic)"
        ),
        "rules": "- Use standard Swahili (Kiswahili) as used in Tanzania/Kenya",
    },
    "ru": {
        "name": "Russian",
        "flag": "🇷🇺",
        "script": "Cyrillic",
        "romanization": "romanized Russian",
        "frequency_examples": (
            "5 = extremely common (pronouns, prepositions, basic verbs/nouns, conjunctions), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, archaic)"
        ),
        "rules": (
            "- Use standard Russian (русский) in Cyrillic script\n"
            "- Provide romanisation (e.g. privet, spasibo, khorosho)\n"
            "- Include stress marks (ударение) on the Russian text for learners where helpful"
        ),
    },
    "vi": {
        "name": "Vietnamese",
        "flag": "🇻🇳",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (particles, pronouns, basic verbs/nouns, classifiers), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, archaic)"
        ),
        "rules": (
            "- Use standard Vietnamese with correct diacritics and tone marks\n"
            "- Use Northern (Hanoi) pronunciation conventions unless context says otherwise"
        ),
    },
    "fa": {
        "name": "Farsi",
        "flag": "🇮🇷",
        "script": "Persian (Arabic script)",
        "romanization": "romanized Farsi",
        "frequency_examples": (
            "5 = extremely common (pronouns, prepositions, basic verbs/nouns, ezafe), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, specialised, archaic Arabic loanwords)"
        ),
        "rules": (
            "- Use standard Modern Persian (فارسی) in Persian script\n"
            "- Write right-to-left\n"
            "- Provide romanisation (e.g. salam, mersi, ketab)\n"
            "- Use Persian vocabulary over Arabic alternatives where both exist (e.g. دانشگاه not جامعة)"
        ),
    },
    "tr": {
        "name": "Turkish",
        "flag": "🇹🇷",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (pronouns, postpositions, basic verbs, -mek/-mak infinitives), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, travel, conversation), "
            "2 = less common (formal, bureaucratic, specific topics), "
            "1 = rare or advanced (literary, Ottoman loanwords, technical)"
        ),
        "rules": (
            "- Use standard Istanbul Turkish with correct vowel harmony and agglutination\n"
            "- Preserve Turkish-specific characters: ç, ğ, ı, ö, ş, ü (and their capitals Ç, Ğ, İ, Ö, Ş, Ü)\n"
            "- Use the dotless ı / dotted İ distinction correctly"
        ),
    },
    "nl": {
        "name": "Dutch",
        "flag": "🇳🇱",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (pronouns, articles, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal, specific topics), "
            "1 = rare or advanced (literary, archaic, highly specialised)"
        ),
        "rules": (
            "- Use standard Netherlands Dutch (Standaardnederlands)\n"
            "- Preserve Dutch digraphs (ij, oe, ui, eu, ei) and diacritics where needed"
        ),
    },
    "pl": {
        "name": "Polish",
        "flag": "🇵🇱",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (pronouns, prepositions, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, conversation, travel), "
            "2 = less common (formal, specific topics), "
            "1 = rare or advanced (literary, archaic, highly specialised)"
        ),
        "rules": (
            "- Use standard Polish with correct diacritics: ą, ć, ę, ł, ń, ó, ś, ź, ż\n"
            "- Use natural conversational register unless formal is requested"
        ),
    },
    "sv": {
        "name": "Swedish",
        "flag": "🇸🇪",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (pronouns, articles, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal, specific topics), "
            "1 = rare or advanced (literary, archaic, highly specialised)"
        ),
        "rules": (
            "- Use standard Swedish (rikssvenska)\n"
            "- Preserve Swedish-specific characters: å, ä, ö"
        ),
    },
    "nb": {
        "name": "Norwegian",
        "full_name": "Norwegian Bokmål",
        "flag": "🇳🇴",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (pronouns, articles, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, hobbies, conversation), "
            "2 = less common (formal, specific topics), "
            "1 = rare or advanced (literary, archaic, Nynorsk-only)"
        ),
        "rules": (
            "- Use Bokmål (the more common written standard)\n"
            "- Preserve Norwegian-specific characters: å, æ, ø"
        ),
    },
    "ro": {
        "name": "Romanian",
        "flag": "🇷🇴",
        "script": "Latin script",
        "romanization": None,
        "frequency_examples": (
            "5 = extremely common (pronouns, articles, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, conversation, travel), "
            "2 = less common (formal, specific topics), "
            "1 = rare or advanced (literary, archaic, highly specialised)"
        ),
        "rules": (
            "- Use standard Romanian with correct diacritics: ă, â, î, ș, ț\n"
            "- Use the definite article enclitic system correctly (e.g. casa = the house)"
        ),
    },
    "uk": {
        "name": "Ukrainian",
        "flag": "🇺🇦",
        "script": "Cyrillic",
        "romanization": "romanized Ukrainian",
        "frequency_examples": (
            "5 = extremely common (pronouns, prepositions, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, conversation, travel), "
            "2 = less common (formal, specific topics), "
            "1 = rare or advanced (literary, archaic, highly specialised)"
        ),
        "rules": (
            "- Use standard Ukrainian (українська) in Cyrillic script\n"
            "- Use Ukrainian vocabulary, NOT Russian equivalents (e.g. гарний not красивый)\n"
            "- Provide romanisation using Ukrainian Latin transliteration\n"
            "- Include the Ukrainian-specific letters: ґ, є, і, ї"
        ),
    },
    "el": {
        "name": "Greek",
        "flag": "🇬🇷",
        "script": "Greek alphabet",
        "romanization": "romanized Greek",
        "frequency_examples": (
            "5 = extremely common (pronouns, articles, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, conversation, travel), "
            "2 = less common (formal, specific topics), "
            "1 = rare or advanced (literary, archaic/katharevousa, highly specialised)"
        ),
        "rules": (
            "- Use modern standard Greek (Demotic / Δημοτική)\n"
            "- Write in the Greek alphabet with correct accent marks (tonos)\n"
            "- Provide romanisation (e.g. kalimera, efcharisto, nero)"
        ),
    },
    "th": {
        "name": "Thai",
        "flag": "🇹🇭",
        "script": "Thai script",
        "romanization": "romanized Thai (RTGS)",
        "frequency_examples": (
            "5 = extremely common (pronouns, particles, basic verbs/nouns, classifiers), "
            "4 = common (food, family, daily life, polite particles), "
            "3 = intermediate (work, travel, conversation), "
            "2 = less common (formal register, specific topics), "
            "1 = rare or advanced (literary, royal vocabulary, Pali/Sanskrit loanwords)"
        ),
        "rules": (
            "- Use standard Central Thai (ภาษาไทยกลาง)\n"
            "- Write in Thai script without spaces between words (standard Thai orthography)\n"
            "- Provide romanisation using RTGS (Royal Thai General System)\n"
            "- Include polite particles ครับ/ค่ะ where natural"
        ),
    },
    "he": {
        "name": "Hebrew",
        "flag": "🇮🇱",
        "script": "Hebrew alphabet",
        "romanization": "romanized Hebrew",
        "frequency_examples": (
            "5 = extremely common (pronouns, prepositions, basic verbs/nouns), "
            "4 = common (food, family, daily life), "
            "3 = intermediate (work, conversation, travel), "
            "2 = less common (formal, specific topics), "
            "1 = rare or advanced (literary, Biblical/archaic, highly specialised)"
        ),
        "rules": (
            "- Use Modern Hebrew (עברית חדשה) in Hebrew script\n"
            "- Write right-to-left without vowel points (niqqud) — standard unpointed script\n"
            "- Provide romanisation (e.g. shalom, toda, boker tov)\n"
            "- Use informal/spoken register unless formal is requested"
        ),
    },
}

# Machine-readable script family per language, used by the frontend to pick the
# right font and (for the reader) how to render/segment text. Defaults to "latin".
SCRIPT_BY_LANG = {
    "yue": "chinese", "cmn": "chinese", "ja": "japanese",
    "ko": "hangul", "hi": "devanagari", "te": "telugu",
    "bn": "bengali", "ur": "arabic", "ar": "arabic", "fa": "arabic",
    "ru": "cyrillic", "uk": "cyrillic",
    "el": "greek", "th": "thai", "he": "hebrew",
}


def _call(prompt: str, api_key: str, model: str = DEFAULT_MODEL,
          *, thinking_budget: int | None = None) -> str:
    """Single Gemini text call with 503 backoff. Pass `thinking_budget=0` to turn
    OFF the 2.5 models' built-in 'thinking' pass — a big latency win for focused,
    low-reasoning calls (e.g. the flashcard Q&A pop-over) where it adds seconds
    without improving the answer."""
    config = None
    if thinking_budget is not None:
        from google.genai import types as _types
        config = _types.GenerateContentConfig(
            thinking_config=_types.ThinkingConfig(thinking_budget=thinking_budget)
        )
    delays = [1, 3]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            time.sleep(delay)
        try:
            resp = _get_client(api_key).models.generate_content(
                model=model, contents=prompt, config=config)
            text = resp.text
            # `.text` is None when the response carried no text part (safety
            # block / empty candidate). `.strip()` on None raises AttributeError
            # and hides the real cause — surface it instead.
            if text is None:
                reason = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
                if reason is None:
                    cands = getattr(resp, "candidates", None) or []
                    reason = getattr(cands[0], "finish_reason", "unknown") if cands else "no candidates"
                raise ValueError(f"Model returned no text (reason: {reason})")
            return text.strip()
        except APIError as e:
            e.model = model            # which quota row to look at (see quota_info)
            if _retryable(e) and attempt < len(delays):
                continue
            raise


async def _call_stream(prompt: str, api_key: str, model: str = DEFAULT_MODEL,
                       *, thinking_budget: int | None = None):
    """Async generator yielding text chunks from a streaming Gemini call.

    The SDK's streaming iterator is blocking, so a worker thread drives it and
    pushes chunks onto an asyncio.Queue the event loop drains — letting the route
    forward tokens to the client the moment the model emits them."""
    config = None
    if thinking_budget is not None:
        from google.genai import types as _types
        config = _types.GenerateContentConfig(
            thinking_config=_types.ThinkingConfig(thinking_budget=thinking_budget)
        )
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    def _produce():
        try:
            stream = _get_client(api_key).models.generate_content_stream(
                model=model, contents=prompt, config=config)
            for chunk in stream:
                # `.text` can RAISE (not just return None) on some SDK versions
                # for non-text/thought/empty chunks — guard it so one odd chunk
                # (often the trailing usage-metadata one) can't abort the stream.
                try:
                    text = chunk.text
                except Exception:
                    text = None
                if text:
                    loop.call_soon_threadsafe(queue.put_nowait, text)
        except Exception as exc:                    # surfaced to the consumer
            if isinstance(exc, APIError):
                exc.model = model      # which quota row to look at (see quota_info)
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    fut = loop.run_in_executor(None, _produce)
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        await fut


def _call_with_image(prompt: str, image_bytes: bytes, api_key: str,
                     model: str = DEFAULT_MODEL) -> str:
    from google.genai import types as _types
    part_img = _types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    delays = [1, 3]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            time.sleep(delay)
        try:
            resp = _get_client(api_key).models.generate_content(
                model=model, contents=[part_img, prompt]
            )
            # resp.text is None when the response carried no text part — e.g. the
            # prompt feedback / a candidate was blocked by safety filters. Calling
            # .strip() on None raises AttributeError, which callers would otherwise
            # swallow into a generic fallback, hiding the real cause. Surface it.
            text = resp.text
            if text is None:
                reason = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
                if reason is None:
                    cands = getattr(resp, "candidates", None) or []
                    reason = getattr(cands[0], "finish_reason", "unknown") if cands else "no candidates"
                raise ValueError(f"Vision model returned no text (reason: {reason})")
            return text.strip()
        except APIError as e:
            e.model = model            # which quota row to look at (see quota_info)
            if _retryable(e) and attempt < len(delays):
                continue
            raise


def analyze_image(image_bytes: bytes, langs: list[str], api_key: str) -> dict:
    """Describe an image and suggest phrases for each target language.

    Returns: {
        "description": "A one-sentence English description",
        "descriptions": {"yue": "一句廣東話描述", "fr": "Une phrase en français", ...},
        "suggestions": {
            "yue": [{"text": "phrase", "en": "English meaning"}, ...],
            ...
        }
    }
    On any failure returns a minimal fallback so the upload still succeeds.
    """
    lang_names = ", ".join(
        f"{code} ({LANG_INFO[code].get('full_name', LANG_INFO[code]['name'])})" for code in langs if code in LANG_INFO
    )
    desc_blocks = "\n".join(
        f'  "{code}": "one sentence in {LANG_INFO[code].get("full_name", LANG_INFO[code]["name"])} describing the image"'
        for code in langs if code in LANG_INFO
    )
    phrase_blocks = "\n".join(
        f'  "{code}": {{\n'
        f'    "sender": [{{"text": "phrase the photo-sender would say", "en": "English"}}],\n'
        f'    "receiver": [{{"text": "phrase the photo-receiver would say in response", "en": "English"}}]\n'
        f'  }}'
        for code in langs if code in LANG_INFO
    )
    prompt = (
        "You are a language-learning assistant. Look at this image and respond with JSON only.\n\n"
        "Tasks:\n"
        "1. Write a single short English sentence describing what you see (10–15 words).\n"
        f"2. For each target language ({lang_names}), write ONE sentence describing the image "
        "in that language (target script only, no romanization).\n"
        f"3. For each target language, provide 3–5 conversational phrases for EACH perspective:\n"
        "   - sender: what the person who sent the photo might say (e.g. sharing context, "
        "expressing feeling about the scene, captioning it)\n"
        "   - receiver: what the person viewing the photo might say in response (e.g. reacting, "
        "asking a question, expressing emotion)\n"
        "Each phrase must be in the target script only (no romanization). "
        "Include a short English translation for each phrase. "
        "Make the two perspectives contextually distinct when the image warrants it "
        "(e.g. a graduation photo: sender says 'just graduated!', receiver says 'congrats!').\n\n"
        "Respond with ONLY this JSON (no markdown, no explanation):\n"
        "{\n"
        '  "description": "...",\n'
        '  "descriptions": {\n'
        f"{desc_blocks}\n"
        "  },\n"
        '  "suggestions": {\n'
        f"{phrase_blocks}\n"
        "  }\n"
        "}"
    )
    try:
        raw = _call_with_image(prompt, image_bytes, api_key, model=VISION_MODEL)
        result = _parse_json(raw)
        description = str(result.get("description", "")).strip() or "Photo"
        descriptions: dict[str, str] = {}
        raw_desc = result.get("descriptions") or {}
        for code in langs:
            d = str(raw_desc.get(code) or "").strip()
            if d:
                descriptions[code] = d
        def _clean_phrases(raw_list) -> list[dict]:
            out = []
            for p in (raw_list or []):
                if isinstance(p, dict):
                    text = str(p.get("text") or "").strip()
                    en = str(p.get("en") or "").strip()
                elif isinstance(p, str):
                    text, en = p.strip(), ""
                else:
                    continue
                if text:
                    out.append({"text": text, "en": en})
            return out[:5]

        suggestions: dict[str, dict] = {}
        raw_sugg = result.get("suggestions") or {}
        for code in langs:
            entry = raw_sugg.get(code)
            if isinstance(entry, dict):
                suggestions[code] = {
                    "sender": _clean_phrases(entry.get("sender")),
                    "receiver": _clean_phrases(entry.get("receiver")),
                }
            elif isinstance(entry, list):
                # Legacy flat list — assign to both perspectives
                phrases = _clean_phrases(entry)
                suggestions[code] = {"sender": phrases, "receiver": phrases}
        if not suggestions:
            logger.warning("analyze_image: parsed response but no usable suggestions "
                           "(langs=%s); phrase menu will be empty", langs)
        return {"description": description, "descriptions": descriptions,
                "suggestions": suggestions}
    except Exception:
        logger.exception("analyze_image failed (langs=%s) — returning fallback", langs)
        return {"description": "Photo", "descriptions": {}, "suggestions": {}}


def get_classifiers_batch(words: list[str], lang: str, api_key: str) -> dict[str, str]:
    """Return a mapping of word → classifier/article for target-language words.

    Makes a single Gemini call. Returns empty string for non-nouns.
    Only useful for languages in _CLASSIFIER_HINT; returns {} for others.
    Caps at 60 words per call.
    """
    if not words or lang not in _CLASSIFIER_HINT:
        return {}
    hint = _CLASSIFIER_HINT[lang]
    name = LANG_INFO.get(lang, {}).get("name", lang)
    batch = words[:60]
    word_list = "\n".join(f"- {w}" for w in batch)
    prompt = (
        f"For each of the following {name} words, provide the correct {hint}\n"
        "Return ONLY valid JSON: an object mapping each word exactly (as given) to its "
        "classifier/article string. Use empty string \"\" for verbs, adjectives, and "
        "other non-nouns. No explanations.\n\n"
        f"Words:\n{word_list}\n\n"
        "Return ONLY this JSON object, nothing else."
    )
    try:
        raw = _call(prompt, api_key)
        data = _parse_json(raw)
        if isinstance(data, dict):
            return {str(k): str(v).strip() for k, v in data.items()}
    except Exception:
        pass
    return {}


def _parse_json(text: str):
    """Parse the first JSON object OR array from the model response.

    Handles bare JSON, JSON inside ```...``` code fences, and responses
    with a preamble or suffix (e.g. 'Here is the lesson:\n{...}\nDone.').
    Returns a dict for object payloads and a list for array payloads.
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
    text = text.strip()
    # Trim any preamble/suffix around the payload. Detect whether the payload is
    # an array ([...]) or an object ({...}) by whichever delimiter appears first.
    obj_start = text.find("{")
    arr_start = text.find("[")
    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        primary = (arr_start, text.rfind("]"))
        fallback = (obj_start, text.rfind("}"))
    else:
        primary = (obj_start, text.rfind("}"))
        fallback = (arr_start, text.rfind("]"))
    for start, end in (primary, fallback):
        if start != -1 and end != -1 and end >= start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    # Last resort: try the raw text (may raise, which callers handle)
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
    name = info.get("full_name", info["name"])
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
            "ONLY if the word is a GENUINE loanword from English (e.g. 巴士 from \"bus\", 的士 from \"taxi\"), "
            "or if an English word genuinely derives from it historically (e.g. 颱風 → \"typhoon\", 茶 → \"tea\"), "
            "mention the connection in one clause. Do NOT fabricate etymological connections — most Chinese words "
            "have NO relation to their English translations. Never claim a Chinese word 'comes from' or 'is a cognate of' "
            "its English translation unless it is a well-documented loanword."
        )
    elif target_lang in ("ko", "hi", "te"):
        etymology_hint = (
            "ONLY if the word is a GENUINE loanword from English (e.g. Korean 컴퓨터 from \"computer\"), "
            "mention the connection in one clause. Do NOT fabricate etymological connections — most words "
            "have NO relation to their English translations."
        )
    else:
        etymology_hint = (
            "If the word shares a Latin/Greek root with an English word, or is a recognisable cognate "
            "or false friend (e.g. French \"librairie\" ≠ \"library\"), note the connection in one clause. "
            "Do NOT fabricate connections — only mention genuine, well-known etymological links."
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


_DIFFICULTY_LEVEL: dict[str, str] = {
    "A1": (
        "CEFR A1 (Beginner): very basic, high-frequency words only (colours, numbers, greetings, "
        "simple objects). Very short sentences (3–6 words). Present tense only. No idioms."
    ),
    "A2": (
        "CEFR A2 (Elementary): common everyday vocabulary. Simple sentences with basic connectors "
        "(and, but, because). Simple past and future allowed. Minimal idioms."
    ),
    "B1": (
        "CEFR B1 (Intermediate): mainstream vocabulary, some common idioms and fixed expressions. "
        "Mix of simple and compound sentences. Range of tenses."
    ),
    "B2": (
        "CEFR B2 (Upper Intermediate): broader vocabulary including less common words and "
        "idiomatic phrases. Varied sentence structure including subordinate clauses."
    ),
    "C1": (
        "CEFR C1 (Advanced): sophisticated vocabulary, nuanced expressions, culture-specific "
        "references, and complex grammar. Fluent, natural prose."
    ),
    "C2": (
        "CEFR C2 (Mastery): near-native register; may include literary devices, proverbs, "
        "regional expressions, and subtle pragmatic nuance. No simplification."
    ),
}

_DIFFICULTY_INSTRUCTIONS: dict[str, str] = {
    level: f"- {desc} Write around {wc} words total."
    for level, desc, wc in [
        ("A1", _DIFFICULTY_LEVEL["A1"], "50–70"),
        ("A2", _DIFFICULTY_LEVEL["A2"], "70–100"),
        ("B1", _DIFFICULTY_LEVEL["B1"], "100–150"),
        ("B2", _DIFFICULTY_LEVEL["B2"], "130–180"),
        ("C1", _DIFFICULTY_LEVEL["C1"], "150–200"),
        ("C2", _DIFFICULTY_LEVEL["C2"], "170–220"),
    ]
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
    name = info.get("full_name", info["name"])
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


async def generate_reader_text_from_image(
    image_bytes: bytes,
    target_lang: str,
    difficulty: str = "B1",
    num_paragraphs: int = 4,
    prompt: str = "",
    existing_titles: list[str] | None = None,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Generate a reader text inspired by an uploaded image.

    Returns: { title: str, content: str, description: str }
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    _roman_kw = ("romanis", "transliterat", "pinyin", "jyutping", "romaja", "iast")
    rules = "\n".join(
        line for line in info["rules"].splitlines()
        if not any(kw in line.lower() for kw in _roman_kw)
    )
    difficulty_rule = _DIFFICULTY_INSTRUCTIONS.get(difficulty, _DIFFICULTY_INSTRUCTIONS["B1"])
    num_paragraphs = max(1, min(10, int(num_paragraphs)))

    user_direction = ""
    if prompt:
        user_direction = f"\nUser direction: {prompt}\nUse the image as the visual setting and the user's direction to guide the story.\n"
    else:
        user_direction = "\nLook at the image and write an interesting, creative story inspired by what you see.\n"

    existing_block = ""
    if existing_titles:
        titles_str = ", ".join(f'"{t}"' for t in existing_titles[:30])
        existing_block = (
            f"\nThe user already has these stories: [{titles_str}]. "
            "Write something DIFFERENT — pick a fresh angle, genre, or theme.\n"
        )

    full_prompt = (
        f"Write a {name} text inspired by this image.{user_direction}{existing_block}"
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
        "- Also provide a one-sentence English description of what you see in the image.\n"
        "Return ONLY valid JSON, no other text:\n"
        '{ "title": "...", "content": "...", "description": "..." }\n'
    )
    raw = await asyncio.to_thread(
        lambda: _parse_json(_call_with_image(full_prompt, image_bytes, api_key, model))
    )
    title = (raw.get("title") or "").strip() or "Image Story"
    content = (raw.get("content") or "").strip()
    description = (raw.get("description") or "").strip() or "Image-inspired story"
    if not content:
        raise ValueError("Gemini returned empty content")
    return {"title": title, "content": content, "description": description}


def _split_source_sentences(text: str) -> list[str]:
    """Split source (usually English) text into sentences — deterministic.

    The original sentences are kept VERBATIM (this is what the reader's
    translation panel shows), so we never trust the LLM for them. Splits on
    sentence-final punctuation followed by whitespace, and on newlines.
    """
    text = (text or "").replace("\r", "")
    # Strip wiki-style bracket citations/tags (e.g. [3], [edit], [citation needed])
    # so they don't block sentence-boundary detection.  We strip BEFORE splitting
    # but the originals are gone — this is acceptable because citations are noise
    # in a language-learning reading.
    text = re.sub(r'\[(?:\d+|edit|citation needed)\]', '', text)
    # Insert a newline before runs of title-case / uppercase words that are glued
    # directly after a sentence-ending period (trafilatura sometimes concatenates
    # headings without whitespace, e.g. "…mountains.HistoryFirst Nations").
    text = re.sub(r'(?<=[.!?。！？])(?=[A-ZÀ-ɏ])', '\n', text)
    out: list[str] = []
    for para in text.split("\n"):
        para = " ".join(para.split())  # collapse internal whitespace
        if not para:
            continue
        # Split after . ! ? (and CJK/fullwidth enders) when whitespace follows.
        for chunk in re.split(r'(?<=[.!?。！？])\s+', para):
            chunk = chunk.strip()
            if chunk:
                out.append(chunk)
    return out


async def translate_article_to_reading(
    source_text: str,
    target_lang: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    batch_size: int = 20,
    difficulty: str = "",
) -> dict:
    """Faithfully translate a source text (fetched article / PDF), sentence by
    sentence, into the target language for the reader.

    LITERAL translation, NOT adaptation — never merge/split/summarise/reorder/
    simplify. We split the source ourselves (so the originals are exact) and
    translate in batches that return a JSON array of strings in the SAME ORDER,
    one per input sentence. Alignment is enforced by COUNT: if a batch doesn't
    come back with exactly N translations we split it in half and retry (down to
    a single sentence), so a flaky batch can never drop or shift the rest.

    Returns `{ segments: [{target, english}] }` — `english` is the EXACT original
    source sentence (shown in the reader, no target→English round-trip),
    positionally aligned with its `target` translation.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    _roman_kw = ("romanis", "transliterat", "pinyin", "jyutping", "romaja", "iast")
    rules = "\n".join(
        line for line in info["rules"].splitlines()
        if not any(kw in line.lower() for kw in _roman_kw)
    )

    src_sents = _split_source_sentences((source_text or "")[:12_000])
    if not src_sents:
        raise ValueError("No source text to translate")

    sem = asyncio.Semaphore(3)

    def _as_target(x) -> str:
        if isinstance(x, str):
            s = x.strip().replace("\n", " ")
        elif isinstance(x, dict):  # tolerate {"target": ...} / {"text": ...}
            s = str(x.get("target") or x.get("text") or "").strip().replace("\n", " ")
        else:
            return ""
        return re.sub(r'\*{1,2}', '', s)

    diff_instr = _DIFFICULTY_LEVEL.get(difficulty, "") if difficulty else ""
    adapt_line = (
        f"\nADAPT the language to this level (simplify vocabulary and grammar as needed, "
        f"but keep the MEANING of each sentence):\n{diff_instr}\n"
        if diff_instr else ""
    )

    async def _translate_chunk(chunk: list[str]) -> list[str]:
        """Return exactly len(chunk) translations, positionally aligned."""
        numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(chunk))
        prompt = (
            f"Translate each numbered sentence below into natural {name}.\n"
            "Translate FAITHFULLY and COMPLETELY — do NOT merge, split, summarise, "
            "reorder, add, or omit. Output the translations IN THE SAME ORDER, "
            "exactly ONE per input sentence (same count). Even a heading or fragment "
            "gets its own translation.\n"
            f"{adapt_line}"
            "Native script only (no romanisation), except proper nouns/brand names.\n"
            "No **bold** or other inline formatting.\n"
            "If a sentence starts with ## or ### (a heading marker), keep the "
            "marker prefix and translate only the text after it.\n"
            f"Rules:\n{rules}\n"
            'Return ONLY a JSON array of strings, e.g. ["译文1", "译文2"], same length '
            "and order as the input.\n\n"
            f"Sentences:\n{numbered}"
        )
        try:
            async with sem:
                raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
        except APIError:
            # Rate-limit / overload — do NOT split-and-retry: fanning out more
            # calls into a rate limit is what turns one 429 into a storm. Abort
            # the whole generation with one clean error (gather cancels siblings).
            raise
        except Exception:
            raw = None

        if isinstance(raw, list) and len(raw) == len(chunk):
            return [_as_target(x) for x in raw]

        # Misaligned but successful response — split to keep the rest aligned.
        if len(chunk) > 1:
            mid = len(chunk) // 2
            left, right = await asyncio.gather(
                _translate_chunk(chunk[:mid]), _translate_chunk(chunk[mid:])
            )
            return left + right

        # Single sentence that wouldn't translate cleanly — one focused retry.
        try:
            async with sem:
                single = await asyncio.to_thread(lambda: _parse_json(_call(
                    f"Translate this sentence into natural {name} (native script only, "
                    f'no romanisation). Return ONLY JSON: {{"target": "..."}}\n\n'
                    f"Sentence: {chunk[0]}", api_key, model)))
            return [_as_target((single or {}).get("target", ""))]
        except APIError:
            raise
        except Exception:
            return [""]

    chunks = [src_sents[i:i + batch_size] for i in range(0, len(src_sents), batch_size)]
    results = await asyncio.gather(*[_translate_chunk(c) for c in chunks])
    targets = [t for r in results for t in r]  # positionally aligned with src_sents

    segments = [
        {"target": tgt or src, "english": src}
        for src, tgt in zip(src_sents, targets)
    ]
    if not any(seg["target"] for seg in segments):
        raise ValueError("Translation produced no sentences")
    logger.info(
        "article import (%s): %d source sentences → %d translated",
        target_lang, len(src_sents), len(segments),
    )
    return {"segments": segments}


async def simplify_article(
    source_text: str,
    target_lang: str,
    difficulty: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    batch_size: int = 20,
) -> dict:
    """Rewrite text that is ALREADY in the target language at a given CEFR level.

    Same sentence-by-sentence alignment contract as translate_article_to_reading:
    returns ``{segments: [{target, english}]}`` where ``english`` is the ORIGINAL
    sentence and ``target`` is the simplified version.  If no simplification is
    needed (C2 or unrecognised level) it passes through verbatim.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    diff_instr = _DIFFICULTY_LEVEL.get(difficulty, "")
    if not diff_instr:
        src_sents = _split_source_sentences((source_text or "")[:12_000])
        return {"segments": [{"target": s, "english": ""} for s in src_sents]}

    _roman_kw = ("romanis", "transliterat", "pinyin", "jyutping", "romaja", "iast")
    rules = "\n".join(
        line for line in info["rules"].splitlines()
        if not any(kw in line.lower() for kw in _roman_kw)
    )

    src_sents = _split_source_sentences((source_text or "")[:12_000])
    if not src_sents:
        raise ValueError("No source text to simplify")

    sem = asyncio.Semaphore(3)

    def _as_target(x) -> str:
        if isinstance(x, str):
            s = x.strip().replace("\n", " ")
        elif isinstance(x, dict):
            s = str(x.get("target") or x.get("text") or "").strip().replace("\n", " ")
        else:
            return ""
        return re.sub(r'\*{1,2}', '', s)

    async def _simplify_chunk(chunk: list[str]) -> list[str]:
        numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(chunk))
        prompt = (
            f"Rewrite each numbered {name} sentence below at a simpler level.\n"
            f"Keep the MEANING of each sentence but adapt the vocabulary and grammar:\n"
            f"{diff_instr}\n"
            "Do NOT merge, split, or omit sentences. Output exactly ONE rewritten "
            "sentence per input, IN THE SAME ORDER.\n"
            "Native script only (no romanisation).\n"
            "No **bold** or other inline formatting.\n"
            "If a sentence starts with ## or ### (a heading marker), keep the "
            "marker prefix and simplify only the text after it.\n"
            f"Rules:\n{rules}\n"
            'Return ONLY a JSON array of strings, same length and order as input.\n\n'
            f"Sentences:\n{numbered}"
        )
        try:
            async with sem:
                raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
        except APIError:
            raise  # rate-limit / overload — don't fan out (see _translate_chunk)
        except Exception:
            raw = None
        if isinstance(raw, list) and len(raw) == len(chunk):
            return [_as_target(x) for x in raw]
        if len(chunk) > 1:
            mid = len(chunk) // 2
            left, right = await asyncio.gather(
                _simplify_chunk(chunk[:mid]), _simplify_chunk(chunk[mid:])
            )
            return left + right
        try:
            async with sem:
                single = await asyncio.to_thread(lambda: _parse_json(_call(
                    f"Rewrite this {name} sentence at a simpler level ({difficulty}). "
                    f"Keep the meaning. Native script only.\n"
                    f'Return ONLY JSON: {{"target": "..."}}\n\n'
                    f"Sentence: {chunk[0]}", api_key, model)))
            return [_as_target((single or {}).get("target", ""))]
        except APIError:
            raise
        except Exception:
            return [chunk[0]]

    chunks = [src_sents[i:i + batch_size] for i in range(0, len(src_sents), batch_size)]
    results = await asyncio.gather(*[_simplify_chunk(c) for c in chunks])
    targets = [t for r in results for t in r]

    segments = [
        {"target": tgt or src, "english": src}
        for src, tgt in zip(src_sents, targets)
    ]
    logger.info(
        "article simplify (%s, %s): %d sentences",
        target_lang, difficulty, len(segments),
    )
    return {"segments": segments}



def triage_feedback(
    title: str, description: str, existing_groups: list[str],
    api_key: str,
) -> dict:
    """AI-triage a feedback report: assign priority, group, and a suggested prompt.

    Returns: { priority, triage_group, triage_summary, suggested_prompt }
    """
    groups_block = ""
    if existing_groups:
        groups_str = ", ".join(f'"{g}"' for g in existing_groups[:50])
        groups_block = (
            f"\nExisting bug groups: [{groups_str}]\n"
            "If this report matches one of these groups, use that EXACT group name. "
            "Otherwise create a new short kebab-case group name.\n"
        )

    prompt = (
        "You are a software triage bot for a language-learning flashcard app. "
        "Analyze this user report and respond with JSON.\n\n"
        f"Title: {title}\n"
        f"Description: {description}\n"
        f"{groups_block}\n"
        "Respond with ONLY valid JSON:\n"
        "{\n"
        '  "priority": "low|medium|high|critical",\n'
        '  "triage_group": "short-kebab-case-group-name",\n'
        '  "triage_summary": "One sentence summary of the issue for developers",\n'
        '  "suggested_prompt": "A detailed prompt for Claude Code to implement the fix or feature. '
        "Include what files likely need changing and what the expected behavior should be.\"\n"
        "}\n"
    )
    try:
        raw = _call(prompt, api_key)
        data = _parse_json(raw)
        valid_priorities = {"low", "medium", "high", "critical"}
        priority = data.get("priority", "medium")
        if priority not in valid_priorities:
            priority = "medium"
        return {
            "priority": priority,
            "triage_group": (data.get("triage_group") or "uncategorized").strip()[:100],
            "triage_summary": (data.get("triage_summary") or "").strip()[:500],
            "suggested_prompt": (data.get("suggested_prompt") or "").strip()[:2000],
        }
    except Exception:
        return {
            "priority": "medium",
            "triage_group": "uncategorized",
            "triage_summary": title,
            "suggested_prompt": "",
        }


def suggest_vocab_for_label(
    label_name: str,
    target_lang: str,
    known_words: list[str],
    api_key: str,
    count: int = 10,
    label_words: list[dict] | None = None,
) -> list[dict]:
    """Ask an LLM to suggest vocabulary words that fit a label/category.

    `label_words` is the vocab already tagged with this label
    ([{target_text, source_text}]) — passed so the model matches its style/
    granularity and never re-suggests what's already there.

    Returns: [{"target": "...", "source": "...", "romanization": "..."}]
    """
    if target_lang not in LANG_INFO:
        return []
    info = LANG_INFO[target_lang]
    lang_name = info.get("full_name", info["name"])
    rom_name = info.get("romanization", "")
    rom_field = f', "romanization": "({rom_name})"' if rom_name else ""

    label_block = ""
    if label_words:
        items = ", ".join(
            f"{w['target_text']} ({w['source_text']})" if w.get("source_text") else w["target_text"]
            for w in label_words[:40]
        )
        label_block = (
            f'\nThe "{label_name}" category already contains these words — match '
            f"their style and specificity, and do NOT repeat any of them: {items}\n"
        )

    known_block = ""
    if known_words:
        sample = known_words[:50]
        known_block = (
            f"\nThe learner already knows these words elsewhere in their deck "
            f"(do NOT repeat them): {', '.join(sample)}\n"
        )

    prompt = (
        f"You are a {lang_name} vocabulary tutor. Suggest exactly {count} "
        f'{lang_name} vocabulary words/phrases that belong to the category '
        f'"{label_name}".\n'
        f"{label_block}"
        f"{known_block}\n"
        f"Return ONLY a JSON array of exactly {count} items. Each item:\n"
        f'{{"target": "({lang_name} word)"{rom_field}, "source": "(English meaning)"}}\n'
        f"Pick common, practical words a language learner should know. "
        f"Mix difficulty levels (beginner to intermediate). No duplicates. "
        f"You MUST return {count} items — never return an empty array.\n"
    )
    for _attempt in range(2):
        try:
            raw = _call(prompt, api_key, DEFAULT_MODEL)
            data = _parse_json(raw)
            if not isinstance(data, list):
                logger.warning("suggest_vocab %r attempt %d: parsed non-list type=%s",
                               label_name, _attempt, type(data).__name__)
                continue
            results = []
            for item in data[:count]:
                if not isinstance(item, dict):
                    continue
                target = (item.get("target") or "").strip()
                source = (item.get("source") or "").strip()
                if target and source:
                    results.append({
                        "target": target,
                        "source": source,
                        "romanization": (item.get("romanization") or "").strip(),
                    })
            logger.info("suggest_vocab %r attempt %d: %d results from %d items",
                        label_name, _attempt, len(results), len(data))
            if results:
                return results
        except Exception as exc:
            logger.warning("suggest_vocab %r attempt %d failed: %s", label_name, _attempt, exc)
    return []


def review_label_merge(
    label_names: list[str],
    api_key: str,
) -> dict:
    """Ask a cheap LLM whether a proposed label merge makes sense.

    Returns: { "verdict": "merge"|"reject", "suggested_name": "...", "reason": "..." }
    """
    names_str = ", ".join(f'"{n}"' for n in label_names)
    prompt = (
        "You are organising labels/tags for a language-learning flashcard deck. "
        "Someone proposed merging these labels into one:\n\n"
        f"Labels: [{names_str}]\n\n"
        "Decide:\n"
        "1. Do these labels genuinely refer to the SAME category/concept? "
        "Labels that merely share a common word (e.g. 'expressing quantity' vs "
        "'expressing emotions') are DIFFERENT categories — reject those.\n"
        "2. If they should merge, suggest the best single label name (concise, "
        "lowercase, no emoji). Prefer the most specific or commonly used form.\n\n"
        "Respond with ONLY valid JSON:\n"
        '{"verdict":"merge" or "reject", "suggested_name":"the merged label name (empty if reject)", "reason":"one short sentence"}\n'
    )
    try:
        raw = _call(prompt, api_key, DEFAULT_MODEL)
        data = _parse_json(raw)
        verdict = data.get("verdict", "reject")
        if verdict not in ("merge", "reject"):
            verdict = "reject"
        return {
            "verdict": verdict,
            "suggested_name": (data.get("suggested_name") or "").strip()[:100],
            "reason": (data.get("reason") or "").strip()[:200],
        }
    except Exception:
        return {"verdict": "merge", "suggested_name": label_names[0], "reason": ""}


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
    name = info.get("full_name", info["name"])
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


async def regenerate_card_fields(
    target_text: str, source_text: str, target_lang: str, *,
    api_key: str, model: str = DEFAULT_MODEL,
) -> dict:
    """Re-author the AI-generated fields for an EXISTING card (the 🔄 refresh
    button on the flashcard editor). Returns {notes, romanization}. The note is a
    fresh learner-insight note from the model; romanization is always recomputed by
    the offline oracle (never the model) so it stays consistent with the ruby."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    rules = info.get("rules", "")
    rules_block = f"Language notes:\n{rules}\n" if rules else ""

    prompt = (
        f"You are helping a learner of {name}. Write a fresh, concise learner's NOTE for "
        f"this flashcard.\n"
        f"{rules_block}"
        f'{name} word/phrase: {target_text}\n'
        f'English meaning: {source_text}\n\n'
        f"NOTES guidance: 1–2 sentences of genuine insight. Do NOT restate what the meaning "
        f"already makes obvious. Focus on register/formality, common collocations, pitfalls for "
        f"English speakers, cultural context, or a genuine etymology/mnemonic hook. Use an empty "
        f"string if there is truly nothing non-obvious to add.\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{ "notes": "<your note, or empty string>" }'
    )
    notes = ""
    try:
        raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
        if isinstance(raw, dict):
            notes = (raw.get("notes") or "").strip()
    except (ValueError, TypeError):
        notes = ""

    import tokenizer
    romanization = tokenizer.romanize_text(target_text, target_lang) or ""
    return {"notes": notes, "romanization": romanization}


async def translate_message(text: str, source_lang: str, target_lang: str, *, api_key: str) -> dict:
    """Translate a chat message and generate a brief nuance note.
    Returns {translated, nuance_note, reply_en, explanation, vocab}.
    When source is English, also returns an explanation of the translation and
    a vocab list of key words the learner can add to their deck."""
    empty = {"translated": text, "nuance_note": "", "reply_en": text,
             "explanation": "", "vocab": []}
    if not text.strip():
        return empty
    src_name = LANG_INFO.get(source_lang, {}).get("name", source_lang) if source_lang != "en" else "English"
    tgt_name = LANG_INFO.get(target_lang, {}).get("name", target_lang) if target_lang != "en" else "English"
    rules = LANG_INFO.get(target_lang, {}).get("rules", "")
    rules_block = (
        f"\nLanguage rules:\n{rules}\n"
        "CRITICAL: the translated text must contain ONLY native script — "
        "never embed romanization, jyutping, pinyin, or pronunciation in the output."
    ) if rules else ""
    reply_en = text if source_lang == "en" else None
    from_english = source_lang == "en"

    if source_lang == target_lang:
        return {**empty, "translated": text, "reply_en": reply_en or text}

    vocab_block = ""
    vocab_json = ""
    if from_english:
        vocab_block = (
            "Also return:\n"
            f"- `explanation`: 1–2 sentences in English explaining HOW the {tgt_name} "
            "translation works (word order, grammar patterns, any idiom used).\n"
            f"- `vocab`: array of the key {tgt_name} content words/phrases in the "
            "translation. Each item: {\"target_text\": \"<native>\", \"english\": "
            "\"<short gloss>\"}. Include 2–6 items — the words worth learning. "
            "Skip function words / particles unless they carry important meaning.\n"
        )
        vocab_json = ', "explanation": "...", "vocab": [{"target_text": "...", "english": "..."}]'

    prompt = (
        f"Translate this {src_name} chat message to natural, conversational {tgt_name} "
        f"— the kind you'd text a friend.{rules_block}\n"
        "Also write a brief English nuance note (1 sentence max) explaining tone, register, "
        "idiom, or cultural meaning lost or changed in translation — empty string if nothing significant.\n"
        f"{vocab_block}"
        "Return ONLY valid JSON, nothing else:\n"
        f'{{"translated": "...", "nuance_note": "..."{vocab_json}}}\n\n'
        f"Message: {text}"
    )
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(None, lambda: _call(prompt, api_key, DEFAULT_MODEL))
    failed = False
    try:
        data = _parse_json(raw)
        translated = data.get("translated", text).strip()
        nuance_note = data.get("nuance_note", "").strip()
    except Exception:
        logger.warning("translate_message: failed to parse LLM response, falling back to original text")
        translated = text
        nuance_note = ""
        data = {}
        failed = True
    if not translated or translated.startswith("{") or translated.startswith("["):
        translated = text
        failed = True
    if reply_en is None:
        reply_en = text

    explanation = ""
    vocab: list[dict] = []
    if from_english:
        explanation = str(data.get("explanation", "")).strip()
        import tokenizer
        for v in (data.get("vocab") or [])[:6]:
            if not isinstance(v, dict):
                continue
            t = (v.get("target_text") or "").strip()
            e = (v.get("english") or "").strip()
            if t and e:
                vocab.append({
                    "target_text": t,
                    "english": e,
                    "romanization": tokenizer.romanize_text(t, target_lang) or "",
                })

    result = {"translated": translated, "nuance_note": nuance_note, "reply_en": reply_en,
              "explanation": explanation, "vocab": vocab}
    if failed:
        result["translation_failed"] = True
    return result


async def analyze_message(text: str, lang: str, *, api_key: str) -> dict:
    """Grammar-check a message written in `lang` + translate it to English.
    Returns {corrections: [...], reply_en: str}
    corrections items: {original, corrected, explanation, construction}"""
    if not text.strip():
        return {"corrections": [], "reply_en": text}
    _info = LANG_INFO.get(lang, {})
    lang_name = _info.get("full_name", _info.get("name", lang))
    rules = LANG_INFO.get(lang, {}).get("rules", "")
    rules_block = (
        f"\nLanguage rules:\n{rules}\n"
        "CRITICAL: all target-language text in your JSON must be native script only — "
        "never embed romanization, jyutping, or pinyin in the output fields."
    ) if rules else ""
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


async def suggest_labels(
    source_text: str, target_text: str, target_lang: str, *,
    api_key: str, model: str = DEFAULT_MODEL,
) -> list[str]:
    """Generate 2–4 short organisational labels for a single vocab card.

    Standalone version of the `suggested_labels` field baked into translate(), for
    cards added through paths that don't run a full translation (tutor chips, lesson
    "add to deck", atomized words). Cheap single call; returns [] on any failure."""
    name = LANG_INFO.get(target_lang, {}).get("name", target_lang)
    prompt = (
        f'Suggest 2–4 short English labels (lowercase) to help a learner of {name} organise '
        f'this vocabulary card in their deck. Include: (1) part of speech (e.g. "verb", "noun", '
        f'"adjective", "adverb"); (2) grammatical function when relevant (e.g. "irregular verb", '
        f'"modal verb", "reflexive verb", "negation"); (3) topic labels only when genuinely useful '
        f'(nested specificity is fine, e.g. both "food" and "vegetable"). Do NOT include near-synonym '
        f'labels for the same concept. Every label must add distinct organisational value.\n'
        f'Return ONLY a JSON array of strings, e.g. ["noun", "food"].\n\n'
        f'{name} word: {target_text}\nEnglish meaning: {source_text}'
    )
    try:
        raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
    except Exception:
        return []
    items = raw if isinstance(raw, list) else (raw.get("suggested_labels") or raw.get("labels")) if isinstance(raw, dict) else []
    if not isinstance(items, list):
        return []
    return [l.strip().lower() for l in items if isinstance(l, str) and l.strip()][:4]
