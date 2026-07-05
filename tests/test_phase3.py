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


# ── A3 · lesson length = play-time tier subset (generation is length-independent) ─

def _author_prompt(**kw):
    return learning._build_lesson_prompt(
        "fr", [{"kind": "vocab", "key": "w", "label": "mot", "gloss": "word"}],
        [], **kw,
    )


def test_lessons_are_always_authored_at_max_depth():
    # Length no longer shapes generation — every lesson is authored thorough and the
    # player subsets it by tier. So the prompt always asks for the full budget.
    p = _author_prompt()
    assert "10–14 drills" in p
    assert "3–4 STEPS" in p


def test_author_prompt_requests_drill_tiers():
    p = _author_prompt()
    assert "TIER every drill" in p          # the tiering instruction
    assert '"tier":"core|standard|extra"' in p   # the drill schema field
    for t in ("core", "standard", "extra"):
        assert t in p


def test_review_lessons_get_the_larger_budget():
    p = _author_prompt(review=[{"key": "old", "label": "vieux", "gloss": "old"}])
    assert "12–16 drills" in p


def test_norm_tier_clamps_to_known_values():
    assert learning._norm_tier("core") == "core"
    assert learning._norm_tier("EXTRA") == "extra"
    assert learning._norm_tier(None) == "standard"      # missing → standard
    assert learning._norm_tier("bogus") == "standard"   # unknown → standard


def test_assembled_drills_carry_a_tier():
    authored = {"intro": "x", "vocab_glossary": {}, "steps": [{"title": "s", "teach": [], "drills": [
        {"kind": "recognition", "concept": "w", "tier": "core",
         "target": "mot", "gloss": "word", "distractors": ["dog", "cat", "sun"]},
        # no tier → defaults to standard so it survives standard/thorough, hidden in quick
        {"kind": "recognition", "concept": "w",
         "target": "chat", "gloss": "cat", "distractors": ["dog", "word", "sun"]},
    ]}]}
    concepts = [{"kind": "vocab", "key": "w", "label": "mot", "gloss": "word"}]
    content = learning.assemble_lesson("fr", concepts, authored)
    tiers = [e.get("tier") for e in content["segments"][0]["exercises"]]
    assert tiers == ["core", "standard"]


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
