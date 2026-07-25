-- 042_textbook_units.sql
-- Separate textbook units from the AI course path.
--
-- Textbook-import lessons used to be authored through the SHARED just-in-time
-- path (main._author_next_lesson peeked lesson_queue and drained it into the AI
-- course's active_plan), which interleaved them into the AI skill-tree path and
-- produced the confusing "N lessons remain in a batch" banner. We now scope each
-- queued lesson to its own textbook course_units row (theme='textbook') and
-- author it via a dedicated per-unit route; the AI path no longer touches the
-- queue.
--
-- This migration (1) adds lesson_queue.unit_id and (2) adopts any in-flight
-- queued batches into freshly-created textbook units so nothing strands once the
-- AI path stops consuming the queue.

ALTER TABLE lesson_queue ADD COLUMN unit_id INTEGER;

-- (2a) One textbook unit per distinct still-queued (course_id, unit_title) batch.
-- idx = (max existing unit idx in the course) + this group's 1-based rank among
-- the course's distinct queued titles, so units keep a stable trailing order.
INSERT INTO course_units (course_id, idx, title, summary, theme)
SELECT g.course_id,
       (SELECT COALESCE(MAX(u.idx), -1) FROM course_units u WHERE u.course_id = g.course_id)
         + (SELECT COUNT(*) FROM (SELECT DISTINCT course_id, unit_title
                                  FROM lesson_queue WHERE unit_id IS NULL) d
            WHERE d.course_id = g.course_id AND d.unit_title <= g.unit_title),
       TRIM(REPLACE(g.unit_title, '📕 ', '')),
       '',
       'textbook'
FROM (SELECT DISTINCT course_id, unit_title FROM lesson_queue WHERE unit_id IS NULL) g;

-- (2b) Link each still-queued row to the textbook unit created for its batch.
UPDATE lesson_queue
SET unit_id = (
    SELECT u.id FROM course_units u
    WHERE u.course_id = lesson_queue.course_id
      AND u.theme = 'textbook'
      AND u.title = TRIM(REPLACE(lesson_queue.unit_title, '📕 ', ''))
    ORDER BY u.id DESC LIMIT 1
)
WHERE unit_id IS NULL;
