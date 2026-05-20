import pytest
from srs import update

BASE = {"repetitions": 0, "interval_days": 1, "ease_factor": 2.5}


def test_again_resets_reps():
    result = update(BASE, "again")
    assert result["repetitions"] == 0
    assert result["interval_days"] == 1


def test_again_lowers_ease():
    result = update(BASE, "again")
    assert result["ease_factor"] < BASE["ease_factor"]


def test_ease_floor():
    low_ease = {**BASE, "ease_factor": 1.3}
    result = update(low_ease, "again")
    assert result["ease_factor"] >= 1.3


def test_good_first_rep():
    result = update(BASE, "good")
    assert result["repetitions"] == 1
    assert result["interval_days"] == 1


def test_good_second_rep():
    card = {**BASE, "repetitions": 1, "interval_days": 1}
    result = update(card, "good")
    assert result["interval_days"] == 6


def test_good_subsequent_rep_uses_ease():
    card = {"repetitions": 2, "interval_days": 6, "ease_factor": 2.5}
    result = update(card, "good")
    assert result["interval_days"] == round(6 * 2.5)


def test_easy_increases_ease():
    result = update(BASE, "easy")
    assert result["ease_factor"] > BASE["ease_factor"]


def test_hard_keeps_reps_going():
    result = update(BASE, "hard")
    assert result["repetitions"] == 1


def test_next_review_is_string():
    result = update(BASE, "good")
    assert isinstance(result["next_review"], str)
    assert len(result["next_review"]) == 19  # "YYYY-MM-DD HH:MM:SS"
