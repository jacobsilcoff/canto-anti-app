-- Textbook library (import v2): uploaded books are kept as first-class records
-- with per-page extracted text + an editable chapter structure (page ranges),
-- so the user can review/correct the parse and generate more lessons from any
-- chapter on demand — no re-upload, no re-extraction.
CREATE TABLE IF NOT EXISTS textbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    num_pages INTEGER NOT NULL DEFAULT 0,
    pages_json TEXT NOT NULL DEFAULT '[]',      -- ["page 1 text", ...] cleaned
    chapters_json TEXT NOT NULL DEFAULT '[]',   -- [{title, start, end, status, lessons}]
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_textbooks_course ON textbooks(course_id);

-- Queue items remember which book chapter they came from, so deleting a book
-- can drop its still-queued lessons and the UI can badge the source.
ALTER TABLE lesson_queue ADD COLUMN textbook_id INTEGER;
ALTER TABLE lesson_queue ADD COLUMN chapter_idx INTEGER;
