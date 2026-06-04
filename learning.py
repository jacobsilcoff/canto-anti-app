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
# Exercise-type registry: each type is a self-describing JSON object. To add a
# new type you (1) add its schema here so the generator can emit it, (2) add a
# renderer + grader in the frontend (static/learn.html EXERCISE_TYPES), and
# (3) list which of its fields hold target-language text in _ROMANIZE_FIELDS so
# romanisation hints get attached. Nothing else needs to change.
EXERCISE_TYPES = ("choice", "word_bank", "listening", "match")

_EXERCISE_CONTRACT = """
Available exercise types (emit a mix, ~8 exercises total, all focused on THIS
lesson's new concepts with a few earlier ones recycled as distractors):

1. "choice" — multiple choice (4 options, exactly one correct).
   { "type":"choice", "concept_key":"<key>", "instruction":"<generic instruction>",
     "prompt":"<the single stimulus word/phrase>", "prompt_lang":"target"|"english",
     "audio":"<target text to read aloud, or empty string>",
     "options":["..","..","..",".."], "answer": <0-based index of correct option> }
   - Recognition: "prompt" is a TARGET word (prompt_lang "target"); options are English meanings.
   - Production: "prompt" is an ENGLISH word (prompt_lang "english"); options are TARGET words.
   - Use both directions across the lesson.
   - "instruction" MUST be generic (e.g. "What does this mean?" / "How do you say this?")
     and must NOT contain the answer or repeat the prompt word.

2. "word_bank" — assemble the target sentence from word tiles.
   { "type":"word_bank", "concept_key":"<key>", "instruction":"Translate: <English sentence>",
     "audio":"<the full target sentence>",
     "answer_tokens":["<target token>", ...],     // correct order, one tile each
     "distractor_tokens":["<plausible wrong tile>", ...] }   // 1-3 extra tiles

3. "listening" — hear target audio, pick what was said (options are TARGET text).
   { "type":"listening", "concept_key":"<key>", "instruction":"What did you hear?",
     "audio":"<target text to read aloud>",        // required, non-empty
     "options":["<target>","<target>","<target>"], "answer": <0-based index> }

4. "match" — match 3–5 target↔English pairs.
   { "type":"match", "instruction":"Match the pairs",
     "pairs":[ {"target":"<target>","english":"<English>"}, ... ] }

Rules:
- Only use vocabulary/grammar from this lesson's concepts or earlier ones (listed
  below). Never introduce unseen words.
- Test EACH new concept in at least two different exercises.
- Include 1–2 exercises that REVIEW earlier concepts (from the known list) so the
  learner keeps practising what they've already met.
- Keep target text natural and correct. Distractors must be plausible but clearly wrong.
""".strip()

# The teaching screen shown BEFORE the exercises.
_TEACH_CONTRACT = """
Also include a "teach" object that briefly TEACHES the new material before the
exercises (the learner reads this first):
{ "teach": {
    "intro": "<1-2 friendly sentences introducing this lesson's theme / grammar point>",
    "items": [ {"target":"<target word or phrase>", "gloss":"<English meaning>",
                "note":"<optional short usage or grammar note; empty string if none>"} ]
} }
Include one item per NEW vocabulary concept, and for each NEW grammar concept an
item whose "note" explains it simply in one sentence.
""".strip()

# Per-type list of fields whose values are target-language text and should get a
# parallel "<field>_roman" romanisation hint (computed by us, never the AI).
_ROMANIZE_PLANS = {
    "choice": [("prompt", "prompt_roman", "if_target"), ("options", "options_roman", "if_options_target")],
    "word_bank": [("answer_tokens", "answer_roman", "join")],
    "listening": [("audio", "audio_roman", "str"), ("options", "options_roman", "list")],
    "match": [("pairs", None, "pairs")],
}


def _attach_romanization(exercises: list[dict], lang: str) -> list[dict]:
    """Fill in romanisation hints for target-language text using our offline
    romanisers (jyutping/pinyin/IAST/romaja). No-op for Latin-script langs."""
    if not LANG_INFO.get(lang, {}).get("romanization"):
        return exercises
    R = tokenizer.romanize_text
    for ex in exercises:
        t = ex.get("type")
        if t == "choice":
            if ex.get("prompt_lang") == "target":
                ex["prompt_roman"] = R(ex.get("prompt", ""), lang)
            else:  # options are target text (production)
                ex["options_roman"] = [R(o, lang) for o in ex.get("options", [])]
        elif t == "word_bank":
            ex["answer_roman"] = R(" ".join(ex.get("answer_tokens", [])), lang)
        elif t == "listening":
            ex["audio_roman"] = R(ex.get("audio", ""), lang)
            ex["options_roman"] = [R(o, lang) for o in ex.get("options", [])]
        elif t == "match":
            for p in ex.get("pairs", []):
                p["target_roman"] = R(p.get("target", ""), lang)
    return exercises


def _build_lesson_prompt(target_lang: str, lesson: dict, prior_concepts: list[dict]) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    rules = info["rules"]
    concepts = lesson.get("concepts", [])
    new_block = "\n".join(
        f'- [{c.get("kind","vocab")}] key={c.get("key")} · {c.get("label","")} = {c.get("gloss","")}'
        for c in concepts
    ) or "(none)"
    prior_block = ", ".join(
        f'{c.get("label","")} ({c.get("gloss","")})' for c in prior_concepts[:80]
    ) or "(none yet)"

    return (
        f"You are writing one bite-size {name} lesson for an English-speaking learner.\n"
        f"Language notes:\n{rules}\n\n"
        f"Lesson: {lesson.get('title','')}\n"
        f"Objective: {lesson.get('objective','')}\n\n"
        f"NEW concepts this lesson must teach and test (use these keys):\n{new_block}\n\n"
        f"Earlier concepts already known (recycle as distractors; do not re-teach):\n{prior_block}\n\n"
        f"{_TEACH_CONTRACT}\n\n"
        f"{_EXERCISE_CONTRACT}\n\n"
        "Return ONLY valid JSON: { \"teach\": {...}, \"exercises\": [ ... ] }"
    )


async def generate_lesson(
    target_lang: str,
    lesson: dict,
    prior_concepts: list[dict] | None = None,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Generate the exercises for one lesson. `lesson` = {title, objective,
    concepts:[{kind,key,label,gloss}]}. Returns {"exercises":[...]} with
    romanisation hints attached for languages that have a romaniser."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = _build_lesson_prompt(target_lang, lesson, prior_concepts or [])
    raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
    exercises = [e for e in (raw.get("exercises") or []) if isinstance(e, dict) and e.get("type") in EXERCISE_TYPES]
    exercises = _attach_romanization(exercises, target_lang)

    teach = raw.get("teach") if isinstance(raw.get("teach"), dict) else {}
    if teach and LANG_INFO[target_lang].get("romanization"):
        for it in teach.get("items", []):
            if isinstance(it, dict):
                it["target_roman"] = tokenizer.romanize_text(it.get("target", ""), target_lang)
    return {"teach": teach, "exercises": exercises}
