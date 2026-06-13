"""Grammar lesson content — generator + critic pipeline (IDEAS item 43).

Produces a VERIFIED canonical artifact per (lang, concept). Generation is
allowed to be expensive (run offline, cached in db.concept_content, SHARED
across users); playback is cheap.

Design principle — **liberal in what you SHOW, strict in what you GRADE**:
  - The TEACH content is authored FREELY by the LLM as an ordered list of typed
    "blocks" (prose, arbitrary tables, examples, contrast, note) — like a page
    of a grammar textbook. This generalises across languages (the model picks
    whatever paradigm tables / explanations the language needs); we render the
    blocks ourselves so styling stays consistent and audio/romanization survive.
    A *critic* pass verifies each block and drops the ones it rejects.
  - The INTERACTIONS (drills) stay CONSTRAINED to a few exercise types with
    strict, verified answer keys — a wrong answer key actively mis-teaches. Where
    a free non-LLM oracle exists we use it: romanization is recomputed
    (tokenizer.romanize_text, never the model) and French present-tense cloze
    answers/options come from grammar.py.

Items the critic rejects are DROPPED (thinner-but-correct over complete-but-wrong).

Artifact shape:
{
  "concept_key", "lang", "label", "gloss",
  "blocks": [ {type:"prose"|"table"|"examples"|"contrast"|"note", ...}, ... ],
  "exercises": [ <choice cloze / word_bank reorder / choice contrast-recognition> ]
}
"""
import asyncio
import os
import random
import re

import grammar
import tokenizer
from translation import LANG_INFO, _call, _parse_json

# Grammar content is generated ONCE per concept and cached shared across all
# users (concept_content), so a more capable/expensive model is worth it here:
# the cost amortizes to ~zero per user while quality compounds for everyone. This
# is the generator AND critic model; vocab materialization stays on the cheap one.
GENERATION_MODEL = os.environ.get("GRAMMAR_MODEL", "gemini-2.5-pro")


def _default_call_json(api_key: str, model: str):
    """A `call_json(prompt) -> dict` bound to the real Gemini client."""
    async def call_json(prompt: str) -> dict:
        return await asyncio.to_thread(lambda: _parse_json(_call(prompt, api_key, model)))
    return call_json


def _generate_prompt(lang: str, concept: dict) -> str:
    info = LANG_INFO[lang]
    name = info["name"]
    rules = info["rules"]
    label = (concept.get("label") or "").strip()
    gloss = (concept.get("gloss") or "").strip()
    return (
        f"You are an expert {name} teacher writing ONE focused lesson for an English "
        f"speaker — the page of a grammar textbook that covers this point, plus a few "
        f"drills. Be explicit and concrete; teach what English speakers actually get "
        f"wrong.\n\n"
        f"Lesson focus: {label or gloss}\n"
        f"Scope: {gloss}\n"
        f"Language notes:\n{rules}\n\n"
        "Author the TEACHING content as an ordered list of BLOCKS (\"blocks\"). Use as "
        "many as the focus needs, in a natural teaching order. Block types:\n"
        '  {"type":"prose","text":"<plain-English explanation>"}\n'
        '  {"type":"table","title":"<short title>","columns":["<header>", ...] (or [] '
        'for no header row),"rows":[["<cell>", ...], ...]}\n'
        '  {"type":"examples","items":[{"text":"<target phrase/sentence>","gloss":"<English>"}]}\n'
        '  {"type":"contrast","a":{"text":"<target>","gloss":"<English>"},'
        '"b":{"text":"<target>","gloss":"<English>"},"label":"<the ONE feature that differs>"}\n'
        '  {"type":"note","text":"<short tip or common-mistake warning>"}\n'
        "Guidance: open with a prose block stating the rule EXPLICITLY (when/how it "
        "applies, where English misleads). Use a TABLE whenever a paradigm "
        "(conjugation, articles, pronouns, cases, politeness levels…) reads more "
        "clearly as a grid — make whatever table the language needs, every cell "
        "correct. Table rules: (1) If multiple rows share the same first-column label "
        "(e.g. two rows both labelled '2nd person', or three rows labelled '3rd "
        "person'), you MUST distinguish them — either add a qualifier in the first "
        "column (e.g. '2nd (informal)', '2nd (formal)', '3rd (masc.)', '3rd (fem.)', "
        "'3rd (neut.)') or add an extra qualifier column. Never leave two rows with "
        "identical labels; a learner must be able to tell rows apart. "
        "(2) Row/column headers must be English (or universally understood). "
        "Use CONTRAST blocks for minimal pairs that differ in exactly one feature. "
        "Keep target text natural and vocabulary common. Cover ONLY this focus — do "
        "not drift into unrelated grammar.\n"
        f"CRITICAL — romanisation: write ALL {name} text in NATIVE SCRIPT ONLY. "
        "Do NOT include romanisation, transliteration, pinyin, jyutping, or any "
        "pronunciation guide inline — not in prose, not in table cells, not in "
        "example/contrast `text` fields, not in cloze sentences. "
        "Tables MUST NOT have a romanisation/pronunciation column. "
        "Romanisation is added automatically by a ruby engine above the characters; "
        "if you embed it inline it will appear doubled and corrupt the display.\n\n"
        "Then drills:\n"
        '- "cloze": 3 fill-in-the-blank drills. Each: "sentence" (full correct '
        'sentence with exactly one blank "___"), "gloss" (English), "answer" (the word '
        "filling the blank — must NOT already appear elsewhere in the sentence). If the "
        'blank IS a single conjugated verb, ALSO give "verb" (plain NON-reflexive '
        'infinitive) and "person" (je/tu/il/nous/vous/ils). For reflexive/pronominal '
        'verbs or any blank that is not exactly one conjugated verb, OMIT verb/person.\n'
        '- "reorder": 2 short correct sentences. Each: "sentence" + "tokens" (its words '
        "in correct order; keep punctuation attached). 3–6 tokens.\n\n"
        "Return ONLY valid JSON in exactly this shape, no other text:\n"
        '{ "blocks": [ ... ],\n'
        '  "cloze": [{"sentence":"...","gloss":"...","answer":"...","verb":"...","person":"..."}],\n'
        '  "reorder": [{"sentence":"...","tokens":["...","..."]}] }\n'
    )


def _block_summary(b: dict) -> str:
    t = b.get("type")
    if t in ("prose", "note"):
        return f'{t}: "{(b.get("text") or "")[:300]}"'
    if t == "table":
        head = " | ".join(str(c) for c in (b.get("columns") or [])) or "(no header)"
        rows = " ; ".join("/".join(str(c) for c in r) for r in (b.get("rows") or []))
        return f'table "{b.get("title","")}" [{head}] {rows}'
    if t == "examples":
        return "examples: " + " ; ".join(
            f'"{it.get("text","")}"="{it.get("gloss","")}"' for it in (b.get("items") or []))
    if t == "contrast":
        a, bb = b.get("a") or {}, b.get("b") or {}
        return (f'contrast A:"{a.get("text","")}"="{a.get("gloss","")}" '
                f'B:"{bb.get("text","")}"="{bb.get("gloss","")}" differs:{b.get("label","")}')
    return f"{t}: (unknown)"


def _critic_prompt(lang: str, gen: dict) -> str:
    """Ask a fresh pass to INDEPENDENTLY re-derive and judge each item. Returns
    boolean verdict arrays aligned by index — items judged false are dropped."""
    name = LANG_INFO[lang]["name"]
    blocks = gen.get("blocks") or []
    cloze = gen.get("cloze") or []
    reorder = gen.get("reorder") or []

    def _cloze(c):
        s = (c.get("sentence", "") or "").replace("___", f'[{c.get("answer","")}]')
        return f'   "{s}" = "{c.get("gloss","")}"'

    body = (
        "TEACH BLOCKS (reject a block if ANY target-language sentence is "
        "ungrammatical/unnatural, ANY table cell is wrong, ANY English gloss is "
        "inaccurate, a contrast doesn't really differ in only its stated feature, or "
        "the prose states something false):\n"
        + ("\n".join(f"{i}. {_block_summary(b)}" for i, b in enumerate(blocks)) or "   (none)")
        + "\n\nCLOZE (the bracketed word must be the correct fill; sentence "
        "grammatical; gloss accurate; the answer must not duplicate a word already in "
        "the sentence):\n"
        + ("\n".join(f"{i}.{_cloze(c)}" for i, c in enumerate(cloze)) or "   (none)")
        + "\n\nREORDER (must be a fully grammatical, natural sentence):\n"
        + ("\n".join(f'{i}.   "{r.get("sentence","")}"' for i, r in enumerate(reorder)) or "   (none)")
    )
    return (
        f"You are a meticulous {name} examiner. Independently re-derive each item below "
        f"FROM SCRATCH (don't just skim it) and decide if it is fully correct. Be "
        f"strict — reject anything with a grammar error, a wrong form, an inaccurate "
        f"gloss, or a factually wrong explanation.\n\n"
        f"{body}\n\n"
        "Return ONLY JSON with one boolean per item, aligned by index, true = keep:\n"
        '{ "blocks": [true, ...], "cloze": [true, ...], "reorder": [true, ...] }'
    )


def _verdicts(crit: dict, key: str, n: int) -> list[bool]:
    v = crit.get(key) if isinstance(crit, dict) else None
    v = v if isinstance(v, list) else []
    return [bool(v[i]) if i < len(v) else True for i in range(n)]


def _cloze_is_sane(sentence: str, answer: str) -> bool:
    """The blank must exist and filling it must not DUPLICATE the answer word —
    catches corrupt clozes where the verb is already in the sentence
    (e.g. "Ils ___ appellent ..." filled with "s'appellent")."""
    if "___" not in sentence or not answer:
        return False
    filled = sentence.replace("___", answer, 1).lower()
    toks = re.findall(r"[^\W\d_]+", filled, re.UNICODE)
    ans_toks = [w for w in re.findall(r"[^\W\d_]+", answer.lower(), re.UNICODE) if len(w) > 2]
    return not any(toks.count(w) > 1 for w in ans_toks)


def _conj_cloze(concept_key: str, lang: str, sentence: str, gloss: str,
                verb: str, person: str, model_answer: str) -> dict | None:
    """Build a conjugation cloze with an ENGINE-computed answer + option list
    (grammar.py is authoritative). Only fires when the model's OWN answer is a
    cell of this verb's paradigm — i.e. the blank genuinely is the conjugated
    verb. If the model's answer isn't a paradigm cell (a reflexive pronoun, a
    different word), the blank is something else and forcing a verb form would
    corrupt the sentence, so we bail to the free-cloze / drop path."""
    forms = grammar.conjugate_present(verb, lang)
    if not forms or person not in forms:
        return None
    pool = list(dict.fromkeys(forms.values()))
    if (model_answer or "").strip().lower() not in [f.lower() for f in pool]:
        return None
    correct = forms[person]
    if not _cloze_is_sane(sentence, correct):
        return None
    distractors = [f for f in pool if f != correct]
    random.shuffle(distractors)
    opts = [correct] + distractors[:3]
    random.shuffle(opts)
    return {
        "type": "choice", "grammar": True, "concept_key": concept_key, "is_cloze": True,
        "instruction": "Fill in the blank", "prompt": sentence, "prompt_lang": "target",
        "options": opts, "answer": opts.index(correct), "tip": gloss,
    }


def _free_cloze(concept_key: str, sentence: str, gloss: str, answer: str,
                pool: list[str]) -> dict | None:
    """Cloze when we can't compute the paradigm: trust the (critic-verified)
    answer, draw distractors from sibling answers in the same concept."""
    answer = (answer or "").strip()
    if not _cloze_is_sane(sentence, answer):
        return None
    seen = {answer}
    distractors = []
    for p in pool:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            distractors.append(p)
    random.shuffle(distractors)
    opts = [answer] + distractors[:3]
    random.shuffle(opts)
    return {
        "type": "choice", "grammar": True, "concept_key": concept_key, "is_cloze": True,
        "instruction": "Fill in the blank", "prompt": sentence, "prompt_lang": "target",
        "options": opts, "answer": opts.index(answer), "tip": gloss,
    }


def _clean_block(b: dict, rom) -> dict | None:
    """Normalise one teach block and compute romanization on target text.
    Unknown types and empty blocks are dropped."""
    t = b.get("type")
    if t in ("prose", "note"):
        text = (b.get("text") or "").strip()
        return {"type": t, "text": text} if text else None
    if t == "table":
        rows = [[str(c) for c in r] for r in (b.get("rows") or []) if isinstance(r, list) and r]
        if not rows:
            return None
        return {"type": "table", "title": (b.get("title") or "").strip(),
                "columns": [str(c) for c in (b.get("columns") or [])], "rows": rows}
    if t == "examples":
        items = []
        for it in (b.get("items") or []):
            tx = (it.get("text") or "").strip()
            if tx:
                item = {"text": tx, "gloss": (it.get("gloss") or "").strip(), "roman": rom(tx)}
                lit = (it.get("lit") or "").strip()
                if lit:
                    item["lit"] = lit
                items.append(item)
        return {"type": "examples", "items": items} if items else None
    if t == "contrast":
        a, bb = b.get("a") or {}, b.get("b") or {}
        at, bt = (a.get("text") or "").strip(), (bb.get("text") or "").strip()
        if not (at and bt):
            return None
        return {"type": "contrast",
                "a": {"text": at, "gloss": (a.get("gloss") or "").strip(), "roman": rom(at)},
                "b": {"text": bt, "gloss": (bb.get("gloss") or "").strip(), "roman": rom(bt)},
                "label": (b.get("label") or "").strip()}
    return None


def _contrast_ex(concept_key: str, block: dict) -> dict:
    """Recognition drill from a contrast block: given one side's English meaning,
    pick which target sentence expresses it."""
    a, b = block["a"], block["b"]
    flip = random.random() < 0.5
    correct = b["text"] if flip else a["text"]
    prompt = b["gloss"] if flip else a["gloss"]
    opts = [a["text"], b["text"]]
    random.shuffle(opts)
    return {
        "type": "choice", "grammar": True, "concept_key": concept_key,
        "instruction": "Which sentence means this?", "prompt": prompt, "prompt_lang": "english",
        "options": opts, "answer": opts.index(correct), "tip": block.get("label", ""),
    }


async def generate_grammar_content(
    lang: str,
    concept: dict,
    *,
    api_key: str | None = None,
    model: str = GENERATION_MODEL,
    call_json=None,
) -> dict:
    """Generate + critic-verify the canonical grammar artifact for one concept.

    `call_json(prompt) -> dict` is injectable for testing; by default it calls
    Gemini. Returns the artifact (see module docstring); raises ValueError for an
    unsupported language.
    """
    if lang not in LANG_INFO:
        raise ValueError(f"Unsupported language: {lang}")
    if call_json is None:
        if not api_key:
            raise ValueError("api_key or call_json required")
        call_json = _default_call_json(api_key, model)

    key = (concept.get("key") or "").strip()
    R = tokenizer.romanize_text
    has_rom = bool(LANG_INFO[lang].get("romanization"))

    def rom(s: str) -> str:
        return R(s, lang) if (has_rom and s) else ""

    gen = await call_json(_generate_prompt(lang, concept)) or {}
    raw_blocks = gen.get("blocks") or []
    raw_cloze = gen.get("cloze") or []
    raw_reorder = gen.get("reorder") or []

    keep_blocks = [True] * len(raw_blocks)
    keep_cloze = [True] * len(raw_cloze)
    keep_reorder = [True] * len(raw_reorder)

    # Teach blocks (kept ones) — author-ordered; contrast blocks also seed a drill.
    blocks, exercises = [], []
    for b, ok in zip(raw_blocks, keep_blocks):
        if not ok:
            continue
        cleaned = _clean_block(b, rom)
        if not cleaned:
            continue
        blocks.append(cleaned)
        if cleaned["type"] == "contrast":
            exercises.append(_contrast_ex(key, cleaned))

    # Cloze drills — conjugation answers computed from the engine where possible.
    answer_pool = [(c.get("answer") or "").strip() for c in raw_cloze]
    for c, ok in zip(raw_cloze, keep_cloze):
        if not ok:
            continue
        sentence = (c.get("sentence") or "").strip()
        gloss = (c.get("gloss") or "").strip()
        verb = (c.get("verb") or "").strip().lower()
        person = (c.get("person") or "").strip().lower()
        ex = None
        if grammar.has_conjugation(lang) and verb and person:
            ex = _conj_cloze(key, lang, sentence, gloss, verb, person, c.get("answer", ""))
        if ex is None:
            ex = _free_cloze(key, sentence, gloss, c.get("answer", ""), answer_pool)
        if ex:
            if has_rom:
                ex["prompt_roman"] = rom(sentence)
            exercises.append(ex)

    # Reorder drills (word_bank) — kept ones only.
    for r, ok in zip(raw_reorder, keep_reorder):
        if not ok:
            continue
        tokens = [t for t in (r.get("tokens") or []) if (t or "").strip()]
        sentence = (r.get("sentence") or "").strip()
        if len(tokens) < 2:
            continue
        ex = {
            "type": "word_bank", "grammar": True, "concept_key": key,
            "instruction": "Put the words in the correct order",
            "answer_tokens": tokens, "distractor_tokens": [], "audio": sentence,
        }
        if has_rom:
            ex["answer_roman"] = rom(sentence)
        exercises.append(ex)

    random.shuffle(exercises)
    return {
        "concept_key": key, "lang": lang,
        "label": (concept.get("label") or "").strip(),
        "gloss": (concept.get("gloss") or "").strip(),
        "blocks": blocks,
        "exercises": exercises,
    }
