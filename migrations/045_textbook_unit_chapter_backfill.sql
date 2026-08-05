-- 045_textbook_unit_chapter_backfill.sql
-- Link OLD textbook units back to the book chapter they came from, by title.
--
-- Migration 044 added course_units.textbook_id/chapter_idx and backfilled them
-- from lesson_queue — but those rows are DELETED as each lesson is authored, so
-- any unit whose queue had already drained kept NULLs. 044 accepted that ("they
-- simply sort after the linked ones"), which was harmless while the Learn page
-- listed units only.
--
-- It stopped being harmless once the Learn page became chapter-first: a chapter
-- is matched to its unit by textbook_id+chapter_idx, so an unlinked unit leaves
-- its chapter rendering as "no lessons yet" with a Build button — next to the
-- very lessons it already produced. The server's one-unit-per-chapter guard
-- (db.get_textbook_units) is blind to them for the same reason, so tapping it
-- would build a duplicate instead of offering "regenerate".
--
-- A textbook unit is titled with its chapter's title (main.create_textbook_unit
-- passes section_title, which the UI fills from the chapter), so matching on
-- title recovers the link. Only UNAMBIGUOUS matches are taken: exactly one
-- chapter, in exactly one book of the same course, with that exact title. Units
-- built from a custom page range ("Pages 12–20") match nothing and stay NULL,
-- which is correct — there is no chapter they belong to.

UPDATE course_units
SET textbook_id = (
        SELECT t.id FROM textbooks t,
             json_each(CASE WHEN json_valid(t.chapters_json)
                            THEN t.chapters_json ELSE '[]' END) c
        WHERE t.course_id = course_units.course_id
          AND t.user_id = (SELECT co.user_id FROM courses co
                           WHERE co.id = course_units.course_id)
          AND json_extract(c.value, '$.title') = course_units.title),
    chapter_idx = (
        SELECT c.key FROM textbooks t,
             json_each(CASE WHEN json_valid(t.chapters_json)
                            THEN t.chapters_json ELSE '[]' END) c
        WHERE t.course_id = course_units.course_id
          AND t.user_id = (SELECT co.user_id FROM courses co
                           WHERE co.id = course_units.course_id)
          AND json_extract(c.value, '$.title') = course_units.title)
WHERE theme = 'textbook'
  AND textbook_id IS NULL
  AND title != ''
  AND (SELECT COUNT(*) FROM textbooks t,
            json_each(CASE WHEN json_valid(t.chapters_json)
                           THEN t.chapters_json ELSE '[]' END) c
       WHERE t.course_id = course_units.course_id
         AND json_extract(c.value, '$.title') = course_units.title) = 1;
