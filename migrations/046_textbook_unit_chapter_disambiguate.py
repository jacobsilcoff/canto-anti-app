"""046 — link the textbook units that migration 045 had to leave alone.

045 recovered a legacy unit's book/chapter by matching its title against the
course's chapter titles, but only when the match was UNIQUE. That is a weaker
guarantee than it sounds: textbook chapters are routinely called "Unit 1",
"Lesson 3" or "Introduction", so a learner with two books in one course hits the
ambiguous case easily — and an unlinked unit renders its chapter as "no lessons
yet" on the Learn page, right beside the lessons it already produced.

There IS enough information to decide. Every textbook lesson stores the author
prompt it was generated from (`course_lessons.llm_debug_json`), and that prompt
opens with the grounding header the route built:

    Textbook: <book title>
    Approved PDF pages: <start>–<end>
    Textbook unit: <unit title>

So the lesson itself names its book and page range. This has to run in Python
rather than SQL: `llm_debug_json` is a `json.dumps()` blob with the default
`ensure_ascii=True`, so a CJK book title is stored as `\\u7cb5\\u8a9e…` and even
the en-dash in the page range is `\\u2013`. A SQL `instr()` against the raw
column would match books with ASCII titles and silently miss every other one —
the worst possible failure for a data migration.

Three passes, most-certain first. Anything still undecided is left NULL, which
is the pre-045 status quo and safe.
"""
import json

# Written by main.create_textbook_unit / create_textbook_lesson into every
# queued lesson's `source`, which the author prompt embeds verbatim.
_BOOK_MARKER = "Textbook: {title}"
_PAGES_MARKER = "Approved PDF pages: {start}–{end}"


def _chapters(raw):
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [c for c in parsed if isinstance(c, dict)] if isinstance(parsed, list) else []


def _prompts(rows):
    """The author prompts stored for a unit's lessons, JSON-decoded so the text
    is comparable against real book titles rather than \\u escapes."""
    out = []
    for (raw,) in rows:
        try:
            debug = json.loads(raw or "{}")
        except (ValueError, TypeError):
            continue
        if isinstance(debug, dict) and isinstance(debug.get("prompt"), str):
            out.append(debug["prompt"])
    return out


def _names_book(prompts, title):
    marker = _BOOK_MARKER.format(title=title)
    return any(marker in p for p in prompts)


def _names_pages(prompts, chapter):
    try:
        marker = _PAGES_MARKER.format(start=int(chapter["start"]), end=int(chapter["end"]))
    except (KeyError, TypeError, ValueError):
        return False
    return any(marker in p for p in prompts)


async def migrate(conn) -> None:
    async with conn.execute(
        """SELECT u.id, u.title, u.course_id, c.user_id
           FROM course_units u JOIN courses c ON c.id = u.course_id
           WHERE u.theme = 'textbook' AND u.textbook_id IS NULL"""
    ) as cur:
        units = await cur.fetchall()
    if not units:
        return

    books_by_course: dict[int, list] = {}
    for _, _, course_id, user_id in units:
        if course_id in books_by_course:
            continue
        async with conn.execute(
            "SELECT id, title, chapters_json FROM textbooks WHERE course_id=? AND user_id=?",
            (course_id, user_id),
        ) as cur:
            books_by_course[course_id] = [
                {"id": r[0], "title": r[1] or "", "chapters": _chapters(r[2])}
                for r in await cur.fetchall()
            ]

    for unit_id, unit_title, course_id, _ in units:
        books = books_by_course.get(course_id) or []
        if not books:
            continue
        async with conn.execute(
            "SELECT llm_debug_json FROM course_lessons WHERE unit_id=?", (unit_id,)
        ) as cur:
            prompts = _prompts(await cur.fetchall())

        title = (unit_title or "").strip()
        candidates = [
            (b, i, ch) for b in books for i, ch in enumerate(b["chapters"])
            if (ch.get("title") or "").strip() == title and title
        ]

        # 1. Title matched exactly one chapter in the whole course (045's rule,
        #    repeated here because 045 may have been skipped or the book edited
        #    since).
        resolved = candidates[0] if len(candidates) == 1 else None

        # 2. Ambiguous title — keep the candidates the unit's own lessons point
        #    at. The book name and the page range are scored separately so that
        #    two same-titled chapters WITHIN one book are still separable.
        if resolved is None and candidates:
            scored = [(_names_book(prompts, b["title"]) * 2 + _names_pages(prompts, ch),
                       b, i, ch) for b, i, ch in candidates]
            best = max(s for s, *_ in scored)
            if best > 0:
                winners = [(b, i, ch) for s, b, i, ch in scored if s == best]
                if len(winners) == 1:
                    resolved = winners[0]

        if resolved is not None:
            book, chapter_idx, _ = resolved
            await conn.execute(
                "UPDATE course_units SET textbook_id=?, chapter_idx=? WHERE id=?",
                (book["id"], chapter_idx, unit_id))
            continue

        # 3. No chapter matches at all — a unit built from a custom page range
        #    ("Pages 30–34"), or one whose chapter was renamed since. It has no
        #    chapter to claim, but if its lessons name exactly one of the
        #    course's books we can still record WHICH BOOK it came from, so the
        #    Learn page files it under that book instead of "Other units".
        named = [b for b in books if _names_book(prompts, b["title"])]
        if len(named) == 1:
            await conn.execute(
                "UPDATE course_units SET textbook_id=? WHERE id=?",
                (named[0]["id"], unit_id))

    await conn.commit()
