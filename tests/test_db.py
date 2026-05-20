import os
import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import db


@pytest_asyncio.fixture(autouse=True)
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()


@pytest.mark.asyncio
async def test_create_and_fetch_card():
    card_id = await db.create_card("Hello", "你好", "nei5 hou2")
    card = await db.get_card(card_id)
    assert card["english"] == "Hello"
    assert card["chinese"] == "你好"
    assert card["jyutping"] == "nei5 hou2"


@pytest.mark.asyncio
async def test_create_card_makes_three_faces():
    card_id = await db.create_card("Hello", "你好", "nei5 hou2")
    faces = await db.get_due_faces()
    card_faces = [f for f in faces if f["card_id"] == card_id]
    assert {f["face"] for f in card_faces} == {"english", "chinese", "cantonese"}


@pytest.mark.asyncio
async def test_due_faces_includes_new():
    await db.create_card("Hello", "你好", "nei5 hou2")
    due = await db.get_due_faces()
    assert len(due) == 3


@pytest.mark.asyncio
async def test_delete_card_removes_faces():
    card_id = await db.create_card("Bye", "拜拜", "baai3 baai3")
    await db.delete_card(card_id)
    assert await db.get_card(card_id) is None
    faces = await db.get_all_faces()
    assert all(f["card_id"] != card_id for f in faces)


@pytest.mark.asyncio
async def test_due_count_counts_faces():
    await db.create_card("One", "一", "jat1")
    await db.create_card("Two", "二", "ji6")
    assert await db.get_due_count() == 6


@pytest.mark.asyncio
async def test_update_face_review():
    card_id = await db.create_card("Hello", "你好", "nei5 hou2")
    state = {"interval_days": 6, "ease_factor": 2.6, "repetitions": 2, "next_review": "2099-01-01 00:00:00"}
    await db.update_face_review(card_id, "english", state)
    # english face should no longer be due
    due = await db.get_due_faces()
    english_due = [f for f in due if f["card_id"] == card_id and f["face"] == "english"]
    assert len(english_due) == 0
    # other faces still due
    other_due = [f for f in due if f["card_id"] == card_id and f["face"] != "english"]
    assert len(other_due) == 2


@pytest.mark.asyncio
async def test_update_card_content():
    card_id = await db.create_card("Hello", "你好", "nei5 hou2")
    await db.update_card(card_id, "Hi", "嗨", "haai1")
    card = await db.get_card(card_id)
    assert card["english"] == "Hi"
    assert card["chinese"] == "嗨"


@pytest.mark.asyncio
async def test_get_all_faces():
    await db.create_card("Hello", "你好", "nei5 hou2")
    await db.create_card("Bye", "拜拜", "baai3 baai3")
    faces = await db.get_all_faces()
    assert len(faces) == 6
