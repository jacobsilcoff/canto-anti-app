-- 044_textbook_unit_chapter.sql
-- Tie each textbook course_unit back to the book chapter it was built from.
--
-- Until now the link lived only on lesson_queue rows, which are DELETED as each
-- lesson is authored — so a fully-generated unit had no way to say "I am chapter
-- 3 of this book". That made three things impossible: ordering textbook units by
-- the book's own chapter order, telling the learner a chapter already has a unit
-- (instead of silently building a second one), and regenerating a chapter's
-- lessons in place.

ALTER TABLE course_units ADD COLUMN textbook_id INTEGER;
ALTER TABLE course_units ADD COLUMN chapter_idx INTEGER;

-- Backfill from any still-queued rows (units whose queue drained keep NULLs;
-- they simply sort after the linked ones, exactly as they do today).
UPDATE course_units
SET textbook_id = (SELECT q.textbook_id FROM lesson_queue q
                   WHERE q.unit_id = course_units.id AND q.textbook_id IS NOT NULL
                   LIMIT 1),
    chapter_idx = (SELECT q.chapter_idx FROM lesson_queue q
                   WHERE q.unit_id = course_units.id AND q.chapter_idx IS NOT NULL
                   LIMIT 1)
WHERE theme = 'textbook';
