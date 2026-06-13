import os
import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import auth
import db


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


@pytest.mark.asyncio
async def test_bootstrap_creates_admin(fresh_db):
    user_id = fresh_db
    user = await db.get_user(user_id)
    assert user["username"] == "jsilcoff"
    assert user["is_admin"] == 1


@pytest.mark.asyncio
async def test_create_and_fetch_card(fresh_db):
    user_id = fresh_db
    card_id = await db.create_card(user_id, "Hello", "你好", "nei5 hou2", target_lang="yue")
    card = await db.get_card(user_id, card_id)
    assert card["source_text"] == "Hello"
    assert card["target_text"] == "你好"
    assert card["romanization"] == "nei5 hou2"
    assert card["target_lang"] == "yue"


@pytest.mark.asyncio
async def test_create_card_makes_three_faces(fresh_db):
    user_id = fresh_db
    card_id = await db.create_card(user_id, "Hello", "你好", "nei5 hou2")
    faces = await db.get_due_faces(user_id)
    card_faces = [f for f in faces if f["card_id"] == card_id]
    assert {f["face"] for f in card_faces} == set(db.FACES)


@pytest.mark.asyncio
async def test_session_roundtrip(fresh_db):
    import time
    user_id = fresh_db
    await db.create_session("tok-abc", user_id, time.time() + 3600)
    assert await db.get_session_user("tok-abc") == user_id
    # Raw token is never stored — only its sha256 hash.
    assert await db.get_session_user("tok-xyz") is None


@pytest.mark.asyncio
async def test_session_expires(fresh_db):
    import time
    user_id = fresh_db
    await db.create_session("expired", user_id, time.time() - 1)
    assert await db.get_session_user("expired") is None


@pytest.mark.asyncio
async def test_delete_user_sessions(fresh_db):
    import time
    user_id = fresh_db
    await db.create_session("a", user_id, time.time() + 3600)
    await db.create_session("b", user_id, time.time() + 3600)
    await db.delete_user_sessions(user_id)
    assert await db.get_session_user("a") is None
    assert await db.get_session_user("b") is None


@pytest.mark.asyncio
async def test_migrations_apply_once(tmp_path, monkeypatch):
    import aiosqlite
    # Point the runner at a throwaway migrations dir with one migration.
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "001_add_widget.sql").write_text(
        "CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT);"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", str(mig_dir))
    db.DB_PATH = str(tmp_path / "cards.db")

    await db.init()  # runs migrations
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='widget'"
        ) as cur:
            assert await cur.fetchone() is not None
        async with conn.execute("SELECT version FROM schema_migrations") as cur:
            versions = {r[0] for r in await cur.fetchall()}
            assert "001_add_widget.sql" in versions

    # Re-running init must not re-apply the migration (would error: table exists).
    await db.init()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute("SELECT COUNT(*) FROM schema_migrations") as cur:
            assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_due_faces_includes_new(fresh_db):
    user_id = fresh_db
    await db.create_card(user_id, "Hello", "你好", "nei5 hou2")
    due = await db.get_due_faces(user_id)
    assert len(due) == 3


@pytest.mark.asyncio
async def test_new_session_staggers_to_primary_face(fresh_db):
    """A brand-new word offers only its primary face, not all three at once."""
    user_id = fresh_db
    await db.create_card(user_id, "Hello", "你好", "nei5 hou2")
    session = await db.get_study_session(user_id)
    assert session["new_count"] == 1
    assert session["cards"][0]["face"] == db.PRIMARY_FACE


@pytest.mark.asyncio
async def test_secondary_faces_unlock_after_primary_graduates(fresh_db):
    """Once the primary face graduates out of learning, the other faces unlock."""
    user_id = fresh_db
    card_id = await db.create_card(user_id, "Hello", "你好", "nei5 hou2")
    # Graduate the primary face: out of learning, due in the future.
    await db.update_face_review(user_id, card_id, db.PRIMARY_FACE, {
        "interval_days": 1, "ease_factor": 2.5, "repetitions": 1,
        "next_review": "2099-01-01 00:00:00", "learning_step": None,
    })
    session = await db.get_study_session(user_id)
    new_faces = {c["face"] for c in session["cards"]}
    assert new_faces == {f for f in db.FACES if f != db.PRIMARY_FACE}


@pytest.mark.asyncio
async def test_primary_in_learning_keeps_secondaries_locked(fresh_db):
    """While the primary face is still mid-learning, secondaries stay locked."""
    user_id = fresh_db
    card_id = await db.create_card(user_id, "Hello", "你好", "nei5 hou2")
    # Primary face seen but still in a learning step (not graduated).
    await db.update_face_review(user_id, card_id, db.PRIMARY_FACE, {
        "interval_days": 0, "ease_factor": 2.5, "repetitions": 0,
        "next_review": "2099-01-01 00:00:00", "learning_step": 1,
    })
    session = await db.get_study_session(user_id)
    assert session["new_count"] == 0


@pytest.mark.asyncio
async def test_delete_card_removes_faces(fresh_db):
    user_id = fresh_db
    card_id = await db.create_card(user_id, "Bye", "拜拜", "baai3 baai3")
    await db.delete_card(user_id, card_id)
    assert await db.get_card(user_id, card_id) is None
    faces = await db.get_all_faces(user_id)
    assert all(f["card_id"] != card_id for f in faces)


@pytest.mark.asyncio
async def test_due_count_counts_faces(fresh_db):
    user_id = fresh_db
    await db.create_card(user_id, "One", "一", "jat1")
    await db.create_card(user_id, "Two", "二", "ji6")
    # New words are staggered to their primary face, so each contributes 1 (not 3)
    # until that face graduates and unlocks the others.
    assert await db.get_due_count(user_id) == 2


@pytest.mark.asyncio
async def test_update_face_review(fresh_db):
    user_id = fresh_db
    card_id = await db.create_card(user_id, "Hello", "你好", "nei5 hou2")
    state = {"interval_days": 6, "ease_factor": 2.6, "repetitions": 2, "next_review": "2099-01-01 00:00:00"}
    await db.update_face_review(user_id, card_id, "source", state)
    due = await db.get_due_faces(user_id)
    source_due = [f for f in due if f["card_id"] == card_id and f["face"] == "source"]
    assert len(source_due) == 0
    other_due = [f for f in due if f["card_id"] == card_id and f["face"] != "source"]
    assert len(other_due) == 2


@pytest.mark.asyncio
async def test_update_card_content(fresh_db):
    user_id = fresh_db
    card_id = await db.create_card(user_id, "Hello", "你好", "nei5 hou2")
    await db.update_card(user_id, card_id, "Hi", "嗨", "haai1")
    card = await db.get_card(user_id, card_id)
    assert card["source_text"] == "Hi"
    assert card["target_text"] == "嗨"


@pytest.mark.asyncio
async def test_get_all_faces(fresh_db):
    user_id = fresh_db
    await db.create_card(user_id, "Hello", "你好", "nei5 hou2")
    await db.create_card(user_id, "Bye", "拜拜", "baai3 baai3")
    faces = await db.get_all_faces(user_id)
    assert len(faces) == 6


@pytest.mark.asyncio
async def test_users_are_siloed(fresh_db):
    admin_id = fresh_db
    other_id = await db.create_user("alice", auth.hash_password("xxx-secret"))
    await db.create_card(admin_id, "Hello", "你好", "nei5 hou2")
    await db.create_card(other_id, "Goodbye", "再見", "zoi3 gin3")

    admin_cards = await db.get_all_cards(admin_id)
    other_cards = await db.get_all_cards(other_id)
    assert len(admin_cards) == 1 and admin_cards[0]["source_text"] == "Hello"
    assert len(other_cards) == 1 and other_cards[0]["source_text"] == "Goodbye"


@pytest.mark.asyncio
async def test_other_user_cannot_modify_card(fresh_db):
    admin_id = fresh_db
    other_id = await db.create_user("alice", auth.hash_password("xxx-secret"))
    card_id = await db.create_card(admin_id, "Hello", "你好", "nei5 hou2")
    # Attempting to delete as the wrong user is a no-op.
    await db.delete_card(other_id, card_id)
    assert await db.get_card(admin_id, card_id) is not None
    await db.update_card(other_id, card_id, "Hijacked", "x", "y")
    card = await db.get_card(admin_id, card_id)
    assert card["source_text"] == "Hello"


@pytest.mark.asyncio
async def test_labels_per_user(fresh_db):
    admin_id = fresh_db
    other_id = await db.create_user("alice", auth.hash_password("xxx-secret"))
    a = await db.create_label(admin_id, "food")
    b = await db.create_label(other_id, "food")
    # Both users can have a label named "food"; they get distinct IDs.
    assert a["id"] != b["id"]
    admin_labels = await db.list_labels(admin_id)
    other_labels = await db.list_labels(other_id)
    assert {l["id"] for l in admin_labels} == {a["id"]}
    assert {l["id"] for l in other_labels} == {b["id"]}


@pytest.mark.asyncio
async def test_settings_per_user(fresh_db):
    admin_id = fresh_db
    other_id = await db.create_user("alice", auth.hash_password("xxx-secret"))
    await db.set_setting(admin_id, "new_cards_per_day", 30)
    await db.set_setting(other_id, "new_cards_per_day", 5)
    assert await db.get_setting(admin_id, "new_cards_per_day") == "30"
    assert await db.get_setting(other_id, "new_cards_per_day") == "5"


@pytest.mark.asyncio
async def test_delete_user_cascades(fresh_db):
    admin_id = fresh_db
    other_id = await db.create_user("alice", auth.hash_password("xxx-secret"))
    await db.create_card(other_id, "Hello", "你好", "nei5 hou2")
    await db.create_label(other_id, "greetings")
    await db.delete_user(other_id)
    assert await db.get_user(other_id) is None
    assert await db.get_all_cards(other_id) == []
    assert await db.list_labels(other_id) == []


@pytest.mark.asyncio
async def test_multilang_card_no_romanization(fresh_db):
    user_id = fresh_db
    card_id = await db.create_card(
        user_id, "Hello", "Bonjour", romanization="", target_lang="fr"
    )
    card = await db.get_card(user_id, card_id)
    assert card["target_lang"] == "fr"
    assert card["romanization"] == ""


# ── Learning path (AI course) ─────────────────────────────────────────────────

_CONCEPTS_L1 = [
    {"kind": "vocab", "key": "greeting_hello", "label": "Bonjour", "gloss": "hello"},
    {"kind": "vocab", "key": "greeting_goodbye", "label": "Au revoir", "gloss": "goodbye"},
]
_CONCEPTS_L2 = [
    {"kind": "grammar", "key": "verb_to_be_je", "label": "suis", "gloss": "am"},
]
_CONTENT = {"segments": [{"teach": {"items": []}, "exercises": []}]}


@pytest.mark.asyncio
async def test_create_and_get_course(fresh_db):
    user_id = fresh_db
    cid = await db.create_course(user_id, "fr", "A1")
    course = await db.get_course(user_id, cid)
    assert course["id"] == cid
    assert course["target_lang"] == "fr"
    assert course["level"] == "A1"
    assert course["units"] == []


@pytest.mark.asyncio
async def test_course_with_lessons_and_unit(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    l1 = await db.create_lesson(cid, 1, "Hello & Goodbye", "Greet", _CONCEPTS_L1, _CONTENT, "Taught hello/goodbye")
    l2 = await db.create_lesson(cid, 2, "My Name", "Introduce", _CONCEPTS_L2, _CONTENT, "Taught suis")
    await db.close_unit(cid, "Greetings", "Basic greeting phrases")
    course = await db.get_course(uid, cid)
    assert len(course["units"]) == 1
    assert course["units"][0]["title"] == "Greetings"
    assert len(course["units"][0]["lessons"]) == 2
    assert course["units"][0]["lessons"][0]["concept_count"] == 2


@pytest.mark.asyncio
async def test_lesson_status_progression(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    l1 = await db.create_lesson(cid, 1, "L1", "obj", _CONCEPTS_L1, _CONTENT, "s1")
    l2 = await db.create_lesson(cid, 2, "L2", "obj", _CONCEPTS_L2, _CONTENT, "s2")
    course = await db.get_course(uid, cid)
    pending = course["units"][0]["lessons"]
    assert pending[0]["status"] == "available"
    assert pending[1]["status"] == "locked"
    # complete first, second becomes available
    await db.complete_lesson(uid, l1, 80)
    course2 = await db.get_course(uid, cid)
    statuses = [l["status"] for l in course2["units"][0]["lessons"]]
    assert statuses[0] == "done"
    assert statuses[1] == "available"


@pytest.mark.asyncio
async def test_in_progress_unit_virtual(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    await db.create_lesson(cid, 1, "L1", "obj", _CONCEPTS_L1, _CONTENT, "s")
    course = await db.get_course(uid, cid)
    assert len(course["units"]) == 1
    assert course["units"][0].get("in_progress") is True
    assert course["units"][0]["title"] is None


@pytest.mark.asyncio
async def test_active_course_and_delete(fresh_db):
    user_id = fresh_db
    cid = await db.create_course(user_id, "fr", "A1")
    active = await db.get_active_course(user_id, "fr")
    assert active is not None and active["id"] == cid
    assert await db.get_active_course(user_id, "es") is None
    await db.delete_course(user_id, cid)
    assert await db.get_courses(user_id) == []
    assert await db.get_active_course(user_id, "fr") is None


@pytest.mark.asyncio
async def test_courses_are_siloed_by_user(fresh_db):
    admin_id = fresh_db
    other_id = await db.create_user("other", auth.hash_password("pw"))
    cid = await db.create_course(admin_id, "fr", "A1")
    assert await db.get_course(other_id, cid) is None
    assert await db.get_courses(other_id) == []


@pytest.mark.asyncio
async def test_get_lesson_has_concepts(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    lid = await db.create_lesson(cid, 1, "Hello & Goodbye", "Greet", _CONCEPTS_L1, _CONTENT, "s")
    lesson = await db.get_lesson(uid, lid)
    assert lesson["title"] == "Hello & Goodbye"
    assert lesson["target_lang"] == "fr"
    assert {c["key"] for c in lesson["concepts"]} == {"greeting_hello", "greeting_goodbye"}
    assert lesson["content"] == _CONTENT
    assert lesson["completed"] is False


@pytest.mark.asyncio
async def test_lesson_llm_debug(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    debug = {"prompt": "test prompt", "response": "test response"}
    lid = await db.create_lesson(cid, 1, "L1", "obj", [], _CONTENT, "s", debug)
    lesson = await db.get_lesson(uid, lid)
    assert lesson["llm_debug"] == debug


@pytest.mark.asyncio
async def test_complete_lesson(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    lid = await db.create_lesson(cid, 1, "L1", "obj", _CONCEPTS_L1, _CONTENT, "s")
    found, first = await db.complete_lesson(uid, lid, 90)
    assert found and first                       # first completion
    lesson = await db.get_lesson(uid, lid)
    assert lesson["completed"] is True
    assert lesson["score"] == 90
    # Re-completing is found but no longer "first" (so XP isn't re-awarded).
    found2, first2 = await db.complete_lesson(uid, lid, 95)
    assert found2 and not first2


@pytest.mark.asyncio
async def test_get_next_lesson_context(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    ctx0 = await db.get_next_lesson_context(cid)
    assert ctx0["lesson_num"] == 1
    assert ctx0["concept_registry"] == []
    assert ctx0["unit_summaries"] == []
    assert ctx0["recent_summaries"] == []

    await db.create_lesson(cid, 1, "L1", "obj", _CONCEPTS_L1, _CONTENT, "summary one")
    await db.close_unit(cid, "Greetings", "Basic phrases")

    ctx1 = await db.get_next_lesson_context(cid)
    assert ctx1["lesson_num"] == 2
    assert len(ctx1["concept_registry"]) == 2
    assert ctx1["concept_registry"][0]["key"] == "greeting_hello"
    assert len(ctx1["unit_summaries"]) == 1
    assert ctx1["unit_summaries"][0]["title"] == "Greetings"
    assert len(ctx1["recent_summaries"]) == 1
    assert ctx1["recent_summaries"][0]["summary"] == "summary one"


@pytest.mark.asyncio
async def test_lesson_ownership(fresh_db):
    admin = fresh_db
    other = await db.create_user("o2", auth.hash_password("pw"))
    cid = await db.create_course(admin, "fr", "A1")
    lid = await db.create_lesson(cid, 1, "L1", "obj", [], _CONTENT, "s")
    assert await db.get_lesson(other, lid) is None
    found, first = await db.complete_lesson(other, lid, 50)
    assert not found and not first


@pytest.mark.asyncio
async def test_close_unit_assigns_all_pending_lessons(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    l1 = await db.create_lesson(cid, 1, "L1", "obj", _CONCEPTS_L1, _CONTENT, "s1")
    l2 = await db.create_lesson(cid, 2, "L2", "obj", _CONCEPTS_L2, _CONTENT, "s2")
    unit_id = await db.close_unit(cid, "Unit One", "First unit")
    course = await db.get_course(uid, cid)
    assert len(course["units"]) == 1
    assert course["units"][0]["id"] == unit_id
    assert len(course["units"][0]["lessons"]) == 2
    # no more in_progress virtual unit
    assert not any(u.get("in_progress") for u in course["units"])


# ── Foundations seeding ────────────────────────────────────────────────────────

_F_UNITS = [
    {"title": "Script Basics", "objective": "obj1", "lessons": [
        {"title": "Intro",     "objective": "", "content": {"segments": []}},
        {"title": "Consonants","objective": "", "content": {"segments": []}},
    ]},
    {"title": "Vowels", "objective": "obj2", "lessons": [
        {"title": "Vowels", "objective": "", "content": {"segments": []}},
    ]},
]


@pytest.mark.asyncio
async def test_seed_foundations_on_empty_course(fresh_db):
    """Foundations seeded into a brand-new course start at idx 0."""
    uid = fresh_db
    cid = await db.create_course(uid, "ko", "A1")
    await db.seed_foundation_units(cid, _F_UNITS)
    course = await db.get_course(uid, cid)
    f_units = [u for u in course["units"] if u["theme"] == "foundations"]
    assert len(f_units) == 2
    # idx values start at 0 and are contiguous
    idxs = sorted(u["idx"] for u in f_units)
    assert idxs == [0, 1]
    # lessons are present
    total = sum(len(u["lessons"]) for u in f_units)
    assert total == 3


@pytest.mark.asyncio
async def test_seed_foundations_prepends_to_existing_ai_units(fresh_db):
    """Backfill: foundations are inserted at the front; existing AI units shift up."""
    uid = fresh_db
    cid = await db.create_course(uid, "yue", "A1")
    # Create one AI lesson and close it into a unit (idx 0)
    await db.create_lesson(cid, 1, "AI Lesson", "obj", _CONCEPTS_L1, _CONTENT, "s")
    await db.close_unit(cid, "AI Unit", "first AI unit")
    course_before = await db.get_course(uid, cid)
    assert course_before["units"][0]["title"] == "AI Unit"
    assert course_before["units"][0]["idx"] == 0

    # Now backfill 2 foundations units
    await db.seed_foundation_units(cid, _F_UNITS)
    course = await db.get_course(uid, cid)
    f_units = [u for u in course["units"] if u["theme"] == "foundations"]
    ai_units = [u for u in course["units"] if u["theme"] != "foundations"]
    # Foundations are at front (lowest idx values)
    assert len(f_units) == 2
    assert len(ai_units) == 1
    assert max(u["idx"] for u in f_units) < min(u["idx"] for u in ai_units)


@pytest.mark.asyncio
async def test_seed_foundations_idempotent_guard(fresh_db):
    """Calling seed twice would double the units — callers must check first.
    This test just verifies the second call isn't silently ignored; callers
    are responsible for the 'already seeded?' guard."""
    uid = fresh_db
    cid = await db.create_course(uid, "ko", "A1")
    await db.seed_foundation_units(cid, _F_UNITS)
    await db.seed_foundation_units(cid, _F_UNITS)   # called again
    course = await db.get_course(uid, cid)
    f_units = [u for u in course["units"] if u["theme"] == "foundations"]
    # Two calls → four foundation units (2×2). Guard belongs in the caller.
    assert len(f_units) == 4
