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
# showing a correct form. Judging is always LLM-based (no deterministic match
# against a pre-baked expected answer — that approach poisoned the judge with
# slash-alternatives and caused false rejections). The plan's `target` is a
# reference example that uses the construction, not a match oracle.


def test_drill_plan_normalization():
    parsed = {"items": [
        {"target": "c'est une maison", "english": "It is a house."},
        {"target": "ce sont des livres", "english": "They are books."},
    ]}
    plan = tutor._normalize_drill_plan(parsed, 4)
    assert len(plan) == 2
    assert plan[0]["english"] == "It is a house."
    assert plan[0]["target"] == "c'est une maison"

    # Backward compat: old `expected` key maps to `target`
    parsed_compat = {"items": [
        {"english": "It is a house.", "expected": "c'est une maison"},
    ]}
    plan_compat = tutor._normalize_drill_plan(parsed_compat, 4)
    assert plan_compat[0]["target"] == "c'est une maison"

    # Fallback old format (English-only, no reference)
    parsed_old = {"phrases": ["It is a house.", "They are books."]}
    plan_old = tutor._normalize_drill_plan(parsed_old, 4)
    assert len(plan_old) == 2
    assert plan_old[0]["english"] == "It is a house."
    assert plan_old[0]["target"] == ""


@pytest.mark.asyncio
async def test_drill_llm_failure_returns_null_feedback(monkeypatch):
    """When the LLM fails, feedback is None (client shows 'couldn't check')."""
    def boom(prompt, api_key, model=None):
        raise AttributeError("'NoneType' object has no attribute 'strip'")
    monkeypatch.setattr(tutor, "_call", boom)
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: "")
    items = [{"english": "a", "target": "x"}, {"english": "b", "target": "y"}]
    out = await tutor.run_lesson_drill(
        "fr", "X", answer="z", plan_items=items,
        turn=1, api_key="fake", model="m", known_words=[])
    assert out["feedback"] is None  # not falsely accepted or falsely rejected
    assert out["done"] is False
    assert out["phrase"] == "b"


@pytest.mark.asyncio
async def test_drill_llm_accepts_correct(monkeypatch):
    """LLM judges an acceptable answer as correct."""
    def fake_call(prompt, api_key, model=None):
        return '{"correct": true, "corrected": "", "alt": "", "note": ""}'
    monkeypatch.setattr(tutor, "_call", fake_call)
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: "")
    items = [{"english": "They are books.", "target": "ce sont des livres"},
             {"english": "b", "target": "y"}]
    out = await tutor.run_lesson_drill(
        "fr", "C'est", answer="ce sont des bouquins", plan_items=items,
        turn=1, api_key="fake", model="m", known_words=[])
    assert out["feedback"]["correct"] is True


@pytest.mark.asyncio
async def test_drill_llm_rejects_wrong_answer(monkeypatch):
    """LLM correctly identifies a wrong answer."""
    def fake_call(prompt, api_key, model=None):
        return '{"correct": false, "corrected": "elle est contente", "alt": "", "note": "Gender agreement."}'
    monkeypatch.setattr(tutor, "_call", fake_call)
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: "")
    items = [{"english": "She is happy.", "target": "elle est contente"},
             {"english": "b", "target": "y"}]
    out = await tutor.run_lesson_drill(
        "fr", "adj agreement", answer="elle est content", plan_items=items,
        turn=1, api_key="fake", model="m", known_words=[])
    assert out["feedback"]["correct"] is False
    assert out["feedback"]["corrected"] == "elle est contente"


@pytest.mark.asyncio
async def test_drill_llm_correct_with_alt(monkeypatch):
    """When correct but not using the construction, alt shows a version that does."""
    def fake_call(prompt, api_key, model=None):
        return '{"correct": true, "corrected": "", "alt": "ils sont prêts", "note": "You could also say: ils sont prêts"}'
    monkeypatch.setattr(tutor, "_call", fake_call)
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: "")
    items = [{"english": "Are they ready?", "target": "sont-ils prêts ?"},
             {"english": "b", "target": "y"}]
    out = await tutor.run_lesson_drill(
        "fr", "inversion", answer="est-ce qu'ils sont prêts", plan_items=items,
        turn=1, api_key="fake", model="m", known_words=[])
    assert out["feedback"]["correct"] is True
    assert out["feedback"]["alt"] == "ils sont prêts"


@pytest.mark.asyncio
async def test_drill_same_answer_override(monkeypatch):
    """If LLM says wrong but corrected matches the answer, override to correct."""
    def fake_call(prompt, api_key, model=None):
        return '{"correct": false, "corrected": "sont-ils prêts", "alt": "", "note": "Wrong"}'
    monkeypatch.setattr(tutor, "_call", fake_call)
    monkeypatch.setattr(tutor.tokenizer, "romanize_text", lambda s, lang: "")
    items = [{"english": "Are they ready?", "target": "sont-ils prêts ?"},
             {"english": "b", "target": "y"}]
    out = await tutor.run_lesson_drill(
        "fr", "inversion", answer="Sont-ils prêts?", plan_items=items,
        turn=1, api_key="fake", model="m", known_words=[])
    assert out["feedback"]["correct"] is True
