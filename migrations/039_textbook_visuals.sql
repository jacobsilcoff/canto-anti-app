-- Page-linked embedded images/diagrams extracted from textbook PDFs.
-- The JPEG bytes live in the existing media directory/table; this column keeps
-- lightweight ownership + page metadata for source review and lesson planning.
ALTER TABLE textbooks ADD COLUMN images_json TEXT NOT NULL DEFAULT '[]';
