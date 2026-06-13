-- Add per-lesson summary column (populated after lesson generation;
-- used to give future lessons context about what has already been taught).
-- NOTE: the baseline CREATE TABLE now includes this column, so on fresh DBs this
-- migration is a no-op (the runner swallows "duplicate column name" errors).
ALTER TABLE course_lessons ADD COLUMN summary TEXT;
