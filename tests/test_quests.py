"""Phase 2 of the lesson redesign: daily quests (B1), unit checkpoints (B3),
and the adjustable daily XP goal (D1)."""
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


# ── Quest rotation ────────────────────────────────────────────────────────────

def test_quest_keys_deterministic_and_distinct():
    keys = db.quest_keys_for(7, "2026-07-05")
    assert keys == db.quest_keys_for(7, "2026-07-05")   # stable for user+day
    assert len(keys) == 3
    assert len(set(keys)) == 3
    assert keys[0] == "earn_xp"                          # the anchor quest
    assert all(k in db.QUEST_TEMPLATES for k in keys)
    # Different day (or user) rotates the non-anchor picks eventually.
    other_days = {tuple(db.quest_keys_for(7, f"2026-07-{d:02d}")) for d in range(1, 15)}
    assert len(other_days) > 1


def test_chest_xp_deterministic_and_bounded():
    xp = db.chest_xp_for(7, "2026-07-05")
    assert xp == db.chest_xp_for(7, "2026-07-05")
    assert db.CHEST_XP_MIN <= xp <= db.CHEST_XP_MAX


# ── Seeding + progress ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_daily_quests_seeds_once(fresh_db):
    uid = fresh_db
    quests = await db.get_daily_quests(uid)
    assert len(quests) == 3
    assert all(q["progress"] == 0 and not q["done"] and not q["claimed"] for q in quests)
    # Second call returns the same rows (no duplicate seeding).
    again = await db.get_daily_quests(uid)
    assert [q["key"] for q in again] == [q["key"] for q in quests]


@pytest.mark.asyncio
async def test_bump_quest_sum_and_max(fresh_db):
    uid = fresh_db
    quests = await db.get_daily_quests(uid)
    keys = {q["key"] for q in quests}

    # Sum-mode quest present today (any non-combo rotation pick) accumulates.
    sum_key = next((k for k in keys if k != "earn_xp" and db.QUEST_TEMPLATES[k]["mode"] == "sum"), None)
    if sum_key:
        await db.bump_quest(uid, sum_key, 1)
        await db.bump_quest(uid, sum_key, 2)
        q = next(q for q in await db.get_daily_quests(uid) if q["key"] == sum_key)
        assert q["progress"] == min(3, q["target"])   # payload clamps to target

    # Max-mode (combo) tracks a high-water mark, never decreasing.
    if "combo" in keys:
        await db.bump_quest(uid, "combo", value=4)
        await db.bump_quest(uid, "combo", value=2)
        q = next(q for q in await db.get_daily_quests(uid) if q["key"] == "combo")
        assert q["progress"] == 4

    # Unknown key and un-seeded keys are safe no-ops.
    await db.bump_quest(uid, "not_a_quest", 5)
    unseeded = next((k for k in db.QUEST_TEMPLATES if k not in keys), None)
    if unseeded:
        await db.bump_quest(uid, unseeded, 5, value=5)
        assert {q["key"] for q in await db.get_daily_quests(uid)} == keys


@pytest.mark.asyncio
async def test_earn_xp_quest_syncs_from_ledger(fresh_db):
    uid = fresh_db
    await db.get_daily_quests(uid)
    await db.add_points(uid, "fr", 25, "lesson")
    q = next(q for q in await db.get_daily_quests(uid) if q["key"] == "earn_xp")
    assert q["progress"] == 25
    await db.add_points(uid, "yue", 30, "review")   # all languages count
    q = next(q for q in await db.get_daily_quests(uid) if q["key"] == "earn_xp")
    assert q["progress"] == 40                       # clamped to target
    assert q["done"]


@pytest.mark.asyncio
async def test_progress_clamped_to_target_in_payload(fresh_db):
    uid = fresh_db
    quests = await db.get_daily_quests(uid)
    key = next(q["key"] for q in quests if q["key"] != "earn_xp" and db.QUEST_TEMPLATES[q["key"]]["mode"] == "sum")
    await db.bump_quest(uid, key, 999)
    q = next(q for q in await db.get_daily_quests(uid) if q["key"] == key)
    assert q["progress"] == q["target"]
    assert q["done"]


# ── Chest ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chest_claim_requires_all_done_and_claims_once(fresh_db):
    uid = fresh_db
    quests = await db.get_daily_quests(uid)
    assert await db.claim_daily_chest(uid) is None   # nothing done yet

    # Complete every quest (earn_xp via the ledger, the rest via bumps).
    await db.add_points(uid, "fr", 100, "lesson")
    for q in quests:
        if q["key"] == "earn_xp":
            continue
        tpl = db.QUEST_TEMPLATES[q["key"]]
        if tpl["mode"] == "max":
            await db.bump_quest(uid, q["key"], value=q["target"])
        else:
            await db.bump_quest(uid, q["key"], q["target"])

    xp = await db.claim_daily_chest(uid)
    assert xp is not None and db.CHEST_XP_MIN <= xp <= db.CHEST_XP_MAX
    assert await db.claim_daily_chest(uid) is None   # already claimed
    assert all(q["claimed"] for q in await db.get_daily_quests(uid))


# ── Unit checkpoints ──────────────────────────────────────────────────────────

def _drill(t, **kw):
    """A minimally PLAYABLE stored drill.

    Option-bearing kinds carry real options and an answer index: the checkpoint
    builder now vets each drill (an unanswerable one is the worst thing to meet
    in a quiz), so a shape-only stub would be filtered out before the sampling
    this file is actually testing.
    """
    ex = {"type": t, "concept_key": kw.pop("key", "k"), **kw}
    if t in ("choice", "listening") and "options" not in ex:
        ex["options"] = ["une pomme", "un livre"]
        ex["answer"] = 0
        if t == "listening":
            ex["audio"] = "une pomme"
        elif ex.get("prompt_lang") == "target" or ex.get("is_cloze"):
            ex.setdefault("prompt", "Je mange ___ rouge")
        else:
            ex.setdefault("prompt", "an apple")
    return ex


async def _unit_with_lessons(uid, lang="fr", n_lessons=3, per_lesson=4):
    cid = await db.create_course(uid, lang, "A1")
    for i in range(n_lessons):
        exercises = [
            _drill("choice", prompt_lang="english"),
            _drill("choice", is_cloze=True),
            _drill("word_bank"),
            _drill("listening"),
            _drill("construction_drill"),   # never sampled
        ][:per_lesson + 1]
        await db.create_lesson(
            cid, i + 1, f"L{i+1}", "obj",
            [{"kind": "vocab", "key": f"w{i}", "label": f"mot{i}", "gloss": "word"}],
            {"segments": [{"teach": None, "exercises": exercises}]},
            "summary",
        )
    unit_id = await db.close_unit(cid, "Unit One", "the first unit")
    return cid, unit_id


@pytest.mark.asyncio
async def test_checkpoint_pool_ownership_and_shape(fresh_db):
    uid = fresh_db
    _, unit_id = await _unit_with_lessons(uid)
    unit = await db.get_unit_checkpoint_pool(uid, unit_id)
    assert unit and unit["title"] == "Unit One"
    assert len(unit["lessons"]) == 3
    assert unit["target_lang"] == "fr"
    # Another user can't reach it.
    other = await db.create_user("someone-else", auth.hash_password("pw"))
    assert await db.get_unit_checkpoint_pool(other, unit_id) is None


@pytest.mark.asyncio
async def test_checkpoint_quiz_deterministic_and_filtered(fresh_db):
    import main
    uid = fresh_db
    _, unit_id = await _unit_with_lessons(uid)
    unit = await db.get_unit_checkpoint_pool(uid, unit_id)
    quiz = main._checkpoint_quiz(unit)
    assert 4 <= len(quiz) <= main._CHECKPOINT_MAX_Q
    assert all(ex["type"] in main._CHECKPOINT_TYPES for ex in quiz)
    # Hardest kinds preferred: with 2 picks/lesson, word_bank + cloze win.
    assert all(ex["type"] == "word_bank" or ex.get("is_cloze") for ex in quiz)
    # Deterministic: same unit → same quiz, same order.
    quiz2 = main._checkpoint_quiz(await db.get_unit_checkpoint_pool(uid, unit_id))
    assert quiz == quiz2


@pytest.mark.asyncio
async def test_record_checkpoint_first_pass_once(fresh_db):
    uid = fresh_db
    _, unit_id = await _unit_with_lessons(uid)

    found, first = await db.record_checkpoint(uid, unit_id, 70, passed=False)
    assert found and not first
    found, first = await db.record_checkpoint(uid, unit_id, 90, passed=True)
    assert found and first                     # first pass → bonus XP
    found, first = await db.record_checkpoint(uid, unit_id, 100, passed=True)
    assert found and not first                 # replays never re-award

    unit = await db.get_unit_checkpoint_pool(uid, unit_id)
    assert unit["checkpoint_passed"] == 1
    assert unit["checkpoint_score"] == 100     # best score kept

    # Lower later score never downgrades.
    await db.record_checkpoint(uid, unit_id, 50, passed=False)
    unit = await db.get_unit_checkpoint_pool(uid, unit_id)
    assert unit["checkpoint_passed"] == 1 and unit["checkpoint_score"] == 100

    # Unknown unit → not found.
    found, first = await db.record_checkpoint(uid, 99999, 90, passed=True)
    assert not found


@pytest.mark.asyncio
async def test_course_units_expose_checkpoint_state(fresh_db):
    uid = fresh_db
    cid, unit_id = await _unit_with_lessons(uid)
    await db.record_checkpoint(uid, unit_id, 85, passed=True)
    course = await db.get_course(uid, cid)
    unit = next(u for u in course["units"] if u["id"] == unit_id)
    assert unit["checkpoint_passed"] == 1
    assert unit["checkpoint_score"] == 85


# ── Daily goal (D1) ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_goal_setting_and_clamp(fresh_db):
    import main
    uid = fresh_db
    assert await main._daily_goal(uid) == 50            # default
    await db.set_setting(uid, "daily_xp_goal", 100)
    assert await main._daily_goal(uid) == 100
    await db.set_setting(uid, "daily_xp_goal", 5)       # below floor
    assert await main._daily_goal(uid) == 10
    await db.set_setting(uid, "daily_xp_goal", "junk")  # malformed
    assert await main._daily_goal(uid) == 50
