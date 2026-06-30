import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
import aiosqlite

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import auth
import db


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


def _utc_today():
    # get_streak anchors "today" to UTC, matching how activity is recorded.
    return datetime.now(timezone.utc).date()


async def _mark_days(user_id, days_ago_list):
    """Insert study_activity rows for the given offsets (in days) before UTC today."""
    today = _utc_today()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        for n in days_ago_list:
            d = (today - timedelta(days=n)).isoformat()
            await conn.execute(
                "INSERT OR IGNORE INTO study_activity (user_id, study_date) VALUES (?, ?)",
                (user_id, d),
            )
        await conn.commit()


@pytest.mark.asyncio
async def test_streak_zero_when_no_activity(fresh_db):
    assert await db.get_streak(fresh_db) == 0


@pytest.mark.asyncio
async def test_streak_counts_consecutive_ending_today(fresh_db):
    await _mark_days(fresh_db, [0, 1, 2, 3, 4])
    assert await db.get_streak(fresh_db) == 5


@pytest.mark.asyncio
async def test_streak_survives_when_last_active_yesterday(fresh_db):
    # Streak is preserved before studying today (most recent == yesterday).
    await _mark_days(fresh_db, [1, 2, 3])
    assert await db.get_streak(fresh_db) == 3


@pytest.mark.asyncio
async def test_streak_breaks_after_two_day_gap(fresh_db):
    # Last activity was 2 days ago -> streak considered lapsed.
    await _mark_days(fresh_db, [2, 3, 4])
    assert await db.get_streak(fresh_db) == 0


@pytest.mark.asyncio
async def test_streak_stops_at_first_gap(fresh_db):
    # Active today, yesterday, then a gap at day 2, then more.
    await _mark_days(fresh_db, [0, 1, 3, 4, 5])
    assert await db.get_streak(fresh_db) == 2


@pytest.mark.asyncio
async def test_record_study_activity_uses_utc_today(fresh_db):
    await db.record_study_activity(fresh_db)
    # A single record today -> streak of 1, and it matches the UTC date.
    assert await db.get_streak(fresh_db) == 1
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT study_date FROM study_activity WHERE user_id=?", (fresh_db,)
        ) as cur:
            rows = [r[0] async for r in cur]
    assert rows == [_utc_today().isoformat()]
