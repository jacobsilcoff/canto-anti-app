import pytest
from srs import (
    update,
    LEARNING_STEPS_MIN,
    GRADUATING_INTERVAL_DAYS,
    EASY_INTERVAL_DAYS,
    MIN_EASE,
)

# A brand-new card: never seen, no learning_step yet.
NEW = {"repetitions": 0, "interval_days": 1, "ease_factor": 2.5,
       "first_seen_date": None, "learning_step": None}


def _review_card(interval=10, ease=2.5, reps=3):
    """A graduated review card (out of learning)."""
    return {"repetitions": reps, "interval_days": interval, "ease_factor": ease,
            "first_seen_date": "2026-01-01", "learning_step": None}


def _learning_card(step=0, ease=2.5):
    """A card partway through the learning steps."""
    return {"repetitions": 0, "interval_days": 0, "ease_factor": ease,
            "first_seen_date": "2026-06-01", "learning_step": step}


# ── New cards enter learning (sub-day steps) ──────────────────────────────────

def test_new_card_enters_learning():
    result = update(NEW, "good")
    assert result["learning_step"] == 1          # advanced to second step
    assert result["interval_days"] == 0          # still sub-day


def test_new_good_schedules_within_minutes():
    result = update(NEW, "good")
    # Next step is minutes out, not a full day — so it can reappear this session.
    assert result["next_review"] > "0"
    assert result["interval_days"] == 0


def test_again_keeps_card_in_learning_step_zero():
    result = update(_learning_card(step=1), "again")
    assert result["learning_step"] == 0          # back to the first step
    assert result["interval_days"] == 0          # minutes away, not a day


def test_hard_repeats_current_learning_step():
    result = update(_learning_card(step=0), "hard")
    assert result["learning_step"] == 0
    assert result["interval_days"] == 0


# ── Graduation ────────────────────────────────────────────────────────────────

def test_good_graduates_after_last_step():
    last = len(LEARNING_STEPS_MIN) - 1
    result = update(_learning_card(step=last), "good")
    assert result["learning_step"] is None
    assert result["interval_days"] == GRADUATING_INTERVAL_DAYS
    assert result["repetitions"] == 1


def test_easy_graduates_immediately_with_longer_interval():
    result = update(NEW, "easy")
    assert result["learning_step"] is None
    assert result["interval_days"] == EASY_INTERVAL_DAYS
    assert result["interval_days"] > GRADUATING_INTERVAL_DAYS


# ── Review cards (graduated) ──────────────────────────────────────────────────

def test_review_good_multiplies_by_ease():
    card = _review_card(interval=10, ease=2.5)
    result = update(card, "good")
    assert result["interval_days"] == round(10 * 2.5)
    assert result["learning_step"] is None


def test_review_easy_beats_good():
    good = update(_review_card(interval=10, ease=2.5), "good")
    easy = update(_review_card(interval=10, ease=2.5), "easy")
    assert easy["interval_days"] > good["interval_days"]


def test_review_hard_smaller_than_good():
    hard = update(_review_card(interval=10, ease=2.5), "hard")
    good = update(_review_card(interval=10, ease=2.5), "good")
    assert hard["interval_days"] < good["interval_days"]
    assert hard["ease_factor"] < 2.5


def test_review_again_lapses_into_learning():
    result = update(_review_card(interval=30, ease=2.5), "again")
    assert result["learning_step"] == 0          # relearning from the first step
    assert result["interval_days"] == 0
    assert result["repetitions"] == 0
    assert result["ease_factor"] < 2.5           # ease penalised on lapse


def test_lapse_respects_ease_floor():
    result = update(_review_card(interval=30, ease=MIN_EASE), "again")
    assert result["ease_factor"] >= MIN_EASE


def test_easy_increases_ease_on_review():
    result = update(_review_card(interval=10, ease=2.5), "easy")
    assert result["ease_factor"] > 2.5


# ── Output shape ──────────────────────────────────────────────────────────────

def test_next_review_is_string():
    result = update(NEW, "good")
    assert isinstance(result["next_review"], str)
    assert len(result["next_review"]) == 19      # "YYYY-MM-DD HH:MM:SS"


def test_result_always_carries_learning_step():
    for q in ("again", "hard", "good", "easy"):
        assert "learning_step" in update(NEW, q)
        assert "learning_step" in update(_review_card(), q)
