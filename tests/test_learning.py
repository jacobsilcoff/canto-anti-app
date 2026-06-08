"""Tests for the unit-plan-first / unified micro-lesson authoring (learning.py).

These cover the DETERMINISTIC assembly + validation — no LLM calls. The contract
that matters most: WE own the answer key (option[answer] is always the intended
correct answer), and romanization is recomputed by the offline oracle.
"""
import os
import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import auth
import db
import learning


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


# ── Assembly: answer keys are correct by construction ────────────────────────

_CONCEPTS = [
    {"kind": "grammar", "key": "articles", "label": "el / la", "gloss": "the (m/f)"},
    {"kind": "vocab", "key": "mesa", "label": "la mesa", "gloss": "the table"},
]


def _correct_option(ex):
    """The option the grader will accept as correct."""
    if ex["type"] in ("choice", "listening"):
        return ex["options"][ex["answer"]]
    if ex["type"] == "word_bank":
        return ex["answer_tokens"]
    return None


def test_assemble_answer_keys_are_correct():
    authored = {
        "intro": "intro line",
        "teach": [{"type": "prose", "text": "Spanish has gendered articles."}],
        "drills": [
            {"kind": "recognition", "concept": "mesa", "target": "la mesa",
             "gloss": "the table", "distractors": ["the book", "the door"]},
            {"kind": "production", "concept": "articles", "gloss": "the book",
             "target": "el libro", "distractors": ["la libro", "el mesa"]},
            {"kind": "listening", "concept": "mesa", "target": "la mesa",
             "gloss": "the table", "distractors": ["el libro", "la puerta"]},
            {"kind": "match", "concept": "articles",
             "pairs": [{"target": "el libro", "english": "the book"},
                       {"target": "la mesa", "english": "the table"}]},
        ],
    }
    content = learning.assemble_lesson("es", _CONCEPTS, authored)
    segs = content["segments"]
    assert len(segs) == 1
    ex = {e["type"]: e for e in segs[0]["exercises"]}
    # Recognition: prompt is the target, correct option is its English gloss.
    assert ex["choice"]["prompt"] == "la mesa" or ex["choice"]["prompt"] == "the book"
    # Every choice/listening exercise's keyed option is the intended correct one.
    by_concept = {e.get("concept_key"): e for e in segs[0]["exercises"]}
    recog = next(e for e in segs[0]["exercises"]
                 if e["type"] == "choice" and e["prompt"] == "la mesa")
    assert recog["options"][recog["answer"]] == "the table"
    prod = next(e for e in segs[0]["exercises"]
                if e["type"] == "choice" and e["prompt_lang"] == "english")
    assert prod["options"][prod["answer"]] == "el libro"
    listen = next(e for e in segs[0]["exercises"] if e["type"] == "listening")
    assert listen["options"][listen["answer"]] == "la mesa"
    assert listen["audio"] == "la mesa"


def test_assemble_drops_invalid_drills():
    authored = {
        "teach": [],
        "drills": [
            {"kind": "recognition", "concept": "mesa", "target": "la mesa"},   # no gloss
            {"kind": "cloze", "concept": "articles", "sentence": "no blank here",
             "answer": "el", "distractors": ["la"]},                           # no ___
            {"kind": "reorder", "concept": "articles", "sentence": "x",
             "tokens": ["solo"]},                                              # <2 tokens
            {"kind": "match", "concept": "articles",
             "pairs": [{"target": "el libro", "english": "the book"}]},        # <2 pairs
        ],
    }
    content = learning.assemble_lesson("es", _CONCEPTS, authored)
    assert content["segments"][0]["exercises"] == []


def test_grammar_flag_tracks_concept_kind():
    authored = {"teach": [], "drills": [
        {"kind": "recognition", "concept": "articles", "target": "el libro",
         "gloss": "the book", "distractors": ["the table", "the door"]},
        {"kind": "recognition", "concept": "mesa", "target": "la mesa",
         "gloss": "the table", "distractors": ["the book", "the door"]},
    ]}
    exs = learning.assemble_lesson("es", _CONCEPTS, authored)["segments"][0]["exercises"]
    flags = {e["concept_key"]: e["grammar"] for e in exs}
    assert flags["articles"] is True
    assert flags["mesa"] is False


def test_romanization_recomputed_for_logographic():
    # For a romanized language (Cantonese) every target gets ruby from the oracle,
    # never the model. The reorder drill carries answer_roman; choice carries roman.
    concepts = [{"kind": "vocab", "key": "hello", "label": "你好", "gloss": "hello"}]
    authored = {"teach": [], "drills": [
        {"kind": "recognition", "concept": "hello", "target": "你好",
         "gloss": "hello", "distractors": ["goodbye", "thanks"]},
        {"kind": "reorder", "concept": "hello", "sentence": "你好",
         "tokens": ["你", "好"]},
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    recog = next(e for e in exs if e["type"] == "choice")
    reorder = next(e for e in exs if e["type"] == "word_bank")
    assert recog["prompt_roman"]              # jyutping computed
    assert reorder["answer_roman"]


def test_french_cloze_uses_conjugation_oracle():
    # When the model tags a cloze with verb+person, grammar.py is authoritative:
    # the correct option must be the engine's paradigm cell, regardless of order.
    concepts = [{"kind": "grammar", "key": "present_er", "label": "-er present",
                 "gloss": "present tense of -er verbs"}]
    authored = {"teach": [], "drills": [
        {"kind": "cloze", "concept": "present_er",
         "sentence": "Nous ___ le français.", "answer": "parlons",
         "gloss": "We speak French.", "verb": "parler", "person": "nous",
         "distractors": ["parle", "parles"]},
    ]}
    exs = learning.assemble_lesson("fr", concepts, authored)["segments"][0]["exercises"]
    assert len(exs) == 1
    cloze = exs[0]
    assert cloze["type"] == "choice"
    assert cloze["options"][cloze["answer"]] == "parlons"


# ── Active unit plan persistence ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_active_plan_roundtrip(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    assert await db.get_active_plan(cid) is None
    plan = {"title": "Greetings", "concepts": [{"key": "hi"}], "cursor": 0}
    await db.set_active_plan(cid, plan)
    got = await db.get_active_plan(cid)
    assert got["title"] == "Greetings"
    assert got["cursor"] == 0
    await db.set_active_plan(cid, None)
    assert await db.get_active_plan(cid) is None
