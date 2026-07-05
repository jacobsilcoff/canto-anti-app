-- Phase 2 of the lesson redesign (docs/LESSON_REDESIGN.md): daily quests (B1)
-- and unit checkpoint quizzes (B3).

-- 3 rotating quests per user per day, seeded lazily on first GET /api/quests.
-- quest_date is the UTC day (date('now')) — same boundary as study_activity
-- and the daily-goal ring. claimed = the chest was opened (set on all of the
-- day's rows together).
CREATE TABLE IF NOT EXISTS daily_quests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    quest_date TEXT NOT NULL,
    quest_key  TEXT NOT NULL,
    target     INTEGER NOT NULL,
    progress   INTEGER NOT NULL DEFAULT 0,
    claimed    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, quest_date, quest_key)
);
CREATE INDEX IF NOT EXISTS idx_daily_quests_user_date ON daily_quests(user_id, quest_date);

-- Unit checkpoint quiz: pass >= 80% seals the unit with a gold shield.
ALTER TABLE course_units ADD COLUMN checkpoint_passed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE course_units ADD COLUMN checkpoint_score INTEGER;
