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

_CURRIC = {
    "level": "A1", "language": "French",
    "units": [
        {"title": "Greetings", "theme": "social", "objective": "Say hello",
         "lessons": [
            {"title": "Hello & Goodbye", "objective": "Greet",
             "new_concepts": [
                {"kind": "vocab", "key": "greeting_hello", "label": "Bonjour", "gloss": "hello"},
                {"kind": "vocab", "key": "greeting_goodbye", "label": "Au revoir", "gloss": "goodbye"},
             ]},
            {"title": "My Name", "objective": "Introduce",
             "new_concepts": [{"kind": "grammar", "key": "verb_to_be_je", "label": "suis", "gloss": "am"}]},
         ]},
        {"title": "Numbers", "theme": "math", "objective": "Count",
         "lessons": [
            {"title": "0-10", "objective": "Count to ten",
             "new_concepts": [{"kind": "vocab", "key": "number_1", "label": "un", "gloss": "one"}]},
         ]},
    ],
}


@pytest.mark.asyncio
async def test_create_and_get_course(fresh_db):
    user_id = fresh_db
    cid = await db.create_course(user_id, "fr", "A1", _CURRIC)
    course = await db.get_course(user_id, cid)
    assert course["title"] == "French A1"
    assert len(course["units"]) == 2
    assert [u["title"] for u in course["units"]] == ["Greetings", "Numbers"]
    first_unit = course["units"][0]
    assert len(first_unit["lessons"]) == 2
    assert first_unit["lessons"][0]["concept_count"] == 2


@pytest.mark.asyncio
async def test_course_unlock_progression(fresh_db):
    user_id = fresh_db
    cid = await db.create_course(user_id, "fr", "A1", _CURRIC)
    course = await db.get_course(user_id, cid)
    statuses = [l["status"] for u in course["units"] for l in u["lessons"]]
    assert statuses == ["available", "locked", "locked"]


@pytest.mark.asyncio
async def test_active_course_and_delete(fresh_db):
    user_id = fresh_db
    cid = await db.create_course(user_id, "fr", "A1", _CURRIC)
    active = await db.get_active_course(user_id, "fr")
    assert active is not None and active["id"] == cid
    assert await db.get_active_course(user_id, "es") is None  # different language
    await db.delete_course(user_id, cid)
    assert await db.get_courses(user_id) == []
    assert await db.get_active_course(user_id, "fr") is None


@pytest.mark.asyncio
async def test_courses_are_siloed_by_user(fresh_db):
    admin_id = fresh_db
    other_id = await db.create_user("other", auth.hash_password("pw"))
    cid = await db.create_course(admin_id, "fr", "A1", _CURRIC)
    assert await db.get_course(other_id, cid) is None
    assert await db.get_courses(other_id) == []


@pytest.mark.asyncio
async def test_get_lesson_has_concepts(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1", _CURRIC)
    course = await db.get_course(uid, cid)
    lid = course["units"][0]["lessons"][0]["id"]
    lesson = await db.get_lesson(uid, lid)
    assert lesson["title"] == "Hello & Goodbye"
    assert lesson["target_lang"] == "fr"
    assert {c["key"] for c in lesson["concepts"]} == {"greeting_hello", "greeting_goodbye"}
    assert lesson["content"] is None
    assert lesson["prior_concepts"] == []


@pytest.mark.asyncio
async def test_lesson_content_roundtrip(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1", _CURRIC)
    course = await db.get_course(uid, cid)
    lid = course["units"][0]["lessons"][0]["id"]
    content = {"exercises": [{"type": "choice", "answer": 0}]}
    assert await db.set_lesson_content(uid, lid, content)
    assert (await db.get_lesson(uid, lid))["content"] == content


@pytest.mark.asyncio
async def test_complete_lesson_unlocks_next(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1", _CURRIC)
    course = await db.get_course(uid, cid)
    lid = course["units"][0]["lessons"][0]["id"]
    assert await db.complete_lesson(uid, lid, 90)
    statuses = [l["status"] for u in (await db.get_course(uid, cid))["units"] for l in u["lessons"]]
    assert statuses[0] == "done" and statuses[1] == "available"
    lesson = await db.get_lesson(uid, lid)
    assert lesson["completed"] and lesson["score"] == 90


@pytest.mark.asyncio
async def test_prior_concepts_accumulate(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1", _CURRIC)
    course = await db.get_course(uid, cid)
    l2 = course["units"][0]["lessons"][1]["id"]
    prior = {c["gloss"] for c in (await db.get_lesson(uid, l2))["prior_concepts"]}
    assert "hello" in prior and "goodbye" in prior


@pytest.mark.asyncio
async def test_lesson_ownership(fresh_db):
    admin = fresh_db
    other = await db.create_user("o2", auth.hash_password("pw"))
    cid = await db.create_course(admin, "fr", "A1", _CURRIC)
    lid = (await db.get_course(admin, cid))["units"][0]["lessons"][0]["id"]
    assert await db.get_lesson(other, lid) is None
    assert not await db.set_lesson_content(other, lid, {"x": 1})
    assert not await db.complete_lesson(other, lid, 50)


_CURRIC2 = {
    "level": "A2", "language": "French",
    "units": [
        {"title": "Past Tense", "theme": "grammar", "objective": "Talk about the past",
         "lessons": [
            {"title": "Yesterday", "objective": "Past events",
             "new_concepts": [{"kind": "grammar", "key": "passe_compose", "label": "passé composé", "gloss": "past tense"}]},
         ]},
    ],
}


@pytest.mark.asyncio
async def test_concept_digest(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1", _CURRIC)
    digest = await db.get_course_concept_digest(cid)
    assert "hello" in digest and "goodbye" in digest


@pytest.mark.asyncio
async def test_append_units_extends_course(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1", _CURRIC)
    before = await db.get_course(uid, cid)
    n_units_before = len(before["units"])
    added = await db.append_units(uid, cid, _CURRIC2, "A2")
    assert added == 1
    after = await db.get_course(uid, cid)
    assert len(after["units"]) == n_units_before + 1
    assert after["level"] == "A2"
    # appended unit comes last and its lesson is locked (earlier ones not done)
    assert after["units"][-1]["title"] == "Past Tense"
    # unit indices stay contiguous and ordered
    assert [u["idx"] for u in after["units"]] == list(range(len(after["units"])))


@pytest.mark.asyncio
async def test_append_units_respects_ownership(fresh_db):
    admin = fresh_db
    other = await db.create_user("o3", auth.hash_password("pw"))
    cid = await db.create_course(admin, "fr", "A1", _CURRIC)
    assert await db.append_units(other, cid, _CURRIC2, "A2") == 0
