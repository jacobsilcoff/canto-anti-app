-- Add per-lesson summary column (populated after lesson generation;
-- used to give future lessons context about what has already been taught).
ALTER TABLE course_lessons ADD COLUMN summary TEXT;
