-- Clear cached grammar artifacts + lessons so they regenerate with reference
-- tables on the teach screen (engine-computed verb-conjugation paradigm +
-- critic-verified paradigm tables).
DELETE FROM concept_content;

UPDATE course_lessons
SET content = NULL
WHERE concepts_introduced IS NOT NULL AND concepts_introduced != '[]';
