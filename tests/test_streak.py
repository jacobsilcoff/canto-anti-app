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
async def test_add_points_records_study_activity(fresh_db):
    # Earning XP from ANY feature must count toward the streak. add_points is the
    # single chokepoint for all XP (review / lesson / tutor / comprehension), so
    # it records the active day itself — no caller can award XP without the streak.
    assert await db.get_streak(fresh_db) == 0
    await db.add_points(fresh_db, "yue", 5, "review")
    assert await db.get_streak(fresh_db) == 1
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT study_date FROM study_activity WHERE user_id=?", (fresh_db,)
        ) as cur:
            rows = [r[0] async for r in cur]
    assert rows == [_utc_today().isoformat()]


@pytest.mark.asyncio
async def test_add_points_noop_does_not_record_activity(fresh_db):
    # Non-positive XP is a no-op and must not fabricate a study day.
    await db.add_points(fresh_db, "yue", 0, "ignored")
    assert await db.get_streak(fresh_db) == 0


@pytest.mark.asyncio
async def test_add_points_extends_streak_across_days(fresh_db):
    # Yesterday earned via XP, today earned via XP -> streak of 2.
    await _mark_days(fresh_db, [1])
    await db.add_points(fresh_db, "yue", 7, "lesson")
    assert await db.get_streak(fresh_db) == 2


@pytest.mark.asyncio
async def test_set_streak_from_zero(fresh_db):
    assert await db.set_streak(fresh_db, 15) == 15
    assert await db.get_streak(fresh_db) == 15


@pytest.mark.asyncio
async def test_set_streak_is_idempotent(fresh_db):
    await db.set_streak(fresh_db, 15)
    assert await db.set_streak(fresh_db, 15) == 15


@pytest.mark.asyncio
async def test_set_streak_can_shorten_existing(fresh_db):
    # A long real streak, then admin trims it to 3 — the boundary day is cleared
    # so the count stops at exactly 3.
    await _mark_days(fresh_db, list(range(10)))
    assert await db.get_streak(fresh_db) == 10
    assert await db.set_streak(fresh_db, 3) == 3


@pytest.mark.asyncio
async def test_set_streak_zero_clears_streak(fresh_db):
    await _mark_days(fresh_db, [0, 1, 2])
    assert await db.set_streak(fresh_db, 0) == 0


@pytest.mark.asyncio
async def test_set_streak_extends_existing(fresh_db):
    await _mark_days(fresh_db, [0, 1])
    assert await db.set_streak(fresh_db, 30) == 30


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


# ── Per-user day boundaries ────────────────────────────────────────────────────
# The day used to roll over at 00:00 UTC for everybody — 5pm in California, noon
# in Auckland. Two consecutive LOCAL study days could land in one UTC day, which
# opened a phantom gap and broke a streak the learner had genuinely kept.

async def _dates(user_id):
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT study_date FROM study_activity WHERE user_id=? ORDER BY study_date",
            (user_id,),
        ) as cur:
            return [r[0] async for r in cur]


def test_local_today_follows_the_users_zone():
    from datetime import datetime, timezone
    # 03:30 UTC on the 2nd is still the 1st in Los Angeles and already the 2nd
    # in Auckland — one instant, three different calendar days.
    now = datetime(2026, 7, 2, 3, 30, tzinfo=timezone.utc)
    assert db.local_today_str("UTC", now) == "2026-07-02"
    assert db.local_today_str("America/Los_Angeles", now) == "2026-07-01"
    assert db.local_today_str("Pacific/Auckland", now) == "2026-07-02"


def test_unknown_timezone_falls_back_to_utc():
    from datetime import datetime, timezone
    now = datetime(2026, 7, 2, 3, 30, tzinfo=timezone.utc)
    for bad in ("Mars/Olympus", "", None, "not a zone"):
        assert db.local_today_str(bad, now) == "2026-07-02"
        assert db.is_valid_timezone(bad) is False
    assert db.is_valid_timezone("America/Los_Angeles") is True


def test_local_day_bounds_span_exactly_one_day():
    start, end = db.local_day_bounds("America/Los_Angeles", "2026-07-01")
    # PDT is UTC-7, so the local day runs 07:00→07:00 UTC.
    assert start == "2026-07-01 07:00:00"
    assert end == "2026-07-02 07:00:00"


@pytest.mark.asyncio
async def test_study_is_recorded_on_the_users_local_day(fresh_db):
    """The regression that cost learners their streaks: an evening review west
    of UTC was filed under tomorrow, so two local days shared one row."""
    await db.set_setting(fresh_db, "timezone", "America/Los_Angeles")
    await db.record_study_activity(fresh_db)
    assert await _dates(fresh_db) == [db.local_today_str("America/Los_Angeles")]
    assert await db.get_streak(fresh_db) == 1


@pytest.mark.asyncio
async def test_streak_reads_today_in_the_users_zone(fresh_db):
    """A learner in Auckland is often a calendar day ahead of UTC; their streak
    must be counted against THEIR today, or it reads as lapsed all morning."""
    tz = "Pacific/Auckland"
    await db.set_setting(fresh_db, "timezone", tz)
    today = datetime.fromisoformat(db.local_today_str(tz)).date()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        for n in range(4):
            await conn.execute(
                "INSERT OR IGNORE INTO study_activity (user_id, study_date) VALUES (?, ?)",
                (fresh_db, (today - timedelta(days=n)).isoformat()))
        await conn.commit()
    assert await db.get_streak(fresh_db) == 4


@pytest.mark.asyncio
async def test_consecutive_local_days_do_not_collapse(fresh_db):
    """The exact shape of the user reports. UTC midnight is 5pm in Los Angeles,
    so an 8pm session and the next afternoon's session used to land in the SAME
    UTC day — four consecutive days of study read as a broken streak."""
    tz = "America/Los_Angeles"
    zone = db.resolve_timezone(tz)
    await db.set_setting(fresh_db, "timezone", tz)
    sessions = ["2026-07-06 20:00", "2026-07-07 20:00",   # Mon/Tue evening
                "2026-07-08 15:00", "2026-07-09 20:00"]   # Wed afternoon, Thu evening

    utc_days, local_days = set(), set()
    for stamp in sessions:
        moment = datetime.fromisoformat(stamp).replace(tzinfo=zone).astimezone(timezone.utc)
        utc_days.add(moment.date().isoformat())
        local_days.add(db.local_today_str(tz, moment))

    # The old UTC bucketing lost a day outright, and left a gap behind it.
    assert len(utc_days) == 3
    assert db._streak_from_dates(sorted(utc_days, reverse=True),
                                 datetime.fromisoformat(max(utc_days)).date()) == 1
    # Local bucketing keeps one row per day the learner actually studied.
    assert len(local_days) == 4
    assert db._streak_from_dates(sorted(local_days, reverse=True),
                                 datetime.fromisoformat(max(local_days)).date()) == 4


@pytest.mark.asyncio
async def test_ensure_min_streak_only_ever_adds(fresh_db):
    """The timezone-change guard: re-reading history against a new day boundary
    can shift the count, and nobody should lose an earned streak to a fix."""
    await _mark_days(fresh_db, [0, 1, 2])
    assert await db.ensure_min_streak(fresh_db, 7) == 7
    assert await db.ensure_min_streak(fresh_db, 3) == 7   # never shortens
    # Anchors on yesterday when today has no activity — it extends a streak,
    # it does not invent a study session for a day still in progress.
    await _mark_days(fresh_db, [])
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("DELETE FROM study_activity WHERE user_id=?", (fresh_db,))
        await conn.commit()
    await _mark_days(fresh_db, [1])
    await db.ensure_min_streak(fresh_db, 4)
    assert _utc_today().isoformat() not in await _dates(fresh_db)
    assert await db.get_streak(fresh_db) == 4


@pytest.mark.asyncio
async def test_points_today_windows_the_local_day(fresh_db):
    """The XP ring must roll over with the streak, not at UTC midnight."""
    await db.set_setting(fresh_db, "timezone", "America/Los_Angeles")
    await db.add_points(fresh_db, "yue", 12, "review")
    assert await db.get_points_today(fresh_db, "yue") == 12
    # An award from before the local day started is not "today".
    start, _ = db.local_day_bounds("America/Los_Angeles")
    before = (datetime.fromisoformat(start) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO points_ledger (user_id, lang, points, reason, created_at) "
            "VALUES (?, 'yue', 99, 'review', ?)", (fresh_db, before))
        await conn.commit()
    assert await db.get_points_today(fresh_db, "yue") == 12


# ── Backdated (offline) reviews ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backdated_activity_credits_the_day_it_happened(fresh_db):
    """Offline reviews queue in IndexedDB and sync whenever the device
    reconnects; crediting them to the sync day dropped the day they were
    actually answered."""
    today = _utc_today()
    yesterday = (today - timedelta(days=1)).isoformat()
    await db.record_study_activity(fresh_db, yesterday)
    assert await _dates(fresh_db) == [yesterday]
    assert await db.get_streak(fresh_db) == 1          # yesterday still counts
    await db.record_study_activity(fresh_db)
    assert await db.get_streak(fresh_db) == 2


@pytest.mark.asyncio
async def test_backdated_review_closes_its_own_gap_without_a_freeze(fresh_db):
    """A synced review that fills the missing day must not also cost a shield."""
    today = _utc_today()
    await _mark_days(fresh_db, [2, 3])
    await db.set_setting(fresh_db, "streak_freezes", "1")
    await db.record_study_activity(fresh_db, (today - timedelta(days=1)).isoformat())
    assert await db.get_streak(fresh_db) == 3
    assert (await db.get_streak_freeze_state(fresh_db))["freezes"] == 1   # unspent


@pytest.mark.asyncio
async def test_client_supplied_study_date_is_clamped(fresh_db):
    """A client date decides which day the streak is credited to, so it is
    validated against the user's own calendar: anything in the future, older
    than the offline-backlog window, or unparseable falls back to server-side
    today. It must never become a way to hand-write streak history."""
    from cryptography.fernet import Fernet
    os.environ.setdefault("API_KEY_ENC_KEY", Fernet.generate_key().decode())
    import main

    await db.set_setting(fresh_db, "timezone", "America/Los_Angeles")
    today = datetime.fromisoformat(db.local_today_str("America/Los_Angeles")).date()
    ok = (today - timedelta(days=1)).isoformat()
    assert await main._clamp_studied_on(fresh_db, ok) == ok
    for bad in [(today + timedelta(days=1)).isoformat(),
                (today - timedelta(days=30)).isoformat(),
                "garbage", "2026-13-45", "", None]:
        assert await main._clamp_studied_on(fresh_db, bad) is None
