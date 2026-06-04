"""Grammar-first lesson content — generator + critic pipeline (IDEAS item 43).

Produces a VERIFIED canonical artifact per (lang, concept). Generation is
allowed to be expensive (run offline, cached in db.concept_content, SHARED
across users); playback is cheap. A *generator* LLM writes the explicit rule,
minimal pairs, and drills; a *critic* LLM independently re-derives and verifies
each item (grammaticality, accurate translation, correct answer key). Items the
critic rejects are DROPPED — the lesson keeps the rest (decision: thinner but
correct beats complete but wrong).

Where we have a free, NON-LLM oracle we cross-check deterministically, breaking
the generator/critic shared-blind-spot problem exactly where LLMs slip most:
  - romanization → our offline romanizers (tokenizer.romanize_text), never the LLM
  - French present-tense forms → grammar.py (answer key + option list computed,
    not trusted from the model)

Artifact shape (every target string verified, every roman computed):
{
  "concept_key": "...", "lang": "fr", "label": "...", "gloss": "...",
  "explain": "<explicit English rule>",
  "minimal_pairs": [
     {"a_text","a_gloss","a_roman","b_text","b_gloss","b_roman","contrast"}
  ],
  "exercises": [ <choice cloze / word_bank reorder / choice minimal-pair> ]
}
The artifact carries `explain` + `minimal_pairs` for the TEACH screen and a list
of ready-to-play `exercises` (reusing the existing choice / word_bank renderers).
"""
import asyncio
import random
import re

import grammar
import tokenizer
from translation import LANG_INFO, DEFAULT_MODEL, _call, _parse_json


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
        f"You are an expert {name} grammar teacher writing ONE explicit grammar "
        f"lesson for an English speaker. Teach grammar the way a good textbook does: "
        f"state the rule plainly, then show it with minimal pairs and drills.\n\n"
        f"Grammar point: {label or gloss}\n"
        f"Meaning / scope: {gloss}\n"
        f"Language notes:\n{rules}\n\n"
        "Produce, as JSON:\n"
        '- "explain": 2–4 sentences of plain English stating the rule EXPLICITLY '
        "(when/how it applies, and where it differs from English — focus on what an "
        "English speaker would get wrong). No fluff.\n"
        '- "minimal_pairs": 2 pairs of short sentences that differ in EXACTLY ONE '
        "feature illustrating this rule (e.g. singular vs plural subject, present vs "
        "past). Each pair: a_text, a_gloss (English), b_text, b_gloss, and contrast "
        "(one phrase naming the single feature that differs). Keep vocabulary very "
        "common and reuse the same words across the pair so ONLY the grammar changes.\n"
        '- "cloze": 3 fill-in-the-blank sentences drilling this rule. Each: '
        '"sentence" (the full correct sentence with exactly one blank written as '
        '"___"), "gloss" (English), "answer" (the word that fills the blank — it must '
        'NOT already appear elsewhere in the sentence). If the blank IS a single '
        'conjugated verb, ALSO give "verb" (the plain, NON-reflexive infinitive) and '
        '"person" (one of je/tu/il/nous/vous/ils). For reflexive/pronominal verbs '
        "(s'appeler, se lever) or any blank that isn't exactly one conjugated verb, "
        'OMIT "verb"/"person" and just give the full "answer". Use simple vocabulary.\n'
        '- "reorder": 2 short correct sentences for a word-ordering drill. Each: '
        '"sentence" (the full correct sentence) and "tokens" (its words in correct '
        "order as an array — split on words, keep punctuation attached). 3–6 tokens.\n"
        '- "tables" (OPTIONAL, usually omit): include a small reference table ONLY if '
        "it genuinely clarifies a PARADIGM other than a single verb's conjugation "
        "(e.g. definite articles by gender/number, subject pronouns, noun endings). Do "
        "NOT make a verb-conjugation table — the app inserts a verified one "
        'automatically. Each table: {"title": "...", "columns": [header labels] (or [] '
        'for no header row), "rows": [["cell", ...], ...]}. Keep tables ≤6 rows, every '
        "cell correct.\n\n"
        "Every target sentence must be fully grammatical and natural. Return ONLY "
        "valid JSON in exactly this shape, no other text:\n"
        "{\n"
        '  "explain": "...",\n'
        '  "minimal_pairs": [{"a_text":"...","a_gloss":"...","b_text":"...","b_gloss":"...","contrast":"..."}],\n'
        '  "cloze": [{"sentence":"...","gloss":"...","answer":"...","verb":"...","person":"..."}],\n'
        '  "reorder": [{"sentence":"...","tokens":["...","..."]}],\n'
        '  "tables": [{"title":"...","columns":["..."],"rows":[["...","..."]]}]\n'
        "}\n"
    )


def _critic_prompt(lang: str, gen: dict) -> str:
    """Ask a fresh pass to INDEPENDENTLY re-derive and judge each item. Returns
    boolean verdict arrays aligned by index — items judged false are dropped."""
    name = LANG_INFO[lang]["name"]
    pairs = gen.get("minimal_pairs") or []
    cloze = gen.get("cloze") or []
    reorder = gen.get("reorder") or []
    tables = gen.get("tables") or []

    def _pair(p):
        return f'   A: "{p.get("a_text","")}" = "{p.get("a_gloss","")}" | B: "{p.get("b_text","")}" = "{p.get("b_gloss","")}" | differs in: {p.get("contrast","")}'

    def _cloze(c):
        s = (c.get("sentence", "") or "").replace("___", f'[{c.get("answer","")}]')
        return f'   "{s}" = "{c.get("gloss","")}"'

    def _reorder(r):
        return f'   "{r.get("sentence","")}"'

    def _table(t):
        head = " | ".join(str(c) for c in (t.get("columns") or [])) or "(no header)"
        rows = " ; ".join("/".join(str(c) for c in r) for r in (t.get("rows") or []))
        return f'   "{t.get("title","")}" [{head}] {rows}'

    body = (
        "MINIMAL PAIRS (each must be two grammatical sentences whose English glosses "
        "are accurate AND that really differ in only the stated feature):\n"
        + ("\n".join(f"{i}.{_pair(p)}" for i, p in enumerate(pairs)) or "   (none)")
        + "\n\nCLOZE (the bracketed word must be the correct fill; sentence grammatical; "
        "gloss accurate):\n"
        + ("\n".join(f"{i}.{_cloze(c)}" for i, c in enumerate(cloze)) or "   (none)")
        + "\n\nREORDER (must be a fully grammatical, natural sentence):\n"
        + ("\n".join(f"{i}.{_reorder(r)}" for i, r in enumerate(reorder)) or "   (none)")
        + "\n\nTABLES (EVERY cell must be correct; reject the whole table if any cell "
        "is wrong):\n"
        + ("\n".join(f"{i}.{_table(t)}" for i, t in enumerate(tables)) or "   (none)")
    )
    return (
        f"You are a meticulous {name} grammar examiner. Independently re-derive each "
        f"item below FROM SCRATCH (don't just skim it) and decide if it is fully "
        f"correct: grammatical, naturally phrased, the English gloss accurate, and "
        f"(for cloze) the bracketed answer the correct, agreeing form that fits where "
        f"the blank is. Be strict — reject anything with an agreement error, a wrong "
        f"form, a mistranslation, OR a cloze whose bracketed answer duplicates a word "
        f"already in the sentence or doesn't grammatically fit the blank.\n\n"
        f"{body}\n\n"
        "Return ONLY JSON with one boolean per item, aligned by index, true = keep:\n"
        '{ "minimal_pairs": [true, ...], "cloze": [true, ...], "reorder": [true, ...], "tables": [true, ...] }'
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
        "type": "choice", "grammar": True, "concept_key": concept_key,
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
        "type": "choice", "grammar": True, "concept_key": concept_key,
        "instruction": "Fill in the blank", "prompt": sentence, "prompt_lang": "target",
        "options": opts, "answer": opts.index(answer), "tip": gloss,
    }


def _verb_from_concept(concept: dict) -> str:
    """The conjugable infinitive a grammar concept teaches (the curriculum puts it
    in `label`), or "". Used to attach an engine-computed conjugation table."""
    cand = re.sub(r"\s*\([^)]*\)\s*", "", concept.get("label", "") or "").strip().lower()
    return cand if cand and grammar.conjugate_present(cand) else ""


def _clean_table(t: dict) -> dict | None:
    rows = [[str(c) for c in r] for r in (t.get("rows") or []) if isinstance(r, list) and r]
    if not rows:
        return None
    return {"title": (t.get("title") or "").strip(),
            "columns": [str(c) for c in (t.get("columns") or [])], "rows": rows}


def _minimal_pair_ex(concept_key: str, pair: dict) -> dict:
    """A recognition drill reinforcing the contrast: given one sentence's English
    meaning, pick which of the two target sentences expresses it."""
    flip = random.random() < 0.5
    correct = pair["b_text"] if flip else pair["a_text"]
    prompt = pair["b_gloss"] if flip else pair["a_gloss"]
    opts = [pair["a_text"], pair["b_text"]]
    random.shuffle(opts)
    return {
        "type": "choice", "grammar": True, "concept_key": concept_key,
        "instruction": "Which sentence means this?", "prompt": prompt, "prompt_lang": "english",
        "options": opts, "answer": opts.index(correct), "tip": pair.get("contrast", ""),
    }


async def generate_grammar_content(
    lang: str,
    concept: dict,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
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

    gen = await call_json(_generate_prompt(lang, concept)) or {}
    raw_pairs = gen.get("minimal_pairs") or []
    raw_cloze = gen.get("cloze") or []
    raw_reorder = gen.get("reorder") or []

    raw_tables = gen.get("tables") or []

    crit = await call_json(_critic_prompt(lang, gen)) or {}
    keep_pairs = _verdicts(crit, "minimal_pairs", len(raw_pairs))
    keep_cloze = _verdicts(crit, "cloze", len(raw_cloze))
    keep_reorder = _verdicts(crit, "reorder", len(raw_reorder))
    keep_tables = _verdicts(crit, "tables", len(raw_tables))

    def rom(s: str) -> str:
        return R(s, lang) if (has_rom and s) else ""

    # Minimal pairs (kept ones) → teach contrast blocks + a recognition drill each.
    pairs, exercises = [], []
    for p, ok in zip(raw_pairs, keep_pairs):
        a, b = (p.get("a_text") or "").strip(), (p.get("b_text") or "").strip()
        if not (ok and a and b):
            continue
        pair = {
            "a_text": a, "a_gloss": (p.get("a_gloss") or "").strip(), "a_roman": rom(a),
            "b_text": b, "b_gloss": (p.get("b_gloss") or "").strip(), "b_roman": rom(b),
            "contrast": (p.get("contrast") or "").strip(),
        }
        pairs.append(pair)
        exercises.append(_minimal_pair_ex(key, pair))

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

    # Tables: an ENGINE-computed conjugation table first (authoritative — never
    # the LLM), then any OTHER paradigm tables the critic approved.
    tables = []
    verb = _verb_from_concept(concept)
    if verb:
        ct = grammar.conjugation_table(verb, lang)
        if ct:
            tables.append(ct)
    for t, ok in zip(raw_tables, keep_tables):
        cleaned = _clean_table(t) if ok else None
        if cleaned:
            tables.append(cleaned)

    return {
        "concept_key": key, "lang": lang,
        "label": (concept.get("label") or "").strip(),
        "gloss": (concept.get("gloss") or "").strip(),
        "explain": (gen.get("explain") or "").strip(),
        "minimal_pairs": pairs,
        "tables": tables,
        "exercises": exercises,
    }
