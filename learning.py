"""Adaptive lesson generation — unit-plan-first, unified micro-lesson authoring.

Two levels (IDEAS item 43):

  1. generate_unit_plan()  — once per unit. One LLM call drafts a coherent
     chapter: an ordered list of 6–10 concepts (vocab + grammar, interleaved).
     Stored on the course as the "active plan"; each micro-lesson consumes 1–2
     concepts from it in order. This is where COHERENCE lives — the unit is the
     chapter, the lesson is a micro-step.

  2. author_lesson()       — once per micro-lesson. ONE LLM call authors the
     WHOLE small lesson together: free-form teach blocks AND the drills, for the
     1–2 concepts handed to it. Grammar and vocab are NOT segregated — the model
     sees one palette (block types + drill kinds) and picks what the point needs.

Design principle — **liberal in what you SHOW, strict in what you GRADE**:
  - Teach blocks are authored freely (prose/table/examples/contrast/note) and
    rendered by the client; a free oracle recomputes romanization on every cell.
  - Drills are authored as {correct answer + distractors}; WE assemble the graded
    exercise (place the known-correct option, shuffle, index) so the answer key is
    correct by construction. Romanization comes from tokenizer, French present-
    tense cloze answers/options from grammar.py — never the model.

Memory passed to generation (compact, three tiers):
  Tier 1 — concept registry : every concept key/label/gloss introduced so far
  Tier 2 — unit summaries   : one line per completed unit
  Tier 3 — recent lessons   : summaries of the last 2–3 lessons (continuity)
"""
import asyncio
import json
import os
import random

import grammar
import tokenizer
from grammar_lessons import _clean_block, _conj_cloze, _free_cloze
from translation import LANG_INFO, DEFAULT_MODEL, _call, _parse_json

# A real, hand-written micro-lesson used as a few-shot example in the author
# prompt. Editing this file is the intended way to steer lesson STYLE implicitly
# (the model pattern-matches its shape and quality). See examples/lesson_fr.json.
_EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "lesson_example.json")


def _load_example() -> dict:
    try:
        with open(_EXAMPLE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ── Memory blocks (shared by both prompts) ───────────────────────────────────

def _registry_block(concept_registry: list[dict]) -> str:
    if not concept_registry:
        return ("No concepts taught yet — this is the very start of the course. "
                "Begin with the absolute basics.")
    lines = "\n".join(
        f'{c.get("key","?")} | {c.get("label","?")} | {c.get("gloss","")}'
        for c in concept_registry
    )
    return ("Concepts already taught (do NOT re-introduce these; you MAY reuse them "
            "in examples and drills):\n" + lines)


def _units_block(unit_summaries: list[dict]) -> str:
    if not unit_summaries:
        return "No completed units yet."
    lines = "\n".join(
        f'Unit {i + 1} "{u.get("title", "")}": {u.get("summary", "")}'
        for i, u in enumerate(unit_summaries)
    )
    return "Completed units:\n" + lines


def _recent_block(recent_summaries: list[dict]) -> str:
    if not recent_summaries:
        return ""
    lines = "\n".join(
        f'Lesson {s.get("lesson_num", "?")} "{s.get("title", "")}": {s.get("summary", "")}'
        for s in recent_summaries
    )
    return "Recent lessons (for continuity):\n" + lines + "\n\n"


def _lang_preamble(info: dict) -> str:
    rom = info["romanization"]
    rom_note = (f"Romanisation scheme: {rom}.\n" if rom else
                "This language uses the Latin alphabet — no romanisation needed.\n")
    return (f"Target language: {info['name']}\n"
            f"Writing system: {info['script']}\n"
            f"{rom_note}"
            f"Language-specific notes:\n{info['rules']}\n\n")


# ── Unit plan ────────────────────────────────────────────────────────────────

def _build_unit_plan_prompt(
    target_lang: str, level_target: str, unit_num: int,
    concept_registry: list[dict], unit_summaries: list[dict],
) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    return (
        f"You are an expert {name} curriculum designer building a Duolingo-style "
        f"course for an English speaker (proficiency goal {level_target}; loose "
        f"guidance — trust your judgment on pacing).\n\n"
        f"{_lang_preamble(info)}"
        f"── WHAT'S BEEN TAUGHT ──\n{_registry_block(concept_registry)}\n\n"
        f"{_units_block(unit_summaries)}\n\n"
        f"── YOUR TASK ──\n"
        f"Design Unit {unit_num}: ONE coherent chapter. It is either a communicative "
        f"theme (e.g. greetings, ordering food, talking about family) or a focused "
        f"grammar area (e.g. the present tense, articles & gender) — never a grab-bag.\n"
        f"List 6–10 concepts in TEACHING ORDER (foundational first; each builds on the "
        f"previous). Mix vocab and grammar as the theme needs. Each micro-lesson will "
        f"later teach 1–2 of these in order, so they must form a sensible sequence.\n"
        f"• vocab concept: label = the most natural everyday {name} word/phrase in "
        f"NATIVE SCRIPT, citation form, NO romanisation. gloss = English meaning.\n"
        f"• grammar concept: label = concise pattern name (English ok); gloss = "
        f"one-line English explanation of the rule.\n"
        f"• key = stable snake_case English-gloss identifier (e.g. greeting_hello, "
        f"present_tense_er). Do NOT reuse a key already taught.\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        '  "title": "<short English chapter title>",\n'
        '  "objective": "<what the learner can do after this unit, one sentence>",\n'
        '  "summary": "<one-sentence description for future-unit context>",\n'
        '  "concepts": [\n'
        f'    {{"kind":"vocab","key":"<snake_case>","label":"<{name} native script>","gloss":"<English>"}},\n'
        '    {"kind":"grammar","key":"<snake_case>","label":"<pattern name>","gloss":"<one-line rule>"}\n'
        '  ]\n'
        '}'
    )


async def generate_unit_plan(
    target_lang: str,
    level_target: str = "A1",
    unit_num: int = 1,
    concept_registry: list[dict] | None = None,
    unit_summaries: list[dict] | None = None,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """One LLM call: draft a unit's ordered concept outline. Returns
    {title, objective, summary, concepts:[...], _raw_prompt, _raw_response}."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = _build_unit_plan_prompt(
        target_lang, level_target, unit_num,
        concept_registry or [], unit_summaries or [],
    )
    raw = await asyncio.to_thread(lambda: _call(prompt, api_key, model))
    parsed = _parse_json(raw) or {}

    concepts = []
    for c in (parsed.get("concepts") or []):
        key = (c.get("key") or "").strip()
        if not key:
            continue
        concepts.append({
            "kind":  (c.get("kind") or "vocab").strip(),
            "key":   key,
            "label": (c.get("label") or "").strip(),
            "gloss": (c.get("gloss") or "").strip(),
        })
    return {
        "title":     (parsed.get("title") or "").strip(),
        "objective": (parsed.get("objective") or "").strip(),
        "summary":   (parsed.get("summary") or "").strip(),
        "concepts":  concepts,
        "_raw_prompt":   prompt,
        "_raw_response": raw or "",
    }


# ── Micro-lesson authoring ───────────────────────────────────────────────────

# Block types the model may use to TEACH (rendered client-side, romanization
# recomputed by us). Drill kinds the model may use to PRACTISE (we assemble the
# graded exercise from {answer + distractors} so the key is correct by build).
_DRILL_KINDS = """\
  {"kind":"recognition","concept":"<key>","target":"<native word/phrase>","gloss":"<English meaning>","distractors":["<other English meaning>", ...]}
  {"kind":"production","concept":"<key>","gloss":"<English prompt>","target":"<native answer>","distractors":["<other native form>", ...]}
  {"kind":"listening","concept":"<key>","target":"<native word/phrase>","gloss":"<English>","distractors":["<other native form>", ...]}
  {"kind":"cloze","concept":"<key>","sentence":"<full native sentence with exactly one ___>","answer":"<native word filling the blank>","gloss":"<English of the sentence>","distractors":["<other native form>", ...],"verb":"<plain infinitive if the blank is one conjugated verb, else omit>","person":"<je|tu|il|nous|vous|ils if verb given, else omit>"}
  {"kind":"reorder","concept":"<key>","sentence":"<full native sentence>","tokens":["<native word>", ...]}
  {"kind":"match","concept":"<key>","pairs":[{"target":"<native>","english":"<English>"}, ...]}"""

_BLOCK_TYPES = """\
  {"type":"prose","text":"<plain-English explanation>"}
  {"type":"table","title":"<short title>","columns":["<header>", ...],"rows":[["<cell>", ...], ...]}
  {"type":"examples","items":[{"text":"<native phrase>","gloss":"<English>"}, ...]}
  {"type":"contrast","a":{"text":"<native>","gloss":"<English>"},"b":{"text":"<native>","gloss":"<English>"},"label":"<the ONE feature that differs>"}
  {"type":"note","text":"<short tip / common-mistake warning>"}"""


def _concepts_block(concepts: list[dict]) -> str:
    lines = []
    for c in concepts:
        kind = c.get("kind") or "vocab"
        lines.append(f'• [{kind}] {c.get("key","")} — {c.get("label","")} = {c.get("gloss","")}')
    return "\n".join(lines)


def _example_block() -> str:
    ex = _load_example()
    if not ex.get("input") or not ex.get("output"):
        return ""
    return (
        "── EXAMPLE (a different language/topic — match its SHAPE and brevity, not its "
        "content) ──\n"
        "INPUT concepts:\n" + json.dumps(ex["input"], ensure_ascii=False) + "\n"
        "GOOD OUTPUT:\n" + json.dumps(ex["output"], ensure_ascii=False, indent=1) + "\n\n"
    )


def _build_lesson_prompt(
    target_lang: str, concepts: list[dict], recent_summaries: list[dict],
) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    native_rule = (
        f"CRITICAL — write ALL {name} text in NATIVE SCRIPT ONLY. Never include "
        "romanisation/transliteration/pinyin/jyutping inline anywhere (prose, table "
        "cells, examples, sentences). A ruby engine adds it automatically above the "
        "characters; inline it would double and corrupt the display."
    ) if info["romanization"] else (
        f"Write all {name} text naturally (Latin alphabet; no pronunciation respelling)."
    )
    return (
        f"You are an expert {name} teacher authoring ONE short Duolingo-style lesson "
        f"for an English speaker — a tightly-focused micro-step. Author the teaching "
        f"text AND the practice drills TOGETHER so they reinforce each other.\n\n"
        f"{_lang_preamble(info)}"
        f"{_recent_block(recent_summaries)}"
        f"── TEACH EXACTLY THESE {len(concepts)} CONCEPT(S) ──\n{_concepts_block(concepts)}\n\n"
        f"{native_rule}\n\n"
        f"── TEACH BLOCKS ──\nAuthor 2–5 ordered blocks (like a page of a textbook). "
        f"Open with prose stating the point plainly (when/how it applies, where English "
        f"misleads). Use a TABLE for any paradigm (conjugation, articles, genders…), "
        f"every cell correct, headers in English; if two rows would share a first-column "
        f"label, add a qualifier so they're distinguishable. Use EXAMPLES for vocab and "
        f"CONTRAST for minimal pairs. Block types:\n{_BLOCK_TYPES}\n\n"
        f"── DRILLS ──\nAuthor 4–7 drills. EVERY drill must practise one of the concepts "
        f"above (set \"concept\" to its key). Give the CORRECT answer plus plausible "
        f"DISTRACTORS — never an index; we build and shuffle the options. Distractors "
        f"must be wrong-but-tempting (same category/length). Mix kinds; lead with easier "
        f"recognition, end with production or reorder. Kinds:\n{_DRILL_KINDS}\n\n"
        f"{_example_block()}"
        f"── REMEMBER ──\n"
        f"• Teach ONLY the concept(s) listed — do not drift.\n"
        f"• {native_rule}\n"
        f"• Every drill tests a listed concept; distractors are wrong on purpose.\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        '  "title": "<short English lesson title>",\n'
        '  "objective": "<what the learner can do after this, one sentence>",\n'
        '  "intro": "<1 English sentence introducing this micro-lesson>",\n'
        '  "summary": "<20 words or less listing the specific items taught>",\n'
        '  "teach": [ <blocks> ],\n'
        '  "drills": [ <drills> ]\n'
        '}'
    )


# ── Drill assembly (we own the answer key) ───────────────────────────────────

def _pick_options(correct: str, distractors: list[str], n: int = 4) -> tuple[list[str], int]:
    """Shuffle [correct] + de-duped distractors into n options; return (opts, idx)."""
    seen = {correct.lower()}
    pool = []
    for d in distractors:
        d = (d or "").strip()
        if d and d.lower() not in seen:
            seen.add(d.lower())
            pool.append(d)
    random.shuffle(pool)
    opts = [correct] + pool[: max(0, n - 1)]
    random.shuffle(opts)
    return opts, opts.index(correct)


def _assemble_drill(d: dict, lang: str, kinds: dict, rom) -> dict | None:
    """Turn one authored drill into a frontend exercise object, or None to drop."""
    kind = (d.get("kind") or "").strip()
    key = (d.get("concept") or "").strip()
    is_grammar = kinds.get(key) == "grammar"
    target = (d.get("target") or "").strip()
    gloss = (d.get("gloss") or "").strip()
    distract = d.get("distractors") or []

    if kind == "recognition":
        if not target or not gloss:
            return None
        opts, ans = _pick_options(gloss, distract)
        if len(opts) < 2:
            return None
        return {"type": "choice", "concept_key": key, "grammar": is_grammar,
                "instruction": "What does this mean?", "prompt": target,
                "prompt_lang": "target", "prompt_roman": rom(target),
                "audio": target, "options": opts, "answer": ans, "tip": gloss}

    if kind == "production":
        if not target or not gloss:
            return None
        opts, ans = _pick_options(target, distract)
        if len(opts) < 2:
            return None
        return {"type": "choice", "concept_key": key, "grammar": is_grammar,
                "instruction": "How do you say this?", "prompt": gloss,
                "prompt_lang": "english", "options": opts,
                "options_roman": [rom(o) for o in opts], "answer": ans, "tip": ""}

    if kind == "listening":
        if not target:
            return None
        opts, ans = _pick_options(target, distract)
        if len(opts) < 2:
            return None
        return {"type": "listening", "concept_key": key, "grammar": is_grammar,
                "instruction": "What did you hear?", "audio": target,
                "audio_roman": rom(target), "options": opts,
                "options_roman": [rom(o) for o in opts], "answer": ans, "tip": gloss}

    if kind == "cloze":
        sentence = (d.get("sentence") or "").strip()
        answer = (d.get("answer") or "").strip()
        verb = (d.get("verb") or "").strip().lower()
        person = (d.get("person") or "").strip().lower()
        ex = None
        if grammar.has_conjugation(lang) and verb and person:
            ex = _conj_cloze(key, lang, sentence, gloss, verb, person, answer)
        if ex is None:
            ex = _free_cloze(key, sentence, gloss, answer, distract)
        if ex is None:
            return None
        ex["grammar"] = is_grammar
        ex["prompt_roman"] = rom(sentence)
        return ex

    if kind == "reorder":
        sentence = (d.get("sentence") or "").strip()
        tokens = [t for t in (d.get("tokens") or []) if (t or "").strip()]
        if len(tokens) < 2:
            return None
        return {"type": "word_bank", "concept_key": key, "grammar": is_grammar,
                "instruction": "Put the words in the correct order",
                "answer_tokens": tokens, "distractor_tokens": [],
                "audio": sentence, "answer_roman": rom(sentence)}

    if kind == "match":
        pairs = []
        for p in (d.get("pairs") or []):
            t = (p.get("target") or "").strip()
            e = (p.get("english") or "").strip()
            if t and e:
                pairs.append({"target": t, "target_roman": rom(t), "english": e})
        if len(pairs) < 2:
            return None
        return {"type": "match", "concept_key": key, "grammar": is_grammar,
                "instruction": "Match the pairs", "pairs": pairs[:5]}

    return None


def assemble_lesson(target_lang: str, concepts: list[dict], authored: dict) -> dict:
    """Validate + assemble authored output into the stored lesson content.
    Pure/deterministic. Returns {"segments": [...]} (one teach→drill segment).

    `authored` — output of author_lesson() ({intro, teach:[blocks], drills:[...]}).
    """
    has_rom = bool(LANG_INFO[target_lang].get("romanization"))
    R = tokenizer.romanize_text

    def rom(s: str) -> str:
        return R(s, target_lang) if (has_rom and s) else ""

    kinds = {(c.get("key") or "").strip(): (c.get("kind") or "vocab") for c in concepts}

    blocks = []
    for b in (authored.get("teach") or []):
        cleaned = _clean_block(b, rom)
        if cleaned:
            blocks.append(cleaned)

    exercises = []
    for d in (authored.get("drills") or []):
        ex = _assemble_drill(d, target_lang, kinds, rom)
        if ex:
            exercises.append(ex)
    random.shuffle(exercises)

    segment = {
        "teach": {"intro": (authored.get("intro") or "").strip(), "blocks": blocks},
        "exercises": exercises,
    }
    return {"segments": [segment]}


async def author_lesson(
    target_lang: str,
    concepts: list[dict],
    recent_summaries: list[dict] | None = None,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """One LLM call: author teach blocks + drills for these 1–2 concepts together,
    then validate/assemble. Returns lesson metadata + content + raw strings.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = _build_lesson_prompt(target_lang, concepts, recent_summaries or [])
    raw = await asyncio.to_thread(lambda: _call(prompt, api_key, model))
    parsed = _parse_json(raw) or {}

    content = assemble_lesson(target_lang, concepts, parsed)
    return {
        "title":     (parsed.get("title") or "").strip(),
        "objective": (parsed.get("objective") or "").strip(),
        "summary":   (parsed.get("summary") or "").strip(),
        "content":   content,
        "_raw_prompt":   prompt,
        "_raw_response": raw or "",
    }
