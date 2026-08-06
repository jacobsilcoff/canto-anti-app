"""Typed free-production drills (translate / type_cloze) and their grading.

Every other gradeable drill kind shows the answer among the options, so a lesson
could be passed without ever writing the target language. These drills close
that gap, which means their grading has to be *liberal* — a learner who produced
a different-but-correct sentence must not be marked wrong.

Grading is two-stage on purpose: an offline accept-set match (free, instant,
works with no key and no network), then an LLM judgement only for answers that
miss it. The third outcome matters as much as the other two — when the grader
can't be reached the route reports `checked: false` rather than guessing.
"""
import os
import time

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DB_PATH", "/tmp/test_typed_drills.db")
os.environ.setdefault("API_KEY_ENC_KEY", Fernet.generate_key().decode())

import auth
import db
import learning
import main
import tokenizer
import tutor


def _rom(lang):
    return lambda s: tokenizer.romanize_text(s, lang) if s else ""


# ── Assembly ─────────────────────────────────────────────────────────────────

def test_translate_assembles_a_typed_exercise():
    ex = learning._assemble_drill(
        {"kind": "translate", "concept": "k", "prompt": "I eat bread",
         "answer": "Je mange du pain", "accept": ["Je mange le pain"], "hint": "-er verb"},
        "fr", {"k": "grammar"}, _rom("fr"),
    )
    assert ex["type"] == "type_answer"
    assert ex["prompt"] == "I eat bread"
    assert ex["prompt_lang"] == "english"
    assert ex["answer"] == "Je mange du pain"
    assert ex["accept"] == ["Je mange le pain"]
    assert ex["hint"] == "-er verb"
    # No options: there is nothing to pick from, which is the point.
    assert "options" not in ex


def test_accept_list_drops_forms_equal_to_the_answer():
    """A duplicate in `accept` is dead weight in the prompt sent to the judge and
    would show up as a distinct 'other valid answer' to the learner."""
    ex = learning._assemble_drill(
        {"kind": "translate", "concept": "k", "prompt": "I eat",
         "answer": "Je mange",
         "accept": ["Je mange", "JE MANGE ", "je mange.", "Je bois"]},
        "fr", {"k": "grammar"}, _rom("fr"),
    )
    assert ex["accept"] == ["Je bois"]


def test_type_cloze_assembles_and_speaks_the_filled_sentence():
    ex = learning._assemble_drill(
        {"kind": "type_cloze", "concept": "k", "sentence": "Je ___ du pain",
         "answer": "mange", "gloss": "I eat bread"},
        "fr", {"k": "grammar"}, _rom("fr"),
    )
    assert ex["type"] == "type_answer" and ex["is_cloze"] is True
    assert ex["prompt"] == "Je ___ du pain"
    assert ex["prompt_lang"] == "target"
    # Audio must be the COMPLETE sentence — playing "Je ___ du pain" is useless.
    assert ex["audio"] == "Je mange du pain"


@pytest.mark.parametrize("drill", [
    {"kind": "translate", "concept": "k", "prompt": "I eat"},              # no answer
    {"kind": "translate", "concept": "k", "answer": "Je mange"},           # no prompt
    {"kind": "type_cloze", "concept": "k", "sentence": "Je mange",
     "answer": "mange"},                                                   # no blank
])
def test_ungradeable_typed_drills_are_dropped(drill):
    assert learning._assemble_drill(drill, "fr", {"k": "grammar"}, _rom("fr")) is None


def test_cjk_typed_drill_romanizes_the_answer():
    ex = learning._assemble_drill(
        {"kind": "translate", "concept": "k", "prompt": "Hello",
         "answer": "你好"},
        "yue", {"k": "vocab"}, _rom("yue"),
    )
    # Romanization comes from the offline oracle, never the model.
    assert ex["answer_roman"].strip()


# ── Offline accept-set matching ──────────────────────────────────────────────

@pytest.mark.parametrize("typed,expected,accept,want", [
    ("Je mange",        "Je mange",  [],                  True),
    ("je mange",        "Je mange",  [],                  True),   # case
    ("  je mange.  ",   "Je mange",  [],                  True),   # space + terminal dot
    ("je  mange",       "Je mange",  [],                  True),   # collapsed whitespace
    ("j’ai",            "j'ai",      [],                  True),   # curly apostrophe
    ("je mange du pain", "Je mange",  ["je mange du pain"], True),  # via accept
    ("je bois",         "Je mange",  [],                  False),
    ("",                "Je mange",  [],                  False),
])
def test_answer_matches(typed, expected, accept, want):
    assert tutor.answer_matches(typed, expected, accept) is want


# ── Route ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_rate_limit():
    original = main.limiter.enabled
    main.limiter.enabled = False
    yield
    main.limiter.enabled = original
    main.limiter._storage.reset()


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    user_id = await db.create_user("typer", auth.hash_password("password1"))
    session = "typed-drill-test-session"
    await db.create_session(session, user_id, time.time() + 3600)
    # A key is configured for these tests so the judge path is reachable; the
    # no-key behaviour has its own test below.
    class _Access:
        api_key = "test-key"
    async def _access(_user, **_kw):
        return _Access()
    monkeypatch.setattr(main, "_resolve_gemini", _access)
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as ac:
        ac.cookies.set("session", session)
        ac._user_id = user_id
        yield ac


@pytest.mark.asyncio
async def test_exact_answer_never_calls_the_llm(client, monkeypatch):
    """The common case must be free. If this starts costing a call, several typed
    drills per lesson stops being affordable."""
    async def boom(*a, **k):
        raise AssertionError("judge_typed_answer must not run for an accept-set hit")
    monkeypatch.setattr(tutor, "judge_typed_answer", boom)

    res = await client.post("/api/lesson/check", json={
        "prompt": "I eat bread", "expected": "Je mange du pain",
        "answer": "  je mange du pain. ", "accept": [], "lang": "fr",
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {"checked": True, "correct": True, "corrected": "",
                    "note": "", "exact": True}


@pytest.mark.asyncio
async def test_a_different_valid_sentence_is_accepted_by_the_judge(client, monkeypatch):
    async def fake_judge(lang, prompt_text, expected, answer, **kw):
        assert answer == "Je prends du pain"
        return {"correct": True, "corrected": "", "corrected_roman": "",
                "alt": "", "alt_roman": "", "note": "Also natural."}
    monkeypatch.setattr(tutor, "judge_typed_answer", fake_judge)

    res = await client.post("/api/lesson/check", json={
        "prompt": "I eat bread", "expected": "Je mange du pain",
        "answer": "Je prends du pain", "lang": "fr",
    })
    body = res.json()
    assert body["checked"] is True and body["correct"] is True
    assert body["exact"] is False          # client says "that works too!"
    assert body["note"] == "Also natural."


@pytest.mark.asyncio
async def test_wrong_answer_comes_back_with_the_correction(client, monkeypatch):
    async def fake_judge(*a, **kw):
        return {"correct": False, "corrected": "Je mange du pain",
                "corrected_roman": "", "alt": "", "alt_roman": "",
                "note": "boire means to drink."}
    monkeypatch.setattr(tutor, "judge_typed_answer", fake_judge)

    body = (await client.post("/api/lesson/check", json={
        "prompt": "I eat bread", "expected": "Je mange du pain",
        "answer": "Je bois du pain", "lang": "fr",
    })).json()
    assert body["checked"] is True and body["correct"] is False
    assert body["corrected"] == "Je mange du pain"


@pytest.mark.asyncio
async def test_unreachable_grader_reports_unchecked_not_wrong(client, monkeypatch):
    """The learner may well have been right. Marking it wrong would punish them
    for our outage; marking it right would teach nothing."""
    async def fake_judge(*a, **kw):
        return None
    monkeypatch.setattr(tutor, "judge_typed_answer", fake_judge)

    body = (await client.post("/api/lesson/check", json={
        "prompt": "I eat bread", "expected": "Je mange du pain",
        "answer": "Je consomme du pain", "lang": "fr",
    })).json()
    assert body["checked"] is False
    assert body["corrected"] == "Je mange du pain"


@pytest.mark.asyncio
async def test_judge_exception_degrades_to_unchecked(client, monkeypatch):
    async def fake_judge(*a, **kw):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(tutor, "judge_typed_answer", fake_judge)

    body = (await client.post("/api/lesson/check", json={
        "prompt": "I eat bread", "expected": "Je mange du pain",
        "answer": "Je consomme du pain", "lang": "fr",
    })).json()
    assert body["checked"] is False


@pytest.mark.asyncio
async def test_empty_answer_is_rejected(client):
    res = await client.post("/api/lesson/check", json={
        "prompt": "I eat bread", "expected": "Je mange du pain",
        "answer": "   ", "lang": "fr",
    })
    assert res.status_code == 400


# ── Judge self-consistency ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_judge_overrides_a_model_that_contradicts_itself(monkeypatch):
    """A model that says "wrong" and then offers the learner's own sentence as
    the correction has contradicted itself; trust the correction."""
    async def fake_call(prompt, api_key, model):
        return {"correct": False, "corrected": "je mange du pain.",
                "note": "should be mange"}
    monkeypatch.setattr(tutor, "_drill_call", fake_call)

    fb = await tutor.judge_typed_answer(
        "fr", "I eat bread", "Je mange du pain", "Je mange du pain",
        api_key="k",
    )
    assert fb["correct"] is True
    assert fb["note"] == ""


@pytest.mark.asyncio
async def test_judge_fills_in_the_expected_answer_when_model_gives_none(monkeypatch):
    async def fake_call(prompt, api_key, model):
        return {"correct": False, "corrected": "", "note": "wrong verb"}
    monkeypatch.setattr(tutor, "_drill_call", fake_call)

    fb = await tutor.judge_typed_answer(
        "fr", "I eat bread", "Je mange du pain", "Je bois", api_key="k",
    )
    assert fb["correct"] is False
    assert fb["corrected"] == "Je mange du pain"


@pytest.mark.asyncio
async def test_no_api_key_degrades_instead_of_503ing_the_lesson(tmp_path, monkeypatch):
    """A self-hosted instance with no key (or an exhausted quota) must not turn a
    mid-lesson drill into a 503 — that reads as the lesson breaking. The
    accept-set path still grades; only the judge is unavailable."""
    from fastapi import HTTPException

    db.DB_PATH = str(tmp_path / "nokey.db")
    await db.init()
    user_id = await db.create_user("nokey", auth.hash_password("password1"))
    await db.create_session("nokey-session", user_id, time.time() + 3600)

    async def no_key(*a, **k):
        raise HTTPException(503, "Shared API key is not configured.")
    monkeypatch.setattr(main, "_resolve_gemini", no_key)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as ac:
        ac.cookies.set("session", "nokey-session")
        # Exact answer still grades — no key needed.
        exact = await ac.post("/api/lesson/check", json={
            "prompt": "I eat", "expected": "Je mange", "answer": "je mange", "lang": "fr"})
        assert exact.status_code == 200 and exact.json()["correct"] is True

        # Anything needing judgement degrades gracefully.
        other = await ac.post("/api/lesson/check", json={
            "prompt": "I eat", "expected": "Je mange", "answer": "Je prends", "lang": "fr"})
        assert other.status_code == 200, other.text
        assert other.json()["checked"] is False


# ── End-to-end through the real assembler ────────────────────────────────────

def test_typed_drills_survive_full_lesson_assembly():
    """_assemble_drill working in isolation isn't enough — the drill has to make
    it through assemble_lesson's step mapping, dedup and tier tagging, which is
    what the player actually reads."""
    authored = {
        "title": "Present tense -er", "objective": "Use -er verbs",
        "intro": "Let's go", "summary": "-er verbs",
        "steps": [
            {"title": "Warm-up", "teach": [{"type": "prose", "text": "Regular -er verbs."}],
             "drills": [{"kind": "recognition", "concept": "er", "tier": "core",
                         "target": "manger", "gloss": "to eat", "distractors": ["to drink"]}]},
            {"title": "Produce", "teach": [], "drills": [
                {"kind": "translate", "concept": "er", "tier": "core",
                 "prompt": "I eat bread", "answer": "Je mange du pain",
                 "accept": ["Je mange le pain"]},
                {"kind": "type_cloze", "concept": "er", "tier": "standard",
                 "sentence": "Tu ___ du pain", "answer": "manges",
                 "gloss": "You eat bread"},
            ]},
        ],
        "vocab_glossary": {"manger": "to eat"},
    }
    concepts = [{"key": "er", "label": "-er verbs", "kind": "grammar",
                 "gloss": "regular -er present", "items": []}]

    out = learning.assemble_lesson("fr", concepts, authored)
    typed = [e for s in out["segments"] for e in s.get("exercises", [])
             if e["type"] == "type_answer"]
    assert len(typed) == 2
    # Tiers must survive: _trimForLength serves 'quick' as core-only, so a typed
    # drill tagged core is the one a short run keeps.
    assert {t["tier"] for t in typed} == {"core", "standard"}
    assert typed[0]["accept"] == ["Je mange le pain"]


# ── Feedback must be readable by the learner who just erred ──────────────────

@pytest.mark.parametrize("note,kept", [
    ("boire means to drink, not to eat.", True),
    ('X means "sold out" — 賣晒 is the set phrase here.', True),   # English quoting target
    ("「埋晒」有「全部收集/關閉」的意思，與「賣晒」（售罄）意思不同。", False),
    ("これは違います。「売り切れ」を使います。", False),
    ("", False),
])
def test_non_english_notes_are_dropped(note, kept):
    """Asked to be a Cantonese tutor, the model will explain the mistake in
    Cantonese — unreadable to exactly the beginner who made it. The prompts ask
    for English; this is the deterministic backstop for when they're ignored."""
    assert bool(tutor._english_note(note)) is kept


@pytest.mark.asyncio
async def test_judge_drops_a_target_language_explanation(monkeypatch):
    async def fake_call(prompt, api_key, model):
        return {"correct": False, "corrected": "賣晒",
                "note": "「埋晒」同「賣晒」意思唔同，唔啱語境。"}
    monkeypatch.setattr(tutor, "_drill_call", fake_call)

    fb = await tutor.judge_typed_answer(
        "yue", "唔好意思，啲龍蝦___喇。", "賣晒", "埋晒",
        is_cloze=True, api_key="k",
    )
    assert fb["correct"] is False
    assert fb["corrected"] == "賣晒"      # the ANSWER stays in the target language
    assert fb["note"] == ""               # the EXPLANATION is dropped, not shown


def test_judge_prompts_demand_english_explanations():
    """Belt and braces: the instruction has to actually be in the prompt, or the
    backstop above is doing all the work and most notes get thrown away."""
    typed = tutor.build_typed_answer_judge_prompt(
        "yue", "the lobsters are sold out", "賣晒", "埋晒", is_cloze=False)
    assert "ENGLISH" in typed
    drill = tutor.build_lesson_drill_judge_prompt(
        "yue", "comparative with 過", "I am taller", "我高過佢", "", level="A1")
    assert "ENGLISH" in drill
