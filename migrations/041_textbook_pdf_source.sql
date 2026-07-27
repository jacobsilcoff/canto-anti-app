-- Keep the uploaded PDF so its pages can be re-rendered on demand for AI
-- vision re-extraction (garbled / romanized / scanned books). Nullable: books
-- uploaded before this migration simply can't be re-read until re-uploaded.
ALTER TABLE textbooks ADD COLUMN pdf_media_id TEXT;
