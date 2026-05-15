from datetime import datetime, timedelta, timezone

QUALITY = {"again": 0, "hard": 3, "good": 4, "easy": 5}


def update(card: dict, quality: str) -> dict:
    """SM-2 algorithm. Returns updated fields (does not mutate card)."""
    q = QUALITY[quality]
    reps = card["repetitions"]
    interval = card["interval_days"]
    ease = card["ease_factor"]

    if q < 3:
        reps = 0
        interval = 1
    else:
        if reps == 0:
            interval = 1
        elif reps == 1:
            interval = 6
        else:
            interval = round(interval * ease)
        reps += 1

    ease = max(1.3, ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    next_review = (datetime.now(timezone.utc) + timedelta(days=interval)).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "repetitions": reps,
        "interval_days": interval,
        "ease_factor": round(ease, 4),
        "next_review": next_review,
    }
