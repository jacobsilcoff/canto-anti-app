-- Drop cached grammar artifacts (and the lessons that embed them) so they
-- regenerate after the reflexive-verb fix: the engine no longer conjugates
-- reflexive/pronominal verbs (s'appeler → wrong "s'appelent"), the conjugation
-- override only fires when the blank really is the verb, and a sanity check
-- rejects clozes that duplicate the answer word.
DELETE FROM concept_content;

UPDATE course_lessons
SET content = NULL
WHERE concepts_introduced IS NOT NULL AND concepts_introduced != '[]';
