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
import re

import grammar
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
        "- This course teaches VOCABULARY, GRAMMAR, and COMMUNICATION only. Do NOT create "
        "any unit, lesson, or concept that teaches the writing system, alphabet, tones, "
        "pinyin/jyutping/romanisation, or pronunciation drills — assume the learner can "
        "already read and pronounce the script (that is handled separately). E.g. never make "
        "concepts like \"high level tone\" or \"the letter é\".\n"
        "- Organise into 6–8 UNITS, each a coherent theme with a one-line objective.\n"
        "- EVERY LESSON MUST BE COHERENT, like a single chapter of a textbook: its "
        "title, objective, and concepts must all describe the SAME thing. Never mix "
        "unrelated material into a lesson (e.g. do NOT put verb conjugation or noun "
        "gender inside a lesson titled \"Hello & Goodbye\"). The title must accurately "
        "summarise what is actually taught.\n"
        "- Each lesson picks the organising principle that fits its focus:\n"
        "  • a COMMUNICATIVE topic (greetings, ordering food, directions) — teach that "
        "topic deeply, including its OWN nuances and usage rules (e.g. for greetings: "
        "bonjour vs salut register, bonsoir/time-of-day, tu vs vous); OR\n"
        "  • a GRAMMAR point (present tense, gender of nouns, negation, word order) — a "
        "focused grammar chapter, with just enough common vocabulary to illustrate it.\n"
        "- Favour DEPTH over breadth: a small, focused lesson (~4–7 related concepts) "
        "that fully teaches one thing beats a grab-bag. Reuse earlier vocab/grammar to "
        "build examples rather than piling on new words. Spend more lessons on what "
        "English speakers find UN-intuitive (gender/agreement, aspect/tense, "
        "classifiers, word order, honorifics) and breeze past what transfers.\n"
        "- Progress strictly from foundational to more complex; never use a grammar "
        "point or word in a lesson before it has been introduced.\n"
        "- For each lesson, list the new concepts. A concept is either:\n"
        '    {"kind":"vocab","key":"<stable snake_case english key>","label":"<word/phrase in '
        + name + '>","gloss":"<English meaning>"}\n'
        '    {"kind":"grammar","key":"<stable snake_case key>","label":"<short grammar point name>","gloss":"<one-line explanation>"}\n'
        "- Keys must be stable and canonical (e.g. \"greeting_hello\", \"number_1_10\", "
        "\"gender_definite_articles\", \"measure_word_go3\") so they can be tracked across lessons.\n"
        "- For verb conjugation, make ONE \"grammar\" concept per verb or pattern "
        "(e.g. key \"verb_present_avoir\", label the INFINITIVE \"avoir\", gloss "
        "\"the verb avoir (to have) — present tense\"), or a pattern like \"present tense "
        "of regular -er verbs\". Do NOT create a separate concept for each conjugated form "
        "(ai, as, a, avons…) — the app drills the full conjugation automatically. "
        "Mark every structural item (verb skills, articles, agreement, particles) as kind \"grammar\".\n"
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


def _prior_lessons_block(lesson_num: int, prior_summaries: list[dict]) -> str:
    """Build the numbered prior-lessons context block for the materialize prompt."""
    if prior_summaries:
        lines = "\n".join(
            f'  Lesson {s["lesson_num"]} ({s["title"]}): {s["summary"]}'
            for s in prior_summaries
        )
        history = f"Prior lessons already covered (do NOT re-teach these as new):\n{lines}\n"
    else:
        history = (
            "No prior vocabulary lessons yet — but the learner may have completed a "
            "foundations track covering the writing system and sounds.\n"
        )
    return f"{history}This is Lesson {lesson_num}."


def _build_materialize_prompt(
    target_lang: str,
    concepts: list[dict],
    lesson_num: int = 1,
    prior_summaries: list[dict] | None = None,
) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    rules = info["rules"]
    lines = [
        f'- key="{c.get("key")}" kind={c.get("kind","vocab")} : {c.get("gloss","")}'
        for c in concepts
    ]
    items = "\n".join(lines)
    context_block = _prior_lessons_block(lesson_num, prior_summaries or [])
    return (
        f"You are preparing one {name} lesson for an English speaker.\n"
        f"Language notes:\n{rules}\n\n"
        f"{context_block}\n\n"
        f"For each item below, give the single most natural, correct everyday {name} "
        f"word or phrase for that meaning (citation/dictionary form, no romanisation).\n"
        f"IMPORTANT — teach the nuances, don't flatten them: when several items have "
        f"overlapping English meanings (e.g. two different ways to say 'thank you', or "
        f"formal vs informal 'you'), give each its correct DISTINCT target, and write a "
        f"short 'note' for EACH explaining exactly when to use it versus the other. These "
        f"distinctions are the whole point of the lesson.\n"
        f"Items:\n{items}\n\n"
        f"Also write:\n"
        f'- "intro": 1–2 sentences in ENGLISH introducing the specific topic of THIS '
        f'lesson. Be specific to what is being taught — do NOT write a generic welcome, '
        f'do NOT say "Welcome to your first lesson", do NOT assume this is the '
        f'learner\'s first experience. You MAY reference earlier lessons by number '
        f'(e.g. "Building on Lesson 2…") if genuinely relevant. '
        f'No {name} words or romanisation in the intro.\n'
        f'- "notes": a one-sentence plain-English usage note for ANY item that has a '
        f'useful nuance, is easily confused with another item, or is a grammar point '
        f'(keyed by its key). English only — no {name} words or romanisation in notes. '
        f'Empty object if truly none.\n'
        f'- "summary": one sentence (≤ 30 words) naming the specific concepts taught in '
        f'this lesson — English glosses, key distinctions. This is stored so future '
        f'lessons know what Lesson {lesson_num} covered. Be specific: list the items, '
        f'not just the topic. Example format: '
        f'"Covered hello/goodbye, thank-you (formal vs informal), and sorry."\n\n'
        "Return ONLY valid JSON in this exact shape:\n"
        '{ "targets": { "<key>": "<target word/phrase>", ... },\n'
        '  "intro": "...",\n'
        '  "notes": { "<key>": "<one-sentence note>", ... },\n'
        '  "summary": "..." }'
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


# Lessons are split into SEGMENTS: teach a few concepts, practise them, teach a
# few more, practise (consolidating the new + refreshing earlier ones).
_SEGMENT_SIZE = 3      # concepts taught per segment
_SEG_BUDGET = 8        # max exercises per segment


def _recognition_ex(it: dict, gloss_pool: list[str]) -> dict:
    opts, ans = _pick_options(it["gloss"], gloss_pool)
    return {
        "type": "choice", "concept_key": it["key"], "instruction": "What does this mean?",
        "prompt": it["target"], "prompt_lang": "target", "prompt_roman": it["roman"],
        "audio": it["target"], "options": opts, "answer": ans, "tip": it.get("note", ""),
    }


def _production_ex(it: dict, target_pool: list[str], lang: str) -> dict:
    R = tokenizer.romanize_text
    opts, ans = _pick_options(it["target"], target_pool)
    return {
        "type": "choice", "concept_key": it["key"], "instruction": "How do you say this?",
        "prompt": it["gloss"], "prompt_lang": "english", "audio": "",
        "options": opts, "options_roman": [R(o, lang) for o in opts],
        "answer": ans, "tip": it.get("note", ""),
    }


def _listening_ex(it: dict, target_pool: list[str], lang: str) -> dict:
    R = tokenizer.romanize_text
    opts, ans = _pick_options(it["target"], target_pool)
    return {
        "type": "listening", "concept_key": it["key"], "instruction": "What did you hear?",
        "audio": it["target"], "audio_roman": it["roman"],
        "options": opts, "options_roman": [R(o, lang) for o in opts],
        "answer": ans, "tip": it.get("note", ""),
    }


def _match_ex(items: list[dict]) -> dict:
    return {
        "type": "match", "instruction": "Match the pairs",
        "pairs": [{"target": it["target"], "target_roman": it["roman"], "english": it["gloss"]} for it in items[:5]],
    }


def _segment_exercises(group: list[dict], lang: str, gloss_pool: list[str],
                       target_pool: list[str], refresh_items: list[dict]) -> list[dict]:
    """Build one segment's exercises: consolidate the just-taught `group`
    (recognition + production each, a listening, a match) plus 1–2 recognition
    exercises REFRESHING earlier material from this lesson."""
    recog = [_recognition_ex(it, gloss_pool) for it in group]   # 1× each — coverage
    extras = [_production_ex(it, target_pool, lang) for it in group]   # → 2× each
    if refresh_items:
        for it in random.sample(refresh_items, min(2, len(refresh_items))):
            e = _recognition_ex(it, gloss_pool)
            e["instruction"] = "Review: what does this mean?"
            extras.append(e)
    if group:
        extras.append(_listening_ex(group[0], target_pool, lang))
    if len(group) >= 3:
        extras.append(_match_ex(group))
    exercises = recog + extras[: max(0, _SEG_BUDGET - len(recog))]
    random.shuffle(exercises)
    return exercises


async def generate_lesson(
    target_lang: str,
    lesson: dict,
    prior_concepts: list[dict] | None = None,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    grammar_content: dict | None = None,
    lesson_num: int = 1,
    prior_summaries: list[dict] | None = None,
) -> dict:
    """Build a segmented lesson: materialise each concept's accurate target via
    one translation call, then deterministically build teach+practice SEGMENTS.
    `lesson` = {title, objective, concepts:[{kind,key,label,gloss}]}.
    `grammar_content` maps a grammar concept key → its verified canonical artifact
    (grammar_lessons.generate_grammar_content) for the grammar-first segment.
    `lesson_num` is the 1-based sequential position in the course; `prior_summaries`
    is a list of {num, title, summary} dicts for all prior lessons with summaries.
    Returns {"segments": [...], "summary": "<brief summary of this lesson>"}."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    concepts = lesson.get("concepts", []) or []
    prior_concepts = prior_concepts or []

    prompt = _build_materialize_prompt(
        target_lang, concepts,
        lesson_num=lesson_num, prior_summaries=prior_summaries or [],
    )
    raw = await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
    targets = raw.get("targets") or {}
    notes = raw.get("notes") or {}
    intro = (raw.get("intro") or "").strip()
    lesson_summary = (raw.get("summary") or "").strip()
    has_rom = bool(LANG_INFO[target_lang].get("romanization"))
    R = tokenizer.romanize_text

    # Materialise. VOCAB concepts become testable items; GRAMMAR concepts get a
    # grammar-first segment (explicit rule + minimal pairs + verified drills) and
    # are kept OUT of the vocab exercise pipeline, so we never ask an ambiguous
    # "how do you say X" where several forms fit.
    grammar_content = grammar_content or {}
    items, grammar_teach, grammar_ex = [], [], []
    for c in concepts:
        key, gloss = c.get("key"), (c.get("gloss") or "").strip()
        target = (targets.get(key) or "").strip()
        note = (notes.get(key) or "").strip()
        roman = R(target, target_lang) if (has_rom and target) else ""
        if c.get("kind") == "grammar":
            art = grammar_content.get(key)
            if art:
                grammar_teach.append({
                    "grammar": True, "target": (c.get("label") or target or gloss),
                    "gloss": gloss, "blocks": art.get("blocks") or [],
                })
                grammar_ex += [dict(e) for e in (art.get("exercises") or [])]
            else:
                grammar_teach.append({"target": target, "target_roman": roman,
                                      "gloss": gloss, "note": note, "grammar": True})
        elif target:
            items.append({"key": key, "gloss": gloss, "target": target, "roman": roman, "note": note})

    gloss_pool = [it["gloss"] for it in items] + [(c.get("gloss") or "").strip() for c in prior_concepts]
    target_pool = [it["target"] for it in items] + [(c.get("label") or "").strip() for c in prior_concepts]

    def _teach_item(it):
        return {"target": it["target"], "target_roman": it["roman"], "gloss": it["gloss"], "note": it.get("note", "")}

    segments: list[dict] = []

    # Grammar-first segment: teach the rule(s) + minimal pairs, then drill them.
    if grammar_teach or grammar_ex:
        if not grammar_ex and grammar.has_conjugation(target_lang):
            # Fallback (no verified artifact): unambiguous conjugation drills.
            for c in concepts:
                inf = _verb_infinitive(c, targets.get(c.get("key"), ""))
                if inf:
                    grammar_ex += grammar.build_conjugation_exercises(inf, c.get("key"), n=2)
        segments.append({"teach": {"items": grammar_teach, "intro": intro}, "exercises": grammar_ex})
        intro = ""   # consumed by the grammar segment

    # Vocab segments: teach a few, practise, teach a few more.
    groups = [items[i:i + _SEGMENT_SIZE] for i in range(0, len(items), _SEGMENT_SIZE)]
    refresh: list[dict] = []
    for group in groups:
        teach = {"items": [_teach_item(it) for it in group]}
        if not segments:
            teach["intro"] = intro
            intro = ""
        exercises = _segment_exercises(group, target_lang, gloss_pool, target_pool, refresh)
        segments.append({"teach": teach, "exercises": exercises})
        refresh = refresh + group

    if not segments:
        segments = [{"teach": {"items": [], "intro": intro}, "exercises": []}]
    return {"segments": segments, "summary": lesson_summary}


def _verb_infinitive(concept: dict, target: str) -> str:
    """Return a conjugable infinitive for a verb concept, or "". Prefers the
    curriculum label (e.g. "aller (present)" → "aller") since the materialised
    target may be a conjugated/messy form; falls back to the target."""
    gloss = (concept.get("gloss") or "").strip().lower()
    is_verbish = gloss.startswith("to ") or "verb" in (concept.get("key", "") + gloss)
    if not is_verbish:
        return ""
    for cand in (concept.get("label", ""), target):
        c = re.sub(r"\s*\([^)]*\)\s*", "", cand or "").strip().lower()
        if c and grammar.conjugate_present(c):
            return c
    return ""
