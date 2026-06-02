from datetime import datetime, timedelta, timezone

QUALITY = {"again": 0, "hard": 3, "good": 4, "easy": 5}

# Sub-day learning steps (in minutes). A new card — and any card that lapses —
# walks through these steps in the same study session before graduating to a
# day-level interval. This is what lets "again" bring a card back a minute later
# instead of pushing it a full day out of reach.
LEARNING_STEPS_MIN = [1, 10]

GRADUATING_INTERVAL_DAYS = 1   # interval when a card graduates with "good"
EASY_INTERVAL_DAYS = 4         # interval when a card graduates straight to "easy"

MIN_EASE = 1.3
HARD_FACTOR = 1.2              # interval multiplier for "hard" on a review card
EASY_BONUS = 1.3              # extra interval multiplier for "easy" on a review card


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _learning_state(step: int, reps: int, ease: float, minutes: int) -> dict:
    return {
        "repetitions": reps,
        "interval_days": 0,
        "ease_factor": round(ease, 4),
        "learning_step": step,
        "next_review": _fmt(_now() + timedelta(minutes=minutes)),
    }


def _graduate(reps: int, ease: float, interval_days: int) -> dict:
    return {
        "repetitions": reps + 1,
        "interval_days": interval_days,
        "ease_factor": round(ease, 4),
        "learning_step": None,
        "next_review": _fmt(_now() + timedelta(days=interval_days)),
    }


def _update_learning(quality: str, reps: int, ease: float, step: int) -> dict:
    """Card is in (re)learning — schedule with minute-level steps."""
    steps = LEARNING_STEPS_MIN

    if quality == "again":
        return _learning_state(0, 0, ease, steps[0])

    if quality == "easy":
        # Skip the remaining steps and graduate straight to the easy interval.
        return _graduate(reps, ease, EASY_INTERVAL_DAYS)

    if quality == "hard":
        # Repeat the current step rather than advancing.
        return _learning_state(step, reps, ease, steps[min(step, len(steps) - 1)])

    # "good" — advance to the next step, graduating once steps are exhausted.
    next_step = step + 1
    if next_step >= len(steps):
        return _graduate(reps, ease, GRADUATING_INTERVAL_DAYS)
    return _learning_state(next_step, reps, ease, steps[next_step])


def _update_review(quality: str, reps: int, ease: float, interval: int) -> dict:
    """Card has graduated — day-level SM-2 with hard/good/easy differentiation."""
    q = QUALITY[quality]

    if quality == "again":
        # Lapse: drop ease and send the card back through the learning steps.
        ease = max(MIN_EASE, ease - 0.2)
        return _learning_state(0, 0, ease, LEARNING_STEPS_MIN[0])

    if quality == "hard":
        ease = max(MIN_EASE, ease - 0.15)
        interval = max(1, round(interval * HARD_FACTOR))
    elif quality == "good":
        interval = max(1, round(interval * ease))
    else:  # easy
        ease = ease + 0.15
        interval = max(1, round(interval * ease * EASY_BONUS))

    return {
        "repetitions": reps + 1,
        "interval_days": interval,
        "ease_factor": round(ease, 4),
        "learning_step": None,
        "next_review": _fmt(_now() + timedelta(days=interval)),
    }


def update(card: dict, quality: str) -> dict:
    """SM-2 with sub-day learning steps. Returns updated fields (does not mutate card).

    A card is "in learning" when ``learning_step`` is set, or when it has never
    been seen (``first_seen_date`` is NULL) — a brand-new card enters learning at
    step 0. Graduated review cards have ``learning_step`` NULL and are scheduled
    in days. The returned dict always carries ``learning_step`` so callers can
    persist it and tell whether the card should reappear this session.
    """
    reps = card["repetitions"]
    interval = card["interval_days"]
    ease = card["ease_factor"]
    step = card.get("learning_step")

    if step is None and card.get("first_seen_date") is None:
        step = 0  # brand-new card entering learning

    if step is not None:
        return _update_learning(quality, reps, ease, step)
    return _update_review(quality, reps, ease, interval)
