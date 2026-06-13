-- Regenerate cached grammar content: conjugation tables now use the canonical
-- singular/plural layout, and generation runs on a more capable model
-- (grammar_lessons.GENERATION_MODEL) — both warrant a fresh build.
DELETE FROM concept_content;

UPDATE course_lessons
SET content = NULL
WHERE concepts_introduced IS NOT NULL AND concepts_introduced != '[]';
