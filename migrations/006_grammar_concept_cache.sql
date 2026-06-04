-- Shared, verified grammar content cache (generator + critic pipeline) and a
-- re-clear of cached vocab lessons so they regenerate with the grammar-first
-- template (explicit rule + minimal pairs + cloze/reorder drills).
CREATE TABLE IF NOT EXISTS concept_content (
    lang TEXT NOT NULL,
    concept_key TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (lang, concept_key)
);

UPDATE course_lessons
SET content = NULL
WHERE concepts_introduced IS NOT NULL AND concepts_introduced != '[]';
