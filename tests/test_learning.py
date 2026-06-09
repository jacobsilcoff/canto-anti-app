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


def test_reorder_tokens_reordered_from_sentence():
    # The model returned tokens in wrong order ("香港人你係") but sentence correct
    # ("你係香港人"). Assembly must reorder tokens to match the sentence.
    concepts = [{"kind": "vocab", "key": "hk_person", "label": "香港人", "gloss": "Hong Kong person"}]
    authored = {"teach": [], "drills": [
        {"kind": "reorder", "concept": "hk_person", "sentence": "你係香港人",
         "tokens": ["香港人", "你", "係"]},   # wrong order from model
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    wb = next(e for e in exs if e["type"] == "word_bank")
    assert wb["answer_tokens"] == ["你", "係", "香港人"]


def test_reorder_dropped_when_tokens_dont_tile_sentence():
    # If the model's tokens can't tile the sentence exactly, drop the drill.
    concepts = [{"kind": "vocab", "key": "test", "label": "你好", "gloss": "hello"}]
    authored = {"teach": [], "drills": [
        {"kind": "reorder", "concept": "test", "sentence": "你好嗎",
         "tokens": ["你", "好"]},   # missing 嗎 — can't tile sentence
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    assert exs == []


def test_reorder_glossary_filtered_to_real_tokens():
    # The reorder glossary keeps only {token: gloss} for tokens that are actually
    # tiles and have a non-empty gloss — stray/blank entries are dropped.
    concepts = [{"kind": "vocab", "key": "iam", "label": "我係", "gloss": "I am"}]
    authored = {"teach": [], "drills": [
        {"kind": "reorder", "concept": "iam", "sentence": "我係學生",
         "tokens": ["我", "係", "學生"],
         "glossary": [
             {"token": "我", "gloss": "I"},
             {"token": "係", "gloss": "am"},
             {"token": "唔", "gloss": "not"},     # not a tile → dropped
             {"token": "學生", "gloss": ""},       # blank gloss → dropped
         ]},
    ]}
    exs = learning.assemble_lesson("yue", concepts, authored)["segments"][0]["exercises"]
    wb = next(e for e in exs if e["type"] == "word_bank")
    assert wb["glossary"] == {"我": "I", "係": "am"}


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


# ── Mastery ledger ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mastery_record_and_retrieve(fresh_db):
    uid = fresh_db
    results = [
        {"concept_key": "greeting_hello", "correct": 3, "total": 4},
        {"concept_key": "article_el_la",  "correct": 1, "total": 5},
    ]
    await db.record_concept_results(uid, "es", results)
    summary = await db.get_mastery_summary(uid, "es")
    by_key = {r["concept_key"]: r for r in summary}
    assert by_key["greeting_hello"]["correct"] == 3
    assert by_key["greeting_hello"]["total"] == 4
    assert by_key["article_el_la"]["correct"] == 1


@pytest.mark.asyncio
async def test_mastery_increments_on_second_attempt(fresh_db):
    uid = fresh_db
    await db.record_concept_results(uid, "fr", [{"concept_key": "present_er", "correct": 2, "total": 3}])
    await db.record_concept_results(uid, "fr", [{"concept_key": "present_er", "correct": 3, "total": 3}])
    summary = await db.get_mastery_summary(uid, "fr")
    assert summary[0]["correct"] == 5
    assert summary[0]["total"] == 6


@pytest.mark.asyncio
async def test_mastery_ignores_bad_rows(fresh_db):
    uid = fresh_db
    await db.record_concept_results(uid, "yue", [
        {"concept_key": "",          "correct": 1, "total": 1},   # blank key
        {"concept_key": "good_key",  "correct": 0, "total": 0},   # zero total
        {"concept_key": "valid_key", "correct": 1, "total": 2},
    ])
    summary = await db.get_mastery_summary(uid, "yue")
    assert len(summary) == 1
    assert summary[0]["concept_key"] == "valid_key"
