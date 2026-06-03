"""AI Learning Path — curriculum + lesson generation (IDEAS item 43).

Two-layer generation:
  1. generate_curriculum() — the syllabus skeleton (units + lessons + objectives
     + the concepts each lesson introduces). Generated once, regeneratable.
  2. (later) generate_lesson() — the actual exercises for one lesson.

Curriculum generation is CEFR-scaffolded: CEFR_SYLLABUS gives a language-agnostic
can-do/topic checklist per level; the model orders/adapts/fills it for the target
language. This keeps an AI course coherent and reliable across languages.
"""
import asyncio

from translation import LANG_INFO, DEFAULT_MODEL, _call, _parse_json

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

# Language-agnostic can-do / topic checklist per CEFR level. These are the
# guardrails the curriculum generator organises into units + lessons; the model
# adapts ordering and detail to the specific language (e.g. tones + measure words
# for Cantonese, gender + verb conjugation for French). Start with A1/A2.
#
# NOTE: the writing/sound SYSTEM itself (script + tones/pronunciation) is handled
# by a separate, curated "Foundations" module keyed on script_family — NOT here.
# That content is finite/factual (a fixed inventory of graphemes/tones), shared
# across users, and best derived from our romanizers rather than AI-generated.
# This syllabus assumes the learner can already read the script and focuses on
# vocabulary, grammar, and communication.
CEFR_SYLLABUS = {
    "A1": [
        "Greetings, goodbyes, and basic courtesy (please, thank you, sorry, excuse me)",
        "Introducing yourself and asking someone's name",
        "Personal information: nationality, where you live, age",
        "Numbers 0–100; giving your age and phone number",
        "Core function words: the verbs 'to be' and 'to have'; basic pronouns",
        "Articles / noun gender, or classifiers & measure words (language-dependent)",
        "Family members and relationships",
        "Days, months, telling the time, and dates",
        "Colours and basic descriptive adjectives",
        "Food and drink; ordering in a café or restaurant",
        "Shopping basics: prices, money, and simple transactions",
        "Common everyday verbs (to go, to do/make, to want, to like)",
        "Expressing likes and dislikes",
        "Daily routine and the simple present tense (or its equivalent)",
        "Places in town and simple directions",
        "The weather and seasons",
        "Asking and answering basic questions (what, where, when, who, how much)",
    ],
    "A2": [
        "Talking about the past (completed actions / past tense or aspect)",
        "Talking about future plans and intentions",
        "Daily routines in more detail; frequency adverbs",
        "Describing people's appearance and personality",
        "Health, the body, and a visit to the doctor or pharmacy",
        "Travel and transport: buying tickets, asking for information",
        "Hobbies, free time, and sports",
        "Shopping for clothes; sizes and preferences",
        "Making comparisons (bigger, better, the most…)",
        "Invitations, suggestions, and arranging to meet",
        "Describing your home, town, and neighbourhood",
        "Work and studies; describing your job or daily occupation",
        "Telephoning and simple messages",
        "Expressing obligation, permission, and ability (must, can, may)",
    ],
}


def _known_block(known_summary: str | None) -> str:
    known_summary = (known_summary or "").strip()
    if not known_summary:
        return (
            "The learner is starting this language from scratch — assume no prior "
            "knowledge and begin with the absolute basics."
        )
    return (
        "The learner already knows the following (do NOT teach these as new; you "
        "may briefly recycle them). Skip or compress lessons whose content they "
        "already know:\n"
        f"{known_summary}"
    )


def _build_curriculum_prompt(target_lang: str, level: str, known_summary: str | None) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    script = info["script"]
    rom = info["romanization"]
    rules = info["rules"]
    checklist = CEFR_SYLLABUS.get(level, CEFR_SYLLABUS["A1"])
    checklist_str = "\n".join(f"- {c}" for c in checklist)
    rom_note = (
        f"Romanisation scheme for examples: {rom}.\n" if rom else
        "This language uses the Latin alphabet; no romanisation needed.\n"
    )

    return (
        f"You are an expert language-curriculum designer. Design a CEFR {level} "
        f"course that teaches {name} from the ground up to an English-speaking learner.\n\n"
        f"Target language: {name}\n"
        f"Writing system: {script}\n"
        f"{rom_note}"
        f"Language-specific notes:\n{rules}\n\n"
        f"Use this CEFR {level} can-do / topic checklist as your backbone. Cover all of "
        f"it, but ORDER, GROUP, and ADAPT it for {name} specifically — front-load what is "
        f"foundational for THIS language (e.g. tones & measure words, or gender & verb "
        f"conjugation), and fold in features the checklist doesn't name but {name} needs:\n"
        f"{checklist_str}\n\n"
        f"{_known_block(known_summary)}\n\n"
        "Design rules:\n"
        "- Organise into 6–8 UNITS, each a coherent theme with a one-line objective.\n"
        "- Each unit has 3–5 LESSONS. Each lesson is small: it introduces about 5–8 "
        "NEW concepts (vocabulary items and/or one grammar point) and reuses earlier ones.\n"
        "- Progress strictly from foundational to more complex; never use a grammar "
        "point or word in a lesson before it has been introduced.\n"
        "- For each lesson, list the new concepts. A concept is either:\n"
        '    {"kind":"vocab","key":"<stable snake_case english key>","label":"<word/phrase in '
        + name + '>","gloss":"<English meaning>"}\n'
        '    {"kind":"grammar","key":"<stable snake_case key>","label":"<short grammar point name>","gloss":"<one-line explanation>"}\n'
        "- Keys must be stable and canonical (e.g. \"greeting_hello\", \"number_1_10\", "
        "\"gender_definite_articles\", \"measure_word_go3\") so they can be tracked across lessons.\n"
        "- Give each unit and lesson a short, learner-friendly English title.\n\n"
        "Return ONLY valid JSON in exactly this shape, no other text:\n"
        "{\n"
        '  "level": "' + level + '",\n'
        '  "language": "' + name + '",\n'
        '  "units": [\n'
        "    {\n"
        '      "title": "...",\n'
        '      "theme": "...",\n'
        '      "objective": "...",\n'
        '      "lessons": [\n'
        "        {\n"
        '          "title": "...",\n'
        '          "objective": "...",\n'
        '          "new_concepts": [ {"kind":"...","key":"...","label":"...","gloss":"..."} ]\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n"
    )


async def generate_curriculum(
    target_lang: str,
    level: str = "A1",
    known_summary: str | None = None,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Generate a CEFR-scaffolded course skeleton (units → lessons → concepts)."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    if level not in LEVELS:
        raise ValueError(f"Unsupported level: {level}")
    prompt = _build_curriculum_prompt(target_lang, level, known_summary)
    raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
    return raw
