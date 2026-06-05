-- The grammar artifact shape changed: teach content is now an LLM-authored list
-- of typed BLOCKS (prose / table / examples / contrast / note) instead of the old
-- explain + tables + minimal_pairs fields. Clear cached artifacts + lessons so
-- they regenerate in the new shape.
DELETE FROM concept_content;

UPDATE course_lessons
SET content = NULL
WHERE concepts_introduced IS NOT NULL AND concepts_introduced != '[]';
