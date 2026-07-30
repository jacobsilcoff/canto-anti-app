"""Lesson redesign — the "Later" nice-to-haves (B4 lightning, B5 streak freeze,
B6 practice hub, C4 listening variants, D4 per-lesson feedback).

C4 is a pure client-side transform (learn.html); B4's assembly is client-side
too. What has server logic here: B5 (earn/consume freeze), B6 (practice-pool
recombination + mistakes filtering), D4 (feedback ring buffer + planner section).
"""
import json
import os
from datetime import timedelta

import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("APP_PASSWORD", "x")

import auth
import db


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


# ── B5 · streak freeze ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_earn_streak_freeze_per_n_and_cap(fresh_db):
    uid = fresh_db
    awards = [await db.earn_streak_freeze(uid) for _ in range(10)]
    assert awards.count(True) == 1                       # exactly one per 10 lessons
    assert (await db.get_streak_freeze_state(uid))["freezes"] == 1
    # Another 10 → a second freeze; then capped at 2.
    for _ in range(10):
        await db.earn_streak_freeze(uid)
    assert (await db.get_streak_freeze_state(uid))["freezes"] == 2
    for _ in range(10):
        await db.earn_streak_freeze(uid)
    assert (await db.get_streak_freeze_state(uid))["freezes"] == db.STREAK_FREEZE_CAP


async def _mark_active(uid, day):
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute(
            "INSERT OR IGNORE INTO study_activity (user_id, study_date) VALUES (?, ?)",
            (uid, day.isoformat()))
        await c.commit()


@pytest.mark.asyncio
async def test_freeze_bridges_one_day_gap_only(fresh_db):
    uid = fresh_db
    today = db._utc_today_date()
    # Studied 2 and 3 days ago → yesterday is missing (a one-day gap).
    await _mark_active(uid, today - timedelta(days=2))
    await _mark_active(uid, today - timedelta(days=3))
    assert await db.get_streak(uid) == 0                 # lapses without a freeze
    await db.set_setting(uid, "streak_freezes", "1")
    assert await db.get_streak(uid) == 3                 # freeze bridges yesterday
    st = await db.get_streak_freeze_state(uid)
    assert st["freezes"] == 0                            # consumed exactly once
    assert st["used_date"] == (today - timedelta(days=1)).isoformat()
    assert await db.get_streak(uid) == 3                 # idempotent (no re-consume)


@pytest.mark.asyncio
async def test_freeze_cannot_bridge_two_day_gap(fresh_db):
    uid = fresh_db
    today = db._utc_today_date()
    await _mark_active(uid, today - timedelta(days=3))   # missed both -1 and -2
    await db.set_setting(uid, "streak_freezes", "2")
    assert await db.get_streak(uid) == 0
    assert (await db.get_streak_freeze_state(uid))["freezes"] == 2   # untouched


# ── B6 · practice hub (pool recombination) ─────────────────────────────────────

def _drill(t, **kw):
    return {"type": t, "concept_key": kw.pop("key", "k"), **kw}


@pytest.mark.asyncio
async def test_practice_pool_only_completed_and_gradeable(fresh_db):
    import main
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    l1 = await db.create_lesson(cid, 1, "L1", "o",
        [{"kind": "vocab", "key": "wa", "label": "m1", "gloss": "w"}],
        {"segments": [{"teach": None, "exercises": [
            _drill("choice", key="wa", prompt_lang="english", prompt="hi",
                   options=["salut", "bonjour"], answer=0),
            _drill("word_bank", key="wa", answer_tokens=["a", "b"]),
            _drill("construction_drill", key="wa", construction="x"),
        ]}]}, "s")
    l2 = await db.create_lesson(cid, 2, "L2", "o",
        [{"kind": "vocab", "key": "wb", "label": "m2", "gloss": "w"}],
        {"segments": [{"teach": None, "exercises": [
            _drill("choice", key="wb", prompt_lang="target", prompt="x",
                   options=["hi", "bye"], answer=0),
        ]}]}, "s")
    # Only L1 completed; L2 left unfinished.
    await db.complete_lesson(uid, l1, 80)

    lessons = await db.get_completed_lesson_contents(uid, cid)
    assert [l["id"] for l in lessons] == [l1]
    pool = main._course_drill_pool(lessons, "fr")
    assert sorted(e["type"] for e in pool) == ["choice", "word_bank"]   # construction excluded
    # Another user can't read this course's lessons.
    other = await db.create_user("x", auth.hash_password("p"))
    assert await db.get_completed_lesson_contents(other, cid) == []


@pytest.mark.asyncio
async def test_mistakes_prefers_weak_concepts(fresh_db):
    import main
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    l1 = await db.create_lesson(cid, 1, "L1", "o",
        [{"kind": "vocab", "key": "weak1", "label": "m1", "gloss": "w"}],
        {"segments": [{"teach": None, "exercises": [
            _drill("choice", key="weak1", prompt_lang="english", prompt="a",
                   options=["1", "2"], answer=0)]}]}, "s")
    await db.complete_lesson(uid, l1, 50)
    await db.record_concept_results(uid, "fr", [{"concept_key": "weak1", "correct": 0, "total": 5}])
    mastery = await db.get_mastery_summary(uid, "fr")
    weak = {m["concept_key"] for m in mastery if m["total"] >= 3 and m["correct"] / m["total"] < 0.7}
    assert weak == {"weak1"}
    pool = main._course_drill_pool(await db.get_completed_lesson_contents(uid, cid), "fr")
    assert any((e.get("concept_key") or "") in weak for e in pool)


# ── D4 · per-lesson feedback ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_feedback_ring_buffer_caps_and_keeps_recent(fresh_db):
    import main
    uid = fresh_db
    for i in range(main._LESSON_FEEDBACK_MAX + 5):
        await main._append_lesson_feedback(uid, {"title": f"T{i}", "concepts": [],
                                                 "rating": "up" if i % 2 else "down"})
    buf = json.loads(await db.get_setting(uid, "lesson_feedback"))
    assert len(buf) == main._LESSON_FEEDBACK_MAX
    assert buf[-1]["title"] == f"T{main._LESSON_FEEDBACK_MAX + 4}"   # newest kept


def test_planner_prompt_quotes_feedback():
    import learning
    prompt = learning._build_plan_prompt(
        "fr", "A1", [], [],
        lesson_feedback=[{"title": "Greetings", "rating": "up"},
                         {"title": "Verb tables", "rating": "down"}])
    assert "LESSON FEEDBACK" in prompt
    assert "Greetings" in prompt and "Verb tables" in prompt
