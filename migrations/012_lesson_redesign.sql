-- Redesign course/lesson tables for one-lesson-at-a-time adaptive generation.
-- Drops all existing course data (units and lessons now created dynamically).
-- Keeps concept_content (shared grammar cache — expensive to regenerate).

DROP TABLE IF EXISTS course_progress;
DROP TABLE IF EXISTS course_concepts;
DROP TABLE IF EXISTS course_lessons;
DROP TABLE IF EXISTS course_units;
DROP TABLE IF EXISTS courses;

CREATE TABLE courses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    target_lang TEXT    NOT NULL,
    level       TEXT    NOT NULL DEFAULT 'A1',
    status      TEXT    NOT NULL DEFAULT 'active',
    created_at  TEXT             DEFAULT (datetime('now'))
);

-- Units are created dynamically when the LLM signals a unit boundary.
CREATE TABLE course_units (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  INTEGER NOT NULL,
    idx        INTEGER NOT NULL DEFAULT 0,
    title      TEXT    NOT NULL DEFAULT '',
    summary    TEXT    NOT NULL DEFAULT ''
);

-- Lessons are created one at a time. unit_id is NULL until the unit is closed.
CREATE TABLE course_lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id       INTEGER NOT NULL,
    unit_id         INTEGER,
    lesson_num      INTEGER NOT NULL DEFAULT 1,
    title           TEXT    NOT NULL DEFAULT '',
    objective       TEXT    NOT NULL DEFAULT '',
    content         TEXT,               -- JSON {segments:[...]}; set at creation
    concepts_json   TEXT    NOT NULL DEFAULT '[]',  -- [{kind,key,label,gloss}]
    summary         TEXT    NOT NULL DEFAULT '',
    llm_debug_json  TEXT,               -- {prompt, response} for the debug panel
    score           INTEGER,
    completed_at    TEXT
);

-- Concept registry: all concepts introduced across the course (for memory context
-- and distractor pools). Populated when a lesson is created.
CREATE TABLE course_concepts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id            INTEGER NOT NULL,
    kind                 TEXT    NOT NULL,
    key                  TEXT    NOT NULL,
    label                TEXT    NOT NULL DEFAULT '',
    gloss                TEXT    NOT NULL DEFAULT '',
    introduced_lesson_id INTEGER,
    UNIQUE(course_id, key)
);

CREATE INDEX IF NOT EXISTS idx_course_units_course   ON course_units(course_id);
CREATE INDEX IF NOT EXISTS idx_course_lessons_course ON course_lessons(course_id);
CREATE INDEX IF NOT EXISTS idx_course_concepts_course ON course_concepts(course_id);
