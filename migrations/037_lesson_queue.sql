-- Textbook import: queued lesson specs consumed one-by-one by lesson generation
-- (the book IS the plan — the planner is skipped while the queue is non-empty).
CREATE TABLE IF NOT EXISTS lesson_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    unit_title TEXT NOT NULL DEFAULT '',
    unit_size INTEGER NOT NULL DEFAULT 1,
    spec_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lesson_queue_course ON lesson_queue(course_id, idx);
