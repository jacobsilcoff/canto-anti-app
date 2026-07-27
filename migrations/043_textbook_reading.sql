-- 043_textbook_reading.sql
-- Reading progress + bookmarks for the dedicated textbook reader (chapter-by-
-- chapter page-image reading). Textbook rows are already per-user, so this lives
-- on the textbooks row itself.

ALTER TABLE textbooks ADD COLUMN last_page INTEGER NOT NULL DEFAULT 0;
ALTER TABLE textbooks ADD COLUMN bookmarks_json TEXT NOT NULL DEFAULT '[]';
