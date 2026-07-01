#!/usr/bin/env python3
"""Restore a user's study streak by backfilling study_activity rows.

Streaks aren't stored as a number — `db.get_streak` derives them from the
`study_activity` table (one row per active UTC day). To make a user's current
streak equal to N, we ensure the last N consecutive days (ending today, UTC)
each have a row. This is the supported way to correct a streak that was lost to
the pre-fix timezone bug in `get_streak`.

Idempotent: uses INSERT OR IGNORE, so re-running is safe and won't double-count.

Usage (run on the host that has the SQLite DB, e.g. the prod/dev server):

    DB_PATH=data/cards.db python scripts/restore_streak.py swong 15

Args: <username> <streak_days>
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import aiosqlite

# Allow importing db from the repo root regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402


async def restore(username: str, streak_days: int) -> int:
    user = await db.get_user_by_username(username)
    if not user:
        raise SystemExit(f"No user named {username!r} found in {db.DB_PATH}")
    user_id = user["id"]

    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=n)).isoformat() for n in range(streak_days)]

    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.executemany(
            "INSERT OR IGNORE INTO study_activity (user_id, study_date) VALUES (?, ?)",
            [(user_id, d) for d in dates],
        )
        await conn.commit()

    new_streak = await db.get_streak(user_id)
    print(f"{username} (id={user_id}): backfilled {streak_days} days -> streak now {new_streak}")
    return new_streak


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python scripts/restore_streak.py <username> <streak_days>")
    username = sys.argv[1]
    streak_days = int(sys.argv[2])
    asyncio.run(restore(username, streak_days))


if __name__ == "__main__":
    main()
