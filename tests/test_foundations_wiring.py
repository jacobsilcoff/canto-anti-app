"""Tests for Foundations wiring into course creation (seed + skippable locking)."""
import os

import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import auth
import db
import foundations as F


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


@pytest.mark.asyncio
async def test_seed_foundation_units_creates_closed_units(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "ko", "A1")
    await db.seed_foundation_units(cid, F.build_units("ko"))
    course = await db.get_course(uid, cid)
    found_units = [u for u in course["units"] if u.get("theme") == "foundations"]
    assert len(found_units) >= 3
    # Lessons carry pre-built content and are numbered sequentially from 1.
    nums = [l["lesson_num"] for u in found_units for l in u["lessons"]]
    assert nums == list(range(1, len(nums) + 1))


@pytest.mark.asyncio
async def test_foundation_lessons_are_skippable(fresh_db):
    # All foundation lessons should be 'available' (none locked) — skippable.
    uid = fresh_db
    cid = await db.create_course(uid, "ko", "A1")
    await db.seed_foundation_units(cid, F.build_units("ko"))
    course = await db.get_course(uid, cid)
    statuses = [l["status"]
                for u in course["units"] if u.get("theme") == "foundations"
                for l in u["lessons"]]
    assert statuses                       # non-empty
    assert all(s == "available" for s in statuses)   # nothing locked


@pytest.mark.asyncio
async def test_ai_lessons_lock_independently_of_foundations(fresh_db):
    # The first AI lesson is available even with foundations incomplete; later AI
    # lessons stay locked (sequential among themselves).
    uid = fresh_db
    cid = await db.create_course(uid, "ko", "A1")
    await db.seed_foundation_units(cid, F.build_units("ko"))
    # Add two AI (pending) lessons.
    await db.create_lesson(cid, 100, "Vocab 1", "", [], {"segments": []}, "")
    await db.create_lesson(cid, 101, "Vocab 2", "", [], {"segments": []}, "")
    course = await db.get_course(uid, cid)
    pending = next(u for u in course["units"] if u.get("in_progress"))
    statuses = [l["status"] for l in pending["lessons"]]
    assert statuses[0] == "available"     # first AI lesson reachable despite foundations
    assert statuses[1] == "locked"        # second still gated behind the first


@pytest.mark.asyncio
async def test_non_foundation_lang_seeds_nothing(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "fr", "A1")
    await db.seed_foundation_units(cid, F.build_units("fr"))   # build_units → []
    course = await db.get_course(uid, cid)
    assert all(u.get("theme") != "foundations" for u in course["units"])
