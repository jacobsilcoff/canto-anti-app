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
    rom_note = (
        f"Do NOT write any {rom} romanisation/transliteration — our system computes "
        f"it from the native script and renders it as ruby automatically. Everything "
        f"you output is post-processed (romanisation added, audio synthesised, options "
        f"shuffled & keyed); emit native script ONLY.\n"
        if rom else
        "This language uses the Latin alphabet — no romanisation needed.\n"
    )
    return (f"Target language: {info['name']}\n"
            f"Writing system: {info['script']}\n"
            f"{rom_note}"
            f"Language-specific notes:\n{info['rules']}\n\n")


# ── Unit plan ────────────────────────────────────────────────────────────────

def _build_unit_plan_prompt(
    target_lang: str, level_target: str, unit_num: int,
    concept_registry: list[dict], unit_summaries: list[dict],
    learner_profile: str = "", mastery: list[dict] | None = None,
) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]

    profile_section = ""
    if learner_profile.strip():
        profile_section = f"── LEARNER BACKGROUND ──\n{learner_profile.strip()}\n\n"

    mastery_section = ""
    if mastery:
        weak = [m for m in mastery
                if m["total"] >= 3 and m["correct"] / m["total"] < 0.7]
        if weak:
            weak_str = ", ".join(m["concept_key"] for m in weak[:10])
            mastery_section = (
                f"── CONCEPTS NEEDING REINFORCEMENT (seen ≥3×, <70% accuracy) ──\n"
                f"{weak_str}\n"
                f"If the theme fits naturally, weave in extra practice or revisit these.\n\n"
            )

    return (
        f"You are an expert {name} curriculum designer building a Duolingo-style "
        f"course for an English speaker (proficiency goal {level_target}).\n\n"
        f"{_lang_preamble(info)}"
        f"{profile_section}"
        f"{mastery_section}"
        f"── WHAT'S BEEN TAUGHT ──\n{_registry_block(concept_registry)}\n\n"
        f"{_units_block(unit_summaries)}\n\n"
        f"── YOUR TASK ──\n"
        f"Design Unit {unit_num}: ONE coherent chapter — a communicative theme "
        f"(greetings, ordering food, family) or a focused grammar area (present tense, "
        f"articles & gender) — never a grab-bag.\n"
        f"List 6–10 concepts in TEACHING ORDER (foundational first; each builds on the "
        f"previous). Mix vocab and grammar as needed. Micro-lessons will teach 1–2 "
        f"concepts each in sequence, so order matters.\n"
        f"• vocab: label = everyday {name} word/phrase in NATIVE SCRIPT, citation form. "
        f"gloss = English meaning.\n"
        f"• grammar: label = concise pattern name (English ok). gloss = one-line English rule.\n"
        f"• key = stable snake_case identifier (e.g. greeting_hello, present_tense_er). "
        f"Do NOT reuse a key already taught.\n\n"
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
    learner_profile: str = "",
    mastery: list[dict] | None = None,
) -> dict:
    """One LLM call: draft a unit's ordered concept outline. Returns
    {title, objective, summary, concepts:[...], _raw_prompt, _raw_response}."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = _build_unit_plan_prompt(
        target_lang, level_target, unit_num,
        concept_registry or [], unit_summaries or [],
        learner_profile=learner_profile, mastery=mastery,
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
  {"kind":"cloze","concept":"<key>","sentence":"<full native sentence with exactly one ___>","answer":"<native word filling the blank>","gloss":"<full English translation of the sentence, shown to learner before they answer — must uniquely identify the answer>","distractors":["<other native form>", ...],"verb":"<plain infinitive if the blank is one conjugated verb, else omit>","person":"<je|tu|il|nous|vous|ils if verb given, else omit>"}
  {"kind":"reorder","concept":"<key>","sentence":"<full native sentence>","tokens":["<native word>", ...],"glossary":[{"token":"<exact token from tokens>","gloss":"<short English, or POS abbrev (PRT/AUX/CONJ/CL) for a function word>"}, ...]}
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
    taught: list[dict] | None = None,
) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    taught_block = ""
    if taught:
        taught_block = f"── ALREADY TAUGHT (the learner knows these) ──\n{_registry_block(taught)}\n\n"
    return (
        f"You are an expert {name} teacher. Author ONE focused micro-lesson "
        f"(teach blocks + drills together) for an English speaker.\n\n"
        f"{_lang_preamble(info)}"
        f"{_recent_block(recent_summaries)}"
        f"{taught_block}"
        f"── TEACH EXACTLY THESE {len(concepts)} CONCEPT(S) ──\n{_concepts_block(concepts)}\n\n"
        f"── TEACH BLOCKS ──\nTeach PROPORTIONATELY — don't over-explain simple words. "
        f"Author 1–4 ordered blocks total (a short textbook page). Use PROSE to state a "
        f"point plainly, TABLE for paradigms (conjugations, articles, genders — headers "
        f"in English), EXAMPLES for vocab, CONTRAST for minimal pairs, NOTE for "
        f"tips/common mistakes.\n"
        f"Straightforward, transparent vocab does NOT need its own teach block — let it "
        f"debut directly in a drill, where its English gloss is shown automatically. "
        f"Reserve teach blocks for grammar and for vocab that genuinely needs explaining "
        f"(non-obvious meaning, false friends, tricky usage, register). A lesson of only "
        f"simple vocab may need just one short EXAMPLES block, or none at all.\n"
        f"Block types:\n{_BLOCK_TYPES}\n\n"
        f"── DRILLS ──\nAuthor 4–7 drills for the concepts above (\"concept\" = its key). "
        f"Provide the CORRECT answer + DISTRACTORS — never an index; we shuffle & key.\n"
        f"EXACTLY ONE option must be correct:\n"
        f"• Every distractor must be unambiguously wrong for this exact prompt.\n"
        f"• CLOZE: the sentence must force exactly one filler — the learner sees the "
        f"full English translation of the sentence alongside the blank, so the answer "
        f"must be the only word that makes the English gloss true. Pronouns are the "
        f"hardest: '___係香港人' with gloss 'He is from Hong Kong' correctly forces 佢. "
        f"Don't blank a slot where multiple taught words fit; use recognition/production "
        f"instead. The `gloss` field must be a full English sentence (not a fragment), "
        f"matching the native sentence word-for-word so the learner can map each part.\n"
        f"• REORDER glossary: for helper tokens the learner doesn't know, add "
        f"`glossary` entries {{token, gloss}} (1–2 words or POS: PRT/AUX/CONJ/CL/PREP). "
        f"Don't gloss words already taught.\n"
        f"Lead with recognition, end with production or reorder. Kinds:\n{_DRILL_KINDS}\n\n"
        f"{_example_block()}"
        f"── VOCAB GLOSSARY ──\n"
        f"`vocab_glossary` = an English gloss for EVERY distinct native word that "
        f"appears in your TEACH BLOCKS (prose, examples, table cells, contrast) — "
        f"content words AND function words (particles, auxiliaries: use a short gloss "
        f"or a POS tag like PRT/AUX/CL). Gloss EVERYTHING, even simple or already-"
        f"taught words; the learner sees these only on tap/hover, so completeness "
        f"helps and over-listing costs nothing. Values: 1–3 words.\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        '  "title": "<short English lesson title>",\n'
        '  "objective": "<what the learner can do after this, one sentence>",\n'
        '  "intro": "<1 English sentence introducing this micro-lesson>",\n'
        '  "summary": "<20 words or less listing the specific items taught>",\n'
        '  "teach": [ <blocks> ],\n'
        '  "drills": [ <drills> ],\n'
        '  "vocab_glossary": {"<native token>": "<English, 1-3 words>", ...}\n'
        '}'
    )


# ── Drill assembly (we own the answer key) ───────────────────────────────────

def _order_tokens_from_sentence(sentence: str, tokens: list[str]) -> list[str] | None:
    """Return `tokens` reordered to match their left-to-right position in `sentence`.

    The model sometimes returns the tiles in the wrong order while `sentence` is
    correct. We walk the sentence (spaces stripped) greedily, matching each
    remaining token at the current position. Returns None if the tokens don't
    tile the sentence exactly (drill should be dropped in that case).
    """
    target = sentence.replace(" ", "")
    remaining = list(tokens)
    result = []
    pos = 0
    while remaining:
        matched = False
        for i, tok in enumerate(remaining):
            if target[pos: pos + len(tok)] == tok:
                result.append(tok)
                remaining.pop(i)
                pos += len(tok)
                matched = True
                break
        if not matched:
            return None
    return result if pos == len(target) else None


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
        # Re-derive correct token order from the sentence — the model sometimes
        # returns the tiles in the wrong order while `sentence` is correct.
        # Drop the drill if the tokens don't tile the sentence exactly.
        ordered = _order_tokens_from_sentence(sentence, tokens)
        if ordered is None:
            return None
        # Glosses for helper words the learner hasn't been taught (token → short
        # English / POS abbrev). Keep only entries whose token is actually a tile.
        tokset = set(ordered)
        glossary = {}
        for g in (d.get("glossary") or []):
            tok = (g.get("token") or "").strip()
            gl = (g.get("gloss") or "").strip()
            if tok in tokset and gl:
                glossary[tok] = gl
        return {"type": "word_bank", "concept_key": key, "grammar": is_grammar,
                "instruction": "Put the words in the correct order",
                "answer_tokens": ordered, "distractor_tokens": [],
                "glossary": glossary,
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
    Pure/deterministic. Returns {"vocab_glossary": {...}, "segments": [...]}

    `authored` — output of author_lesson() ({intro, teach, drills, vocab_glossary}).

    `vocab_glossary` = {native_word: English} for words used in TEACH text. The
    client reveals these on hover/tap (hidden by default), so we don't try to guess
    which words the learner knows — we keep every gloss the model offers, plus the
    concepts being introduced now. (Exercise prompts are never glossed client-side,
    so this can't leak answers.)
    """
    has_rom = bool(LANG_INFO[target_lang].get("romanization"))
    R = tokenizer.romanize_text

    def rom(s: str) -> str:
        return R(s, target_lang) if (has_rom and s) else ""

    kinds = {(c.get("key") or "").strip(): (c.get("kind") or "vocab") for c in concepts}

    # Concept labels being introduced NOW always get a gloss entry.
    concept_glossary = {
        c["label"].strip(): c["gloss"].strip()
        for c in concepts
        if (c.get("label") or "").strip() and (c.get("gloss") or "").strip()
    }
    llm_glossary = {}
    for k, v in (authored.get("vocab_glossary") or {}).items():
        k = (k or "").strip()
        v = (v or "").strip()
        if k and v:
            llm_glossary[k] = v
    # Concept entries take priority over LLM entries for the same word.
    vocab_glossary = {**llm_glossary, **concept_glossary}

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
    return {"vocab_glossary": vocab_glossary, "segments": [segment]}


async def author_lesson(
    target_lang: str,
    concepts: list[dict],
    recent_summaries: list[dict] | None = None,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    taught: list[dict] | None = None,
) -> dict:
    """One LLM call: author teach blocks + drills for these 1–2 concepts together,
    then validate/assemble. Returns lesson metadata + content + raw strings.

    `taught` — concepts the learner already knows (so the model doesn't re-teach
               them and the reorder glossary marks only genuinely-new helper words).
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = _build_lesson_prompt(target_lang, concepts, recent_summaries or [], taught)
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
