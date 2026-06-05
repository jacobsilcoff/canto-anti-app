-- Clear cached VOCAB lesson content so lessons regenerate with the latest
-- exercise generation (grammar separated from vocab, labeled conjugation drills,
-- no ambiguous "say X" questions). Foundations lessons have no concepts
-- (concepts_introduced = '[]') and keep their pre-built content.
UPDATE course_lessons
SET content = NULL
WHERE concepts_introduced IS NOT NULL AND concepts_introduced != '[]';
