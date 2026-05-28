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
async def test_due_faces_includes_new(fresh_db):
    user_id = fresh_db
    await db.create_card(user_id, "Hello", "你好", "nei5 hou2")
    due = await db.get_due_faces(user_id)
    assert len(due) == 3


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
    assert await db.get_due_count(user_id) == 6


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
