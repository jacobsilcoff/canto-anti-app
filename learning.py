"""Adaptive lesson generation — just-in-time planner + unified lesson authoring.

Two LLM calls per lesson, BOTH adaptive (no frozen unit plan):

  1. plan_next_lesson()    — once per lesson. One cheap LLM call decides the
     single best NEXT lesson from live learner state (concept registry, the
     current chapter, weak skills, known/weak/recently-added deck words + a CEFR
     spread, learner profile). It returns a `lesson_spec`: a focused skill, how
     BROAD to teach it (a whole grammar family in one go vs. a narrow exceptions
     lesson), the items to cover, and whether to continue or open a new chapter.
     COHERENCE lives in the chapter it carries forward; ADAPTIVITY lives in
     re-deciding every lesson (so tutor chat / new cards steer what comes next).

  2. author_lesson()       — once per lesson. ONE LLM call authors the WHOLE
     lesson together: free-form teach blocks AND the drills, for the skill +
     items handed to it. Grammar and vocab are NOT segregated — the model sees
     one palette (block types + drill kinds) and picks what the point needs.

The model for both calls is provider-pluggable via `llm.call` (Gemini or Claude),
selected by the caller; the deterministic assembler below is identical regardless.

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
import json
import os
import random

import grammar
import llm
import tokenizer
from grammar_lessons import _clean_block, _conj_cloze, _free_cloze
from translation import LANG_INFO, DEFAULT_MODEL, _parse_json

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

_REGISTRY_CAP = 150   # most recent concepts included in prompts (bounds prompt size)


def _registry_block(concept_registry: list[dict]) -> str:
    if not concept_registry:
        return ("No concepts taught yet — this is the very start of the course. "
                "Begin with the absolute basics.")
    recent = concept_registry[-_REGISTRY_CAP:]
    lines = "\n".join(
        f'{c.get("key","?")} | {c.get("label","?")} | {c.get("gloss","")}'
        for c in recent
    )
    omitted = len(concept_registry) - len(recent)
    head = (f"Concepts already taught ({omitted} earlier ones omitted; do NOT "
            if omitted else "Concepts already taught (do NOT ")
    return (head + "re-introduce these; you MAY reuse them in examples and drills):\n"
            + lines)


def _word_list_block(words: list[dict]) -> str:
    """Compact `word = gloss` lines for SRS-deck word lists in prompts."""
    return "\n".join(
        f'{w.get("target_text","")} = {w.get("gloss","")}'
        for w in words if (w.get("target_text") or "").strip()
    )


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


# ── Next-lesson planner (just-in-time, adaptive) ─────────────────────────────

_PLAN_KNOWN_SAMPLE = 60   # known words shown to the planner (strongest first)


def _chapter_block(chapter: dict | None) -> str:
    """The chapter currently in progress, so the planner can continue it (or
    decide it's done and open a new one)."""
    if not chapter or not (chapter.get("title") or "").strip():
        return ("CURRENT CHAPTER: none in progress — you MUST open a new chapter "
                "(set chapter_action to \"new\" and provide `chapter`).\n")
    return (
        f"CURRENT CHAPTER (in progress): \"{chapter.get('title','')}\"\n"
        f"  objective: {chapter.get('objective','')}\n"
        f"  so far: {chapter.get('summary','')}\n"
        f"Continue this chapter (chapter_action \"continue\") until its objective is "
        f"met, THEN open a new one (\"new\"). Cutting a chapter short is fine if the "
        f"learner clearly needs something else next.\n"
    )


def _build_plan_prompt(
    target_lang: str, level_target: str,
    concept_registry: list[dict], recent_summaries: list[dict],
    current_chapter: dict | None = None,
    learner_profile: str = "", mastery: list[dict] | None = None,
    known_words: list[dict] | None = None, weak_words: list[dict] | None = None,
    recent_cards: list[dict] | None = None, cefr_spread: str = "",
) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]

    profile_section = ""
    if (learner_profile or "").strip():
        profile_section = f"── LEARNER BACKGROUND ──\n{learner_profile.strip()}\n\n"

    level_section = ""
    if (cefr_spread or "").strip():
        level_section = (
            f"── VOCAB LEVEL (CEFR spread of known words) ──\n{cefr_spread.strip()}\n\n"
        )

    deck_section = ""
    if known_words:
        deck_section = (
            f"── FLASHCARD VOCAB THE LEARNER ALREADY KNOWS (strongest first) ──\n"
            f"{_word_list_block(known_words[:_PLAN_KNOWN_SAMPLE])}\n"
            f"Do NOT propose these as NEW vocab — the learner knows them. Lean on them "
            f"to teach grammar (a pattern is easier through familiar words).\n\n"
        )

    recent_section = ""
    if recent_cards:
        recent_section = (
            f"── JUST ADDED TO THEIR DECK (tutor chat / flashcards, newest first) ──\n"
            f"{_word_list_block(recent_cards)}\n"
            f"The learner is actively picking these up elsewhere in the app. If a few "
            f"cohere into a theme or a grammar point, BUILD the next lesson around them "
            f"— this is how the course adapts to what they're learning right now.\n\n"
        )

    weak_section = ""
    if weak_words:
        weak_section = (
            f"── STRUGGLING FLASHCARD WORDS ──\n{_word_list_block(weak_words)}\n\n"
        )

    mastery_section = ""
    if mastery:
        weak = [m for m in mastery
                if m["total"] >= 3 and m["correct"] / m["total"] < 0.7]
        if weak:
            weak_str = ", ".join(m["concept_key"] for m in weak[:10])
            mastery_section = (
                f"── SKILLS NEEDING REINFORCEMENT (seen ≥3×, <70% accuracy) ──\n"
                f"{weak_str}\n"
                f"A `focus:\"review\"` lesson on one of these is a good choice when due.\n\n"
            )

    return (
        f"You are an expert {name} teacher planning the SINGLE next lesson for an "
        f"English speaker (proficiency goal {level_target}). One lesson, chosen now "
        f"from the learner's live state — not a fixed syllabus.\n\n"
        f"{_lang_preamble(info)}"
        f"{profile_section}"
        f"{level_section}"
        f"{mastery_section}"
        f"{deck_section}"
        f"{recent_section}"
        f"{weak_section}"
        f"── WHAT'S BEEN TAUGHT ──\n{_registry_block(concept_registry)}\n\n"
        f"{_recent_block(recent_summaries)}"
        f"{_chapter_block(current_chapter)}\n"
        f"── YOUR TASK ──\n"
        f"Pick the best next lesson. A lesson is a SATISFYING CHUNK, not one word:\n"
        f"• GRAMMAR: when a pattern has a clean regular core, teach the WHOLE core in "
        f"one lesson (e.g. all regular present-tense -er verbs at once), then schedule "
        f"the irregular / spelling-change cases as their OWN later lesson with "
        f"focus=\"exceptions\". List the specific forms/verbs to cover in target_items.\n"
        f"• VOCAB: group a themed SET of 4–8 related words into one lesson (not one at a "
        f"time). Put each word in target_items.\n"
        f"• scope=\"broad\" for a full family/set, \"narrow\" for a focused exceptions or "
        f"review lesson.\n"
        f"• Never re-teach a concept key already taught; an exceptions/review lesson must "
        f"use a NEW key (e.g. present_er_spelling).\n"
        f"• key = stable snake_case. vocab labels/target_items in NATIVE {name} SCRIPT, "
        f"citation form; glosses in English.\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{\n'
        '  "chapter_action": "continue" | "new",\n'
        '  "chapter": {"title":"<short English chapter title>","objective":"<one sentence>","summary":"<one sentence>"},\n'
        '  "skill": {"kind":"grammar"|"vocab","key":"<snake_case>","label":"<pattern name OR native theme word>","gloss":"<one-line English>"},\n'
        '  "scope": "broad" | "narrow",\n'
        '  "focus": "new" | "exceptions" | "review",\n'
        f'  "target_items": [{{"label":"<{name} native script>","gloss":"<English>"}}, ...],\n'
        '  "rationale": "<one sentence: why THIS lesson next>"\n'
        '}\n'
        "`chapter` is required only when chapter_action is \"new\" (else omit or null).\n"
        "For a GRAMMAR skill, target_items are the forms/verbs to cover; for VOCAB they "
        "are the words to teach."
    )


def _norm_items(items) -> list[dict]:
    out = []
    for it in (items or []):
        label = (it.get("label") or "").strip()
        if not label:
            continue
        out.append({"label": label, "gloss": (it.get("gloss") or "").strip()})
    return out


async def plan_next_lesson(
    target_lang: str,
    level_target: str = "A1",
    *,
    concept_registry: list[dict] | None = None,
    recent_summaries: list[dict] | None = None,
    current_chapter: dict | None = None,
    learner_profile: str = "",
    mastery: list[dict] | None = None,
    known_words: list[dict] | None = None,
    weak_words: list[dict] | None = None,
    recent_cards: list[dict] | None = None,
    cefr_spread: str = "",
    api_key: str,
    anthropic_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> dict:
    """One LLM call: decide the single next lesson from live learner state.
    Returns a normalized lesson_spec:
      {chapter_action, chapter, skill, scope, focus, target_items, rationale,
       _raw_prompt, _raw_response}."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = _build_plan_prompt(
        target_lang, level_target,
        concept_registry or [], recent_summaries or [], current_chapter,
        learner_profile=learner_profile, mastery=mastery,
        known_words=known_words, weak_words=weak_words,
        recent_cards=recent_cards, cefr_spread=cefr_spread,
    )
    raw = await llm.call(prompt, model=model, gemini_key=api_key, anthropic_key=anthropic_key)
    parsed = _parse_json(raw) or {}

    skill_in = parsed.get("skill") or {}
    skill = {
        "kind":  (skill_in.get("kind") or "vocab").strip(),
        "key":   (skill_in.get("key") or "").strip(),
        "label": (skill_in.get("label") or "").strip(),
        "gloss": (skill_in.get("gloss") or "").strip(),
    }
    action = (parsed.get("chapter_action") or "").strip().lower()
    if action not in ("continue", "new"):
        action = "new" if not current_chapter else "continue"
    ch_in = parsed.get("chapter") or {}
    chapter = {
        "title":     (ch_in.get("title") or "").strip(),
        "objective": (ch_in.get("objective") or "").strip(),
        "summary":   (ch_in.get("summary") or "").strip(),
    }
    return {
        "chapter_action": action,
        "chapter":        chapter,
        "skill":          skill,
        "scope":          (parsed.get("scope") or "broad").strip().lower(),
        "focus":          (parsed.get("focus") or "new").strip().lower(),
        "target_items":   _norm_items(parsed.get("target_items")),
        "rationale":      (parsed.get("rationale") or "").strip(),
        "_raw_prompt":    prompt,
        "_raw_response":  raw or "",
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
  {"type":"prose","text":"<English explanation — **bold** and *italic* markdown supported>"}
    → Flowing paragraph. Use for rules, patterns, conceptual explanations.
  {"type":"table","title":"<short English title>","columns":["<English header>",...],"rows":[["<cell>",...], ...]}
    → Paradigm grid rendered as a table. Ideal for pronoun charts, conjugation tables,
      classifier lists, gender/article tables. Headers in English; cells in native script
      (ruby + audio added automatically by us). Use whenever a pattern reads clearest as a grid.
  {"type":"examples","items":[{"text":"<native sentence>","gloss":"<natural English>","lit":"<word-for-word literal gloss>"},...]}
    → Example sentences shown as: native (with speaker button) / (lit. ...) / English.
      The `lit` field is powerful for grammar — include it whenever target-language word
      order diverges from English (e.g. verb-final, no copula, topic-comment, particles).
      Omit `lit` when the order matches English closely.
  {"type":"contrast","a":{"text":"<native>","gloss":"<English>"},"b":{"text":"<native>","gloss":"<English>"},"label":"<single differing feature>"}
    → Two sentences side-by-side. Use for: ✓ correct vs ✗ wrong, minimal pairs,
      near-synonyms with different nuance. Label names the single differing feature.
  {"type":"note","text":"<tip, 'Don't say X — say Y', or exception — **bold** supported>"}
    → Highlighted callout box. Use for common learner errors, important exceptions,
      'don't say...' warnings."""""


def _concepts_block(concepts: list[dict]) -> str:
    lines = []
    for c in concepts:
        kind = c.get("kind") or "vocab"
        lines.append(f'• [{kind}] {c.get("key","")} — {c.get("label","")} = {c.get("gloss","")}')
        items = c.get("items") or []
        if items:
            covered = ", ".join(
                f'{i.get("label","")}={i.get("gloss","")}' if i.get("gloss") else i.get("label", "")
                for i in items if i.get("label")
            )
            if covered:
                lines.append(f'    cover the WHOLE set in this lesson: {covered}')
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


def _review_block(review: list[dict]) -> str:
    if not review:
        return ""
    lines = "\n".join(
        f'• {c.get("key","")} — {c.get("label","")} = {c.get("gloss","")}'
        for c in review
    )
    return (
        f"── REVIEW (interleaving) ──\n"
        f"These previously-taught concepts are due for review:\n{lines}\n"
        f"In ADDITION to the drills for the new concepts, include EXACTLY "
        f"{len(review)} drill(s) whose \"concept\" is one of these review keys — "
        f"use fresh sentences/contexts (not the ones they were taught with), and "
        f"where natural COMBINE them with the new concepts. Place review drills "
        f"between the new-concept drills, not all at the end.\n\n"
    )


def _brief_block(brief: dict | None) -> str:
    """A one-line steer from the planner: the lesson's title/objective + how broad
    to go (a whole family vs. a focused exceptions/review lesson)."""
    if not brief:
        return ""
    focus = (brief.get("focus") or "new").strip()
    scope = (brief.get("scope") or "broad").strip()
    title = (brief.get("title") or "").strip()
    objective = (brief.get("objective") or "").strip()
    lines = ["── LESSON BRIEF (from the planner) ──"]
    if title:
        lines.append(f"Working title: {title}")
    if objective:
        lines.append(f"Objective: {objective}")
    lines.append(f"Scope: {scope}  ·  Focus: {focus}")
    if focus == "exceptions":
        lines.append("This is an EXCEPTIONS lesson: the regular core is already taught — "
                     "drill ONLY the irregular / spelling-change cases and contrast them "
                     "with the regular pattern.")
    elif focus == "review":
        lines.append("This is a REVIEW lesson: consolidate, mix contexts, lighter on new "
                     "teaching, heavier on varied drills.")
    elif scope == "broad":
        lines.append("This is a BROAD lesson: teach the WHOLE set/family together; drills "
                     "should span all the items, not just one.")
    return "\n".join(lines) + "\n\n"


def _build_lesson_prompt(
    target_lang: str, concepts: list[dict], recent_summaries: list[dict],
    taught: list[dict] | None = None, review: list[dict] | None = None,
    known_words: list[dict] | None = None, weak_words: list[dict] | None = None,
    brief: dict | None = None,
) -> str:
    info = LANG_INFO[target_lang]
    name = info["name"]
    taught_block = ""
    if taught:
        taught_block = f"── ALREADY TAUGHT (the learner knows these) ──\n{_registry_block(taught)}\n\n"
    deck_block = ""
    if known_words:
        deck_block = (
            f"── LEARNER'S KNOWN FLASHCARD WORDS (their SRS deck) ──\n"
            f"{_word_list_block(known_words)}\n"
            f"PREFER these when you need extra words in example sentences and drills — "
            f"they need no glossing and make the new material feel familiar.\n\n"
        )
    weak_block = ""
    if weak_words:
        weak_block = (
            f"── STRUGGLING FLASHCARD WORDS ──\n"
            f"{_word_list_block(weak_words)}\n"
            f"The learner keeps failing these in flashcard review. If one fits "
            f"naturally, work 1–2 of them into example sentences or drills (set the "
            f"drill's \"concept\" to the lesson concept it practises). Don't force it.\n\n"
        )
    n_drills = "8–12" if review else "7–10"
    return (
        f"You are an expert {name} teacher. Author ONE focused micro-lesson "
        f"(teach blocks + drills together) for an English speaker.\n\n"
        f"{_lang_preamble(info)}"
        f"{_brief_block(brief)}"
        f"{_recent_block(recent_summaries)}"
        f"{taught_block}"
        f"{deck_block}"
        f"{weak_block}"
        f"── TEACH EXACTLY THIS LESSON ({len(concepts)} concept(s); cover every listed item) ──\n{_concepts_block(concepts)}\n\n"
        f"── TEACH BLOCKS ──\n"
        f"Write a TEXTBOOK PAGE for these concepts. The learner should finish the teach "
        f"section with a thorough understanding — not just surface familiarity.\n"
        f"• GRAMMAR concepts: aim for 4–8 blocks. Open with a PROSE rule, follow with a "
        f"TABLE if there's a paradigm (pronoun charts, tone tables, aspect markers), then "
        f"EXAMPLES with `lit` fields where word order diverges from English, then at "
        f"least one NOTE about a common learner error or 'Don't say X — say Y'.\n"
        f"• VOCAB concepts: proportionate depth. Simple everyday words can debut in a "
        f"drill (gloss shown automatically) with no teach block. Reserve teach blocks for "
        f"vocab with non-obvious meaning, false friends, tricky usage, or register. A "
        f"vocab-only lesson might need just one short EXAMPLES block, or none at all.\n"
        f"The `lit` (literal, word-for-word) field in examples is the most powerful "
        f"grammar teaching tool — it shows HOW the structure works. Include it whenever "
        f"target-language word order diverges from English (e.g. verb-final sentences, "
        f"topic-comment structure, copula-less sentences, postpositions/particles).\n"
        f"Block types:\n{_BLOCK_TYPES}\n\n"
        f"{_review_block(review or [])}"
        f"── DRILLS ──\nAuthor {n_drills} drills for these concepts, in EASY → HARD order "
        f"(DO NOT shuffle — the order is intentional and the learner sees them in sequence).\n"
        f"Suggested ordering: recognition (warm-up) → listening → production → cloze → "
        f"reorder (hardest — pure recall + construction). Include at least 2 reorder "
        f"drills for grammar concepts; reorder tiles expose word-order rules better than "
        f"any other drill type.\n"
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
        f"• REORDER: `tokens` must tile the `sentence` exactly (no spaces). For helper "
        f"tokens the learner doesn't know, add `glossary` entries {{token, gloss}} "
        f"(1–2 words or POS: PRT/AUX/CONJ/CL/PREP). Don't gloss words already taught.\n"
        f"Drill kinds:\n{_DRILL_KINDS}\n\n"
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
    correct. We tile the sentence (spaces stripped) with the tokens via DFS with
    backtracking, trying longer tokens first at each position (a greedy walk
    fails when a short token shadows a longer one, e.g. tiles 好/你好 against
    你好…). Returns None if the tokens can't tile the sentence exactly (drill
    should be dropped in that case).
    """
    target = sentence.replace(" ", "")

    def walk(pos: int, remaining: list[str]) -> list[str] | None:
        if not remaining:
            return [] if pos == len(target) else None
        tried = set()
        # Longest-first so 你好 is preferred over 你 when both match.
        for tok in sorted(set(remaining), key=len, reverse=True):
            if tok in tried:
                continue
            tried.add(tok)
            if target.startswith(tok, pos):
                rest = list(remaining)
                rest.remove(tok)
                tail = walk(pos + len(tok), rest)
                if tail is not None:
                    return [tok] + tail
        return None

    return walk(0, list(tokens))


def _norm(s: str) -> str:
    """Normalise an option for duplicate detection: casefold, collapse whitespace,
    strip terminal punctuation and leading English fillers ('the cat' ≈ 'cat').
    Catches model distractors that are the correct answer in disguise."""
    s = " ".join((s or "").split()).casefold()
    s = s.rstrip(".!?。！？…,，;；:：")
    for art in ("the ", "a ", "an ", "to "):
        if s.startswith(art):
            s = s[len(art):]
            break
    return s.strip()


def _pick_options(correct: str, distractors: list[str], n: int = 4) -> tuple[list[str], int]:
    """Shuffle [correct] + de-duped distractors into n options; return (opts, idx).
    Distractors equal to the answer after normalisation are dropped — they would
    make the drill ambiguous (two arguably-correct options)."""
    seen = {_norm(correct)}
    pool = []
    for d in distractors:
        d = (d or "").strip()
        key = _norm(d)
        if d and key and key not in seen:
            seen.add(key)
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
        # Homophones are undecidable by ear: drop distractors whose romanization
        # matches the answer's (e.g. 嗰 vs 個, both go3 — free offline oracle).
        target_rom = _norm(rom(target))
        if target_rom:
            distract = [d for d in distract if _norm(rom((d or "").strip())) != target_rom]
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
        # Romanize around the blank — romanize_text would silently swallow "___".
        ex["prompt_roman"] = " ___ ".join(
            rom(part.strip()) for part in sentence.split("___")
        ).strip() if "___" in sentence else rom(sentence)
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

    # Inline construction drill for each NEW grammar concept: an interactive,
    # LLM-graded "translate this phrase" practice (rendered by the lesson player,
    # graded by /api/lesson/drill). Appended after the deterministic drills so the
    # learner meets the form in graded drills first, then produces it freely.
    for c in concepts:
        if (c.get("kind") or "vocab") != "grammar":
            continue
        label = (c.get("label") or "").strip()
        if label:
            exercises.append({
                "type": "construction_drill",
                "concept_key": (c.get("key") or "").strip(),
                "grammar": True,
                "construction": label,
            })

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
    anthropic_key: str | None = None,
    model: str = DEFAULT_MODEL,
    taught: list[dict] | None = None,
    review: list[dict] | None = None,
    known_words: list[dict] | None = None,
    weak_words: list[dict] | None = None,
    brief: dict | None = None,
) -> dict:
    """One LLM call: author teach blocks + drills for the given skill/concepts
    together, then validate/assemble. Returns lesson metadata + content + raw strings.

    `taught` — concepts the learner already knows (so the model doesn't re-teach
               them and the reorder glossary marks only genuinely-new helper words).
    `review` — previously-taught concepts to interleave as review drills (spiral
               review). They join the assembly's kinds/glossary maps but are NOT
               re-registered — the caller persists only `concepts`.
    `known_words` / `weak_words` — the learner's SRS deck (strong words to build
               with, struggling words to weave in for extra practice).
    `brief` — the planner's steer (title/objective/scope/focus) so a broad lesson
               teaches a whole family and an exceptions/review lesson behaves right.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = _build_lesson_prompt(target_lang, concepts, recent_summaries or [], taught, review,
                                  known_words=known_words, weak_words=weak_words, brief=brief)
    raw = await llm.call(prompt, model=model, gemini_key=api_key, anthropic_key=anthropic_key)
    parsed = _parse_json(raw) or {}

    content = assemble_lesson(target_lang, concepts + list(review or []), parsed)
    return {
        "title":     (parsed.get("title") or "").strip(),
        "objective": (parsed.get("objective") or "").strip(),
        "summary":   (parsed.get("summary") or "").strip(),
        "content":   content,
        "_raw_prompt":   prompt,
        "_raw_response": raw or "",
    }
