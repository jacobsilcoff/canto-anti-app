"""Adaptive lesson generation — one lesson at a time (IDEAS item 43).

Two-step flow:
  1. generate_next_lesson()  — one LLM call: decides what to teach next AND
     materialises vocab concept targets. Returns lesson metadata + close_unit
     signal + raw LLM strings for the debug panel.
  2. build_lesson_segments() — deterministic exercise building. Pure/no-LLM.
     Grammar concepts go through grammar_lessons.generate_grammar_content()
     separately (shared cache, heavier model, amortised across users).

Memory passed to generation (three tiers, compact):
  Tier 1 — concept registry  : every concept key/label/gloss introduced so far
  Tier 2 — unit summaries    : one paragraph per completed unit
  Tier 3 — recent lessons    : full summaries of the last 2–3 lessons
"""
import asyncio
import random
import re

import grammar
import tokenizer
from translation import LANG_INFO, DEFAULT_MODEL, _call, _parse_json

_SEGMENT_SIZE = 3   # vocab concepts taught per segment
_SEG_BUDGET   = 8   # max exercises per segment


# ── Prompt ────────────────────────────────────────────────────────────────────

def _build_next_lesson_prompt(
    target_lang: str,
    level_target: str,
    lesson_num: int,
    concept_registry: list[dict],
    unit_summaries: list[dict],
    recent_summaries: list[dict],
) -> str:
    info   = LANG_INFO[target_lang]
    name   = info["name"]
    script = info["script"]
    rom    = info["romanization"]
    rules  = info["rules"]

    # Tier 1: concept registry
    if concept_registry:
        reg_lines = "\n".join(
            f'{c.get("key","?")} | {c.get("label","?")} | {c.get("gloss","")}'
            for c in concept_registry
        )
        reg_block = (
            "Concepts already taught (do NOT introduce these as new — you may use "
            "them in examples or build on them):\n" + reg_lines
        )
    else:
        reg_block = (
            "No concepts taught yet — this is the very first lesson. "
            "Begin with the absolute basics."
        )

    # Tier 2: completed unit summaries
    if unit_summaries:
        unit_lines = "\n".join(
            f'Unit {i + 1} "{u.get("title", "")}": {u.get("summary", "")}'
            for i, u in enumerate(unit_summaries)
        )
        unit_block = "Completed units:\n" + unit_lines
    else:
        unit_block = "No completed units yet."

    # Tier 3: recent lesson summaries
    recent_block = ""
    if recent_summaries:
        rec_lines = "\n".join(
            f'Lesson {s.get("lesson_num", "?")} "{s.get("title", "")}": {s.get("summary", "")}'
            for s in recent_summaries
        )
        recent_block = "Recent lessons:\n" + rec_lines + "\n\n"

    rom_note = (
        f"Romanisation scheme: {rom}.\n" if rom else
        "This language uses the Latin alphabet — no romanisation needed.\n"
    )

    return (
        f"You are an expert {name} teacher. Generate the NEXT lesson in an adaptive "
        f"course for an English speaker learning {name}.\n\n"
        f"Target language: {name}\n"
        f"Writing system: {script}\n"
        f"{rom_note}"
        f"Proficiency goal: {level_target} (loose guidance — trust your judgment on pacing)\n"
        f"Language-specific notes:\n{rules}\n\n"
        f"── WHAT'S BEEN TAUGHT SO FAR ──\n"
        f"{reg_block}\n\n"
        f"{unit_block}\n\n"
        f"{recent_block}"
        f"This is Lesson {lesson_num}.\n\n"
        f"── LESSON DESIGN RULES ──\n"
        f"• Teach ONE focused topic or grammar point — do NOT mix unrelated material.\n"
        f"• Do NOT re-introduce any concept already listed above as new.\n"
        f"• Vocab concepts: label = most natural everyday {name} word/phrase in native "
        f"script, citation form, NO romanisation in the label. Provide it in \"targets\" too.\n"
        f"• Grammar concepts: label = concise pattern name; gloss = one-line English "
        f"explanation. No target entry needed (handled by a separate grammar pipeline).\n"
        f"• 4–7 tightly related concepts per lesson. Depth over breadth.\n"
        f"• Where glosses overlap (e.g. two ways to say 'thank you'), distinguish them "
        f"in the gloss and add a note explaining when to use each.\n\n"
        f"── UNIT BOUNDARY ──\n"
        f"A unit is a coherent communicative theme (~4–8 lessons). After this lesson, "
        f"should the current unit be closed? If yes: close_unit=true, provide unit_title "
        f"(short English name) and unit_summary (one sentence). Otherwise close_unit=false.\n\n"
        f"── RETURN FORMAT ──\n"
        f"Return ONLY valid JSON, no other text:\n"
        "{{\n"
        '  "title": "short English lesson title",\n'
        '  "objective": "what the learner can do after this (one sentence)",\n'
        '  "concepts": [\n'
        f'    {{"kind":"vocab","key":"<snake_case_key>","label":"<{name} native script>","gloss":"<English>"}},\n'
        f'    {{"kind":"grammar","key":"<snake_case_key>","label":"<grammar name>","gloss":"<one-line explanation>"}}\n'
        "  ],\n"
        '  "targets": {{"<key>": "<native script form>"}},\n'
        '  "intro": "1-2 English sentences introducing this specific topic",\n'
        '  "notes": {{"<key>": "<one-sentence usage note>"}},\n'
        '  "summary": "<30 words or less listing the specific items taught>",\n'
        '  "close_unit": false,\n'
        '  "unit_title": null,\n'
        '  "unit_summary": null\n'
        "}}\n"
        "(targets: vocab concepts only; grammar concepts are handled by a separate pipeline)"
    )


# ── Generation ────────────────────────────────────────────────────────────────

async def generate_next_lesson(
    target_lang: str,
    level_target: str = "A1",
    lesson_num: int = 1,
    concept_registry: list[dict] | None = None,
    unit_summaries: list[dict] | None = None,
    recent_summaries: list[dict] | None = None,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """One LLM call: decide what to teach + materialise vocab targets.

    Returns lesson metadata, targets, close_unit signal, and raw LLM strings
    (stored as llm_debug_json for the in-app debug panel).
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")

    prompt = _build_next_lesson_prompt(
        target_lang, level_target, lesson_num,
        concept_registry or [], unit_summaries or [], recent_summaries or [],
    )
    raw = await asyncio.to_thread(lambda: _call(prompt, api_key, model))
    parsed = _parse_json(raw) or {}

    return {
        "title":         (parsed.get("title")       or "").strip(),
        "objective":     (parsed.get("objective")   or "").strip(),
        "concepts":      parsed.get("concepts")     or [],
        "targets":       parsed.get("targets")      or {},
        "intro":         (parsed.get("intro")       or "").strip(),
        "notes":         parsed.get("notes")        or {},
        "summary":       (parsed.get("summary")     or "").strip(),
        "close_unit":    bool(parsed.get("close_unit")),
        "unit_title":    (parsed.get("unit_title")   or "").strip() or None,
        "unit_summary":  (parsed.get("unit_summary") or "").strip() or None,
        "_raw_prompt":   prompt,
        "_raw_response": raw or "",
    }


# ── Segment building (deterministic, no LLM) ─────────────────────────────────

def _pick_options(correct: str, pool: list[str], n: int = 4) -> tuple[list[str], int]:
    seen = {correct}
    distractors: list[str] = []
    for p in pool:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            distractors.append(p)
    random.shuffle(distractors)
    opts = [correct] + distractors[: max(0, n - 1)]
    random.shuffle(opts)
    return opts, opts.index(correct)


def _recognition_ex(it: dict, gloss_pool: list[str]) -> dict:
    opts, ans = _pick_options(it["gloss"], gloss_pool)
    return {
        "type": "choice", "concept_key": it["key"],
        "instruction": "What does this mean?",
        "prompt": it["target"], "prompt_lang": "target", "prompt_roman": it["roman"],
        "audio": it["target"], "options": opts, "answer": ans, "tip": it.get("note", ""),
    }


def _production_ex(it: dict, target_pool: list[str], lang: str) -> dict:
    R = tokenizer.romanize_text
    opts, ans = _pick_options(it["target"], target_pool)
    return {
        "type": "choice", "concept_key": it["key"],
        "instruction": "How do you say this?",
        "prompt": it["gloss"], "prompt_lang": "english", "audio": "",
        "options": opts, "options_roman": [R(o, lang) for o in opts],
        "answer": ans, "tip": it.get("note", ""),
    }


def _listening_ex(it: dict, target_pool: list[str], lang: str) -> dict:
    R = tokenizer.romanize_text
    opts, ans = _pick_options(it["target"], target_pool)
    return {
        "type": "listening", "concept_key": it["key"],
        "instruction": "What did you hear?",
        "audio": it["target"], "audio_roman": it["roman"],
        "options": opts, "options_roman": [R(o, lang) for o in opts],
        "answer": ans, "tip": it.get("note", ""),
    }


def _match_ex(items: list[dict]) -> dict:
    return {
        "type": "match", "instruction": "Match the pairs",
        "pairs": [
            {"target": it["target"], "target_roman": it["roman"], "english": it["gloss"]}
            for it in items[:5]
        ],
    }


def _segment_exercises(
    group: list[dict], lang: str,
    gloss_pool: list[str], target_pool: list[str],
    refresh_items: list[dict],
) -> list[dict]:
    recog  = [_recognition_ex(it, gloss_pool) for it in group]
    extras = [_production_ex(it, target_pool, lang) for it in group]
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


def _verb_infinitive(concept: dict, target: str) -> str:
    gloss = (concept.get("gloss") or "").strip().lower()
    is_verbish = gloss.startswith("to ") or "verb" in (concept.get("key", "") + gloss)
    if not is_verbish:
        return ""
    for cand in (concept.get("label", ""), target):
        c = re.sub(r"\s*\([^)]*\)\s*", "", cand or "").strip().lower()
        if c and grammar.conjugate_present(c):
            return c
    return ""


def build_lesson_segments(
    target_lang: str,
    lesson_data: dict,
    grammar_content: dict | None = None,
    prior_concepts: list[dict] | None = None,
) -> dict:
    """Build teach + exercise segments from pre-materialised lesson data.
    Pure/deterministic — no LLM calls.

    `lesson_data`     — output of generate_next_lesson()
    `grammar_content` — {concept_key: grammar_lessons artifact}
    Returns {"segments": [...]}.
    """
    prior_concepts  = prior_concepts or []
    concepts        = lesson_data.get("concepts") or []
    targets         = lesson_data.get("targets")  or {}
    notes           = lesson_data.get("notes")    or {}
    intro           = lesson_data.get("intro")    or ""

    has_rom = bool(LANG_INFO[target_lang].get("romanization"))
    R = tokenizer.romanize_text
    grammar_content = grammar_content or {}

    items: list[dict]         = []
    grammar_teach: list[dict] = []
    grammar_ex: list[dict]    = []

    for c in concepts:
        key    = c.get("key") or ""
        gloss  = (c.get("gloss") or "").strip()
        target = (targets.get(key) or "").strip()
        note   = (notes.get(key)   or "").strip()
        roman  = R(target, target_lang) if (has_rom and target) else ""

        if c.get("kind") == "grammar":
            art = grammar_content.get(key)
            if art:
                grammar_teach.append({
                    "grammar": True,
                    "target": (c.get("label") or target or gloss),
                    "gloss": gloss,
                    "blocks": art.get("blocks") or [],
                })
                grammar_ex += [dict(e) for e in (art.get("exercises") or [])]
            else:
                grammar_teach.append({
                    "target": c.get("label") or target or gloss,
                    "target_roman": roman, "gloss": gloss,
                    "note": note, "grammar": True,
                })
        elif target:
            items.append({"key": key, "gloss": gloss, "target": target,
                          "roman": roman, "note": note})

    gloss_pool  = ([it["gloss"]  for it in items]
                   + [(c.get("gloss")  or "").strip() for c in prior_concepts])
    target_pool = ([it["target"] for it in items]
                   + [(c.get("label")  or "").strip() for c in prior_concepts])

    def _teach_item(it: dict) -> dict:
        return {"target": it["target"], "target_roman": it["roman"],
                "gloss": it["gloss"], "note": it.get("note", "")}

    segments: list[dict] = []

    if grammar_teach or grammar_ex:
        if not grammar_ex and grammar.has_conjugation(target_lang):
            for c in concepts:
                inf = _verb_infinitive(c, targets.get(c.get("key", ""), ""))
                if inf:
                    grammar_ex += grammar.build_conjugation_exercises(inf, c.get("key"), n=2)
        segments.append({
            "teach":     {"items": grammar_teach, "intro": intro},
            "exercises": grammar_ex,
        })
        intro = ""

    groups  = [items[i:i + _SEGMENT_SIZE] for i in range(0, len(items), _SEGMENT_SIZE)]
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

    return {"segments": segments}
