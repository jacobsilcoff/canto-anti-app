"""Tests for the tutor chat — db CRUD/isolation, prompt building, and the strict
normalization of the model's structured reply. No LLM calls (same philosophy as
test_learning.py: everything we store/grade is deterministic)."""
import os
import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import auth
import db
import tutor


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


# ── Conversations: CRUD + per-user isolation ─────────────────────────────────

@pytest.mark.asyncio
async def test_conversation_crud_and_titles(fresh_db):
    uid = fresh_db
    cid = await db.create_tutor_conversation(uid, "yue")
    convs = await db.list_tutor_conversations(uid, "yue")
    assert [c["id"] for c in convs] == [cid]
    assert convs[0]["title"] == ""

    # First user message becomes the title (clipped to 60 chars).
    await db.add_tutor_message(uid, cid, "user", "你好！我想學廣東話 " + "x" * 80)
    convs = await db.list_tutor_conversations(uid, "yue")
    assert convs[0]["title"].startswith("你好！")
    assert len(convs[0]["title"]) <= 60

    # Tutor reply doesn't overwrite the title.
    await db.add_tutor_message(uid, cid, "tutor", '{"reply":"你好！"}')
    convs = await db.list_tutor_conversations(uid, "yue")
    assert convs[0]["title"].startswith("你好！我想")

    msgs = await db.get_tutor_messages(uid, cid)
    assert [m["role"] for m in msgs] == ["user", "tutor"]

    await db.delete_tutor_conversation(uid, cid)
    assert await db.list_tutor_conversations(uid, "yue") == []
    assert await db.get_tutor_messages(uid, cid) == []


@pytest.mark.asyncio
async def test_conversations_isolated_per_user_and_lang(fresh_db):
    uid = fresh_db
    other = await db.create_user("other", auth.hash_password("pw"))
    cid = await db.create_tutor_conversation(uid, "yue")
    await db.add_tutor_message(uid, cid, "user", "hello")

    # Another user can't read, write, or delete it.
    assert await db.get_tutor_conversation(other, cid) is None
    assert await db.get_tutor_messages(other, cid) == []
    assert await db.add_tutor_message(other, cid, "user", "intrude") is None
    await db.delete_tutor_conversation(other, cid)
    assert await db.get_tutor_conversation(uid, cid) is not None

    # Conversations are per-language.
    assert await db.list_tutor_conversations(uid, "fr") == []


# ── Points ledger ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_points_ledger(fresh_db):
    uid = fresh_db
    assert await db.get_points_total(uid, "yue") == 0
    await db.add_points(uid, "yue", 2, "used 係 correctly")
    await db.add_points(uid, "yue", 3, "full sentence")
    await db.add_points(uid, "yue", 0, "ignored")      # non-positive → no-op
    await db.add_points(uid, "fr", 5, "other lang")
    assert await db.get_points_total(uid, "yue") == 5
    assert await db.get_points_total(uid, "fr") == 5
    other = await db.create_user("other2", auth.hash_password("pw"))
    assert await db.get_points_total(other, "yue") == 0


# ── Prompt building ───────────────────────────────────────────────────────────

def test_prompt_contains_context_and_contract():
    p = tutor.build_tutor_prompt(
        "yue", "我想食飯",
        [{"role": "user", "text": "你好"}, {"role": "tutor", "text": "你好！"}],
        level="A2", learner_profile="Heritage speaker, reads slowly.",
        known_words=[{"target_text": "食", "gloss": "eat"}],
        concept_registry=[{"key": "copula", "label": "係", "gloss": "to be"}],
        weak_concepts=[{"target_text": "難", "gloss": "difficult"}],
    )
    assert "Cantonese" in p
    assert "Heritage speaker" in p
    assert "食 = eat" in p                       # known deck words
    assert "copula | 係" in p                    # course registry
    assert "難 = difficult" in p                 # weak words
    assert "Learner: 你好" in p and "Tutor: 你好！" in p
    assert "Learner's new message: 我想食飯" in p
    assert '"corrections"' in p and '"new_items"' in p and '"points"' in p


def test_prompt_history_clipped():
    history = [{"role": "user", "text": f"msg{i}"} for i in range(40)]
    p = tutor.build_tutor_prompt("fr", "bonjour", history)
    assert "msg39" in p
    assert "msg5" not in p                       # beyond HISTORY_LIMIT


# ── Reply normalization ───────────────────────────────────────────────────────

def test_normalize_clips_and_recomputes_romanization(monkeypatch):
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: f"rom({s})")
    parsed = {
        "reply": "好呀！",
        "corrections": [{"quote": f"q{i}", "corrected": f"c{i}", "explanation": "e"}
                        for i in range(6)],
        "new_items": ([{"target_text": f"詞{i}", "english": "word",
                        "romanization": "MODEL-LIES", "notes": ""} for i in range(8)]
                      + [{"target_text": "", "english": "dropped"}]),
        "points": [{"concept": "係", "points": 99, "reason": "r"},
                   {"concept": "x", "points": "nan", "reason": ""},
                   {"concept": "y", "points": -2, "reason": ""},
                   {"concept": "z", "points": 2, "reason": ""},
                   {"concept": "w", "points": 1, "reason": ""}],
    }
    out = tutor._normalize(parsed, "yue", raw="")
    assert out["reply"] == "好呀！"
    assert len(out["corrections"]) == tutor.MAX_CORRECTIONS
    assert len(out["new_items"]) == tutor.MAX_NEW_ITEMS
    # Romanization always recomputed by the oracle, never trusted from the model.
    assert all(it["romanization"] == f'rom({it["target_text"]})' for it in out["new_items"])
    # Points clamped to 1–3; bad entries (non-int, non-positive) dropped without
    # consuming slots; list clipped to MAX_POINT_ITEMS valid awards.
    assert [p["points"] for p in out["points"]] == [3, 2, 1]


def test_normalize_falls_back_to_raw_text():
    out = tutor._normalize({}, "fr", raw="Bonjour ! Comment ça va ?")
    assert out["reply"] == "Bonjour ! Comment ça va ?"
    assert out["corrections"] == [] and out["new_items"] == [] and out["points"] == []


def test_normalize_no_romanization_for_latin():
    out = tutor._normalize(
        {"reply": "ok", "new_items": [{"target_text": "bonjour", "english": "hello"}]},
        "fr", raw="")
    assert out["new_items"][0]["romanization"] == ""


def test_normalize_keeps_reply_en_and_gloss():
    out = tutor._normalize(
        {"reply": "我好肚餓", "reply_en": "I'm very hungry",
         "gloss": {"我": "I", "好": "very", "肚餓": "hungry", "": "blank", "x": ""}},
        "yue", raw="")
    assert out["reply_en"] == "I'm very hungry"
    assert out["gloss"] == {"我": "I", "好": "very", "肚餓": "hungry"}   # blank entries dropped


def test_normalize_drops_new_items_user_already_used_or_knows():
    # 食 appears in the learner's own message; 飯 is in their deck — neither is
    # "new". 麵 is genuinely new and survives.
    parsed = {"reply": "好", "new_items": [
        {"target_text": "食", "english": "to eat"},
        {"target_text": "飯", "english": "rice"},
        {"target_text": "麵", "english": "noodles"},
    ]}
    out = tutor._normalize(parsed, "yue", raw="",
                           user_msg="我想食嘢", known_texts={"飯", "水"})
    assert [it["target_text"] for it in out["new_items"]] == ["麵"]


# ── Inline lesson construction-drill: resilience + verdict normalization ──────
# The drill is formative and must NEVER hard-fail or mark an answer wrong without
# showing a correct form. These guard the two failure modes that ended drills early:
# (1) the LLM call itself raising, and (2) a response with no usable `corrected`.

def test_drill_feedback_accepts_when_no_correction(monkeypatch):
    # Model returned valid JSON but no `corrected` (or an empty one): we can't show
    # a correct form, so we must NOT mark it wrong — degrade to accepted.
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: "")
    for parsed in ({}, {"correct": False, "note": "wrong"}, {"corrected": "  "}):
        fb = tutor._normalize_drill_feedback(parsed, "fr", learner_answer="c'est un livre")
        assert fb is not None
        assert fb["correct"] is True
        assert fb["corrected"] == "c'est un livre"   # echoes the learner's own answer
        assert fb["note"] == ""


def test_drill_feedback_case_punctuation_override(monkeypatch):
    # The exact hallucination: "wrong" with a corrected form differing ONLY by case
    # and a trailing period → overridden to correct, note cleared.
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: "")
    fb = tutor._normalize_drill_feedback(
        {"correct": False, "corrected": "C'est un livre.",
         "note": "Error: missing apostrophe."},
        "fr", learner_answer="c'est un livre")
    assert fb["correct"] is True
    assert fb["note"] == ""


def test_drill_feedback_keeps_real_errors(monkeypatch):
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: "")
    fb = tutor._normalize_drill_feedback(
        {"correct": False, "corrected": "elle est contente",
         "note": "Wrong gender agreement."},
        "fr", learner_answer="elle est content")
    assert fb["correct"] is False
    assert fb["corrected"] == "elle est contente"
    assert "gender" in fb["note"].lower()


@pytest.mark.asyncio
async def test_drill_judge_never_hard_fails_when_llm_raises(monkeypatch):
    # The root cause of drills ending early: a raising `_call` (e.g. empty response
    # → `.text` is None → AttributeError) propagating into a 502. It must now be
    # caught and degrade to an accepted fallback so the drill continues.
    def boom(prompt, api_key, model=None):
        raise AttributeError("'NoneType' object has no attribute 'strip'")
    monkeypatch.setattr(tutor, "_call", boom)
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: "")
    out = await tutor.run_lesson_drill(
        "fr", "C'est / Ce sont", answer="ce sont des livres",
        phrases=["It is a house.", "They are books.", "It is a car.", "They are dogs."],
        turn=2, api_key="fake", model="m", level="A1", known_words=[])
    assert out["feedback"] is not None
    assert out["feedback"]["correct"] is True
    assert out["done"] is False          # drill continues
    assert out["phrase"] == "It is a car."   # advances to the next planned phrase


@pytest.mark.asyncio
async def test_drill_judge_continues_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(tutor, "_call", lambda *a, **k: "Sorry, I can't do that.")
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: "")
    out = await tutor.run_lesson_drill(
        "fr", "X", answer="bonjour", phrases=["a", "b", "c", "d"],
        turn=1, api_key="fake", model="m", known_words=[])
    assert out["feedback"]["correct"] is True
    assert out["done"] is False
    assert out["phrase"] == "b"
