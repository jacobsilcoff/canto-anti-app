import os

import aiosqlite
import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import auth
import db


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("undo-user", auth.hash_password("test-password"))


async def face_row(card_id: int, face: str) -> dict:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM card_faces WHERE card_id=? AND face=?",
            (card_id, face),
        ) as cur:
            return dict(await cur.fetchone())


@pytest.mark.asyncio
async def test_review_undo_restores_schedule_xp_and_quest(fresh_db):
    user_id = fresh_db
    card_id = await db.create_card(user_id, "Hello", "你好", "nei5 hou2")
    before = await face_row(card_id, "target")
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO daily_quests
               (user_id, quest_date, quest_key, target, progress)
               VALUES (?, date('now'), 'reviews', 10, 3)""",
            (user_id,),
        )
        await conn.commit()

    changed = {
        "interval_days": 3,
        "ease_factor": 2.6,
        "repetitions": 1,
        "next_review": "2099-01-01 00:00:00",
        "learning_step": None,
    }
    review_id = await db.apply_card_review(
        user_id, card_id, "target", changed, xp=5, lang="yue",
    )
    assert review_id is not None
    assert await db.get_points_total(user_id, "yue") == 5
    assert (await face_row(card_id, "target"))["next_review"] == changed["next_review"]

    result = await db.undo_card_review(user_id, review_id)
    assert result == {"out_of_order": False, "xp": 5}
    after = await face_row(card_id, "target")
    for key in (
        "next_review", "interval_days", "ease_factor", "repetitions",
        "first_seen_date", "learning_step",
    ):
        assert after[key] == before[key]
    assert await db.get_points_total(user_id, "yue") == 0
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            """SELECT progress FROM daily_quests
               WHERE user_id=? AND quest_key='reviews'""",
            (user_id,),
        ) as cur:
            assert (await cur.fetchone())[0] == 3


@pytest.mark.asyncio
async def test_review_undo_must_be_newest_first(fresh_db):
    user_id = fresh_db
    first_card = await db.create_card(user_id, "One", "一", "jat1")
    second_card = await db.create_card(user_id, "Two", "二", "ji6")
    state = {
        "interval_days": 1,
        "ease_factor": 2.5,
        "repetitions": 1,
        "next_review": "2099-01-01 00:00:00",
        "learning_step": None,
    }
    first_id = await db.apply_card_review(user_id, first_card, "target", state)
    second_id = await db.apply_card_review(user_id, second_card, "target", state)

    assert await db.undo_card_review(user_id, first_id) == {
        "out_of_order": True,
        "latest_id": second_id,
    }
    assert (await db.undo_card_review(user_id, second_id))["out_of_order"] is False
    assert (await db.undo_card_review(user_id, first_id))["out_of_order"] is False
