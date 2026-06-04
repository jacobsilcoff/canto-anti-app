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
import random

import tokenizer
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


def _next_level(level: str) -> str | None:
    """The CEFR level after `level`, or None if already at the top."""
    try:
        i = LEVELS.index(level)
    except ValueError:
        return None
    return LEVELS[i + 1] if i + 1 < len(LEVELS) else None


def _build_curriculum_prompt(target_lang: str, level: str, known_summary: str | None) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    script = info["script"]
    rom = info["romanization"]
    rules = info["rules"]
    checklist = CEFR_SYLLABUS.get(level)
    if checklist:
        backbone = (
            f"Use this CEFR {level} can-do / topic checklist as your backbone. Cover all of "
            f"it, but ORDER, GROUP, and ADAPT it for {name} specifically — front-load what is "
            f"foundational for THIS language (e.g. tones & measure words, or gender & verb "
            f"conjugation), and fold in features the checklist doesn't name but {name} needs:\n"
            + "\n".join(f"- {c}" for c in checklist)
        )
    else:
        backbone = (
            f"Cover the standard topics, vocabulary, and grammatical competencies expected at "
            f"CEFR {level} in {name}, progressing in difficulty from the start of the level to "
            f"the end. Build directly on what the learner already knows (below)."
        )
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
        f"{backbone}\n\n"
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


# ── Lesson content (exercises) ────────────────────────────────────────────────
#
# Exercise-type registry (frontend renderers in static/learn.html EXERCISE_TYPES).
# Generation builds these DETERMINISTICALLY from accurately-materialised concepts —
# the AI is only used to (a) translate glosses → target text and (b) write the
# teach intro / grammar notes. It never picks answers or builds option lists, so
# prompts, answers, and directions are always correct.
EXERCISE_TYPES = ("choice", "word_bank", "listening", "match")


def _build_materialize_prompt(target_lang: str, concepts: list[dict]) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    rules = info["rules"]
    lines = [
        f'- key="{c.get("key")}" kind={c.get("kind","vocab")} : {c.get("gloss","")}'
        for c in concepts
    ]
    items = "\n".join(lines)
    return (
        f"You are preparing one beginner {name} lesson for an English speaker.\n"
        f"Language notes:\n{rules}\n\n"
        f"For each item below, give the single most natural, correct everyday {name} "
        f"word or phrase for that meaning (citation/dictionary form, no romanisation).\n"
        f"IMPORTANT — teach the nuances, don't flatten them: when several items have "
        f"overlapping English meanings (e.g. two different ways to say 'thank you', or "
        f"formal vs informal 'you'), give each its correct DISTINCT target, and write a "
        f"short 'note' for EACH explaining exactly when to use it versus the other. These "
        f"distinctions are the whole point of the lesson.\n"
        f"Items:\n{items}\n\n"
        f"Also write:\n"
        f'- "intro": 1–2 friendly sentences introducing this lesson.\n'
        f'- "notes": a one-sentence plain-English usage note for ANY item that has a '
        f'useful nuance, is easily confused with another item, or is a grammar point '
        f'(keyed by its key). Empty object if truly none.\n\n'
        "Return ONLY valid JSON in this exact shape:\n"
        '{ "targets": { "<key>": "<target word/phrase>", ... },\n'
        '  "intro": "...",\n'
        '  "notes": { "<key>": "<one-sentence note>", ... } }'
    )


def _pick_options(correct: str, pool: list[str], n: int = 4) -> tuple[list[str], int]:
    """Build an options list = correct + up to n-1 distinct distractors, shuffled.
    Returns (options, index_of_correct)."""
    seen = {correct}
    distractors = []
    for p in pool:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            distractors.append(p)
    random.shuffle(distractors)
    opts = [correct] + distractors[: max(0, n - 1)]
    random.shuffle(opts)
    return opts, opts.index(correct)


def _build_exercises(items: list[dict], lang: str,
                     gloss_pool: list[str], target_pool: list[str]) -> list[dict]:
    """Deterministically build correct exercises from materialised items.
    Each item: {key, gloss, target, roman}. Guarantees right answers/directions."""
    R = tokenizer.romanize_text
    has_rom = bool(LANG_INFO.get(lang, {}).get("romanization"))
    recog, prod, listen = [], [], []

    for it in items:
        tgt, gl, tip = it["target"], it["gloss"], it.get("note", "")
        # Recognition: show target, choose the English meaning.
        opts, ans = _pick_options(gl, gloss_pool)
        recog.append({
            "type": "choice", "concept_key": it["key"], "instruction": "What does this mean?",
            "prompt": tgt, "prompt_lang": "target", "prompt_roman": it["roman"],
            "audio": tgt, "options": opts, "answer": ans, "tip": tip,
        })
        # Production: show English, choose the target word.
        topts, tans = _pick_options(tgt, target_pool)
        prod.append({
            "type": "choice", "concept_key": it["key"], "instruction": "How do you say this?",
            "prompt": gl, "prompt_lang": "english", "audio": "",
            "options": topts, "options_roman": [R(o, lang) if has_rom else "" for o in topts],
            "answer": tans, "tip": tip,
        })

    for it in items[:3]:
        lopts, lans = _pick_options(it["target"], target_pool)
        listen.append({
            "type": "listening", "concept_key": it["key"], "instruction": "What did you hear?",
            "audio": it["target"], "audio_roman": it["roman"],
            "options": lopts, "options_roman": [R(o, lang) if has_rom else "" for o in lopts],
            "answer": lans, "tip": it.get("note", ""),
        })

    exercises = recog + prod + listen
    random.shuffle(exercises)
    exercises = exercises[:12]

    if len(items) >= 3:
        sub = items[:5]
        exercises.append({
            "type": "match", "instruction": "Match the pairs",
            "pairs": [{"target": it["target"], "target_roman": it["roman"], "english": it["gloss"]} for it in sub],
        })
    return exercises


async def generate_lesson(
    target_lang: str,
    lesson: dict,
    prior_concepts: list[dict] | None = None,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Build a lesson: materialise each concept's accurate target text via one
    translation call, then construct exercises deterministically. `lesson` =
    {title, objective, concepts:[{kind,key,label,gloss}]}. Returns
    {"teach": {...}, "exercises": [...]}."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    concepts = lesson.get("concepts", []) or []
    prior_concepts = prior_concepts or []

    prompt = _build_materialize_prompt(target_lang, concepts)
    raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
    targets = raw.get("targets") or {}
    notes = raw.get("notes") or {}
    intro = (raw.get("intro") or "").strip()
    has_rom = bool(LANG_INFO[target_lang].get("romanization"))
    R = tokenizer.romanize_text

    # Materialised items that have a target (skip concepts we couldn't translate).
    # Keep the FULL contextual gloss ("thank you (for a gift)") so near-synonyms
    # stay distinct and become teaching distractors rather than ambiguous answers.
    items, teach_items = [], []
    for c in concepts:
        key, gloss = c.get("key"), (c.get("gloss") or "").strip()
        target = (targets.get(key) or "").strip()
        note = (notes.get(key) or "").strip()
        roman = R(target, target_lang) if (has_rom and target) else ""
        if target:
            items.append({"key": key, "gloss": gloss, "target": target, "roman": roman, "note": note})
        teach_items.append({"target": target, "target_roman": roman, "gloss": gloss, "note": note})

    gloss_pool = [it["gloss"] for it in items] + [(c.get("gloss") or "").strip() for c in prior_concepts]
    target_pool = [it["target"] for it in items] + [(c.get("label") or "").strip() for c in prior_concepts]

    exercises = _build_exercises(items, target_lang, gloss_pool, target_pool) if items else []
    teach = {"intro": intro, "items": teach_items}
    return {"teach": teach, "exercises": exercises}
