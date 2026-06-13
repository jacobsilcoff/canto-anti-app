-- Per-lesson crown level for the skill-tree path. Each completion bumps the
-- crown by 1 up to 3 (replays earn crowns but not XP). 0 = never completed.
ALTER TABLE course_lessons ADD COLUMN crown_level INTEGER NOT NULL DEFAULT 0;
