"""Phase 3 of the lesson redesign: friends weekly XP league (B2), lesson
length presets (A3), and the course focus dial (D2)."""
import os

import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import aiosqlite

import auth
import db
import learning


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


async def _befriend(a: int, b: int) -> None:
    assert (await db.send_friend_request(a, b))["ok"]
    pending = (await db.get_friends(b))["received"]
    fid = next(f["id"] for f in pending if f["user_id"] == a)
    assert await db.respond_friend_request(fid, b, accept=True) == a


# ── B2 · weekly league ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_league_empty_without_friends(fresh_db):
    assert await db.get_weekly_league(fresh_db) == []


@pytest.mark.asyncio
async def test_league_ranks_friends_this_week(fresh_db):
    me = fresh_db
    maya = await db.create_user("maya", auth.hash_password("pw"))
    kai = await db.create_user("kai", auth.hash_password("pw"))
    stranger = await db.create_user("stranger", auth.hash_password("pw"))
    await _befriend(me, maya)
    await _befriend(kai, me)          # direction doesn't matter

    await db.add_points(me, "fr", 30, "lesson")
    await db.add_points(maya, "yue", 70, "lesson")
    await db.add_points(stranger, "fr", 999, "lesson")   # not a friend — excluded
    # Last week's XP must not count: backdate a big award for kai.
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO points_ledger (user_id, lang, points, reason, created_at) "
            "VALUES (?, 'fr', 500, 'lesson', date('now', '-14 days'))",
            (kai,),
        )
        await conn.commit()

    league = await db.get_weekly_league(me)
    assert [r["username"] for r in league] == ["maya", "jsilcoff", "kai"]
    assert [r["xp"] for r in league] == [70, 30, 0]
    assert [r["rank"] for r in league] == [1, 2, 3]
    me_row = next(r for r in league if r["you"])
    assert me_row["username"] == "jsilcoff"
    assert not any(r["username"] == "stranger" for r in league)


# ── A3 · lesson length presets ────────────────────────────────────────────────

def _author_prompt(length):
    return learning._build_lesson_prompt(
        "fr", [{"kind": "vocab", "key": "w", "label": "mot", "gloss": "word"}],
        [], length=length,
    )


def test_lesson_length_quick_shrinks_the_budget():
    p = _author_prompt("quick")
    assert "4–6 drills" in p
    assert "exactly 2 STEPS" in p
    assert "ULTRA-COMPRESSED" in p


def test_lesson_length_thorough_grows_the_budget():
    p = _author_prompt("thorough")
    assert "10–14 drills" in p
    assert "THOROUGH" in p


def test_lesson_length_standard_matches_legacy_shape():
    p = _author_prompt("standard")
    assert "7–10 drills" in p
    assert "2–4 STEPS" in p
    # Unknown values fall back to standard rather than exploding.
    assert _author_prompt("bogus") == p


def test_lesson_length_review_budget():
    p = learning._build_lesson_prompt(
        "fr", [{"kind": "vocab", "key": "w", "label": "mot", "gloss": "word"}],
        [], review=[{"key": "old", "label": "vieux", "gloss": "old"}], length="quick",
    )
    assert "5–7 drills" in p


# ── D2 · course focus dial ────────────────────────────────────────────────────

def _plan_prompt(focus):
    return learning._build_plan_prompt("fr", "A1", [], [], course_focus=focus)


def test_course_focus_lines():
    assert "COURSE FOCUS" not in _plan_prompt("balanced")
    assert "GRAMMAR" in _plan_prompt("grammar")
    assert "VOCABULARY" in _plan_prompt("vocab")
    assert "CONVERSATION" in _plan_prompt("conversation")
    assert "COURSE FOCUS" not in _plan_prompt("bogus")   # unknown → no steering


def test_setting_validators_fall_back():
    import main
    assert main._valid_lesson_length("quick") == "quick"
    assert main._valid_lesson_length(None) == "standard"
    assert main._valid_lesson_length("huge") == "standard"
    assert main._valid_course_focus("conversation") == "conversation"
    assert main._valid_course_focus(None) == "balanced"
    assert main._valid_course_focus("x") == "balanced"
