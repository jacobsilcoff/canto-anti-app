"""Tests for the textbook import v2 (textbook.py + db textbook CRUD).

Deterministic parts only: page cleanup (repeated-text-layer collapse, running
header/footer stripping), the chapter-detection skeleton + normalization,
lesson-spec normalization, per-chapter segmentation orchestration (LLM faked),
and the textbooks table round-trip."""
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

import auth
import db
import main
import textbook


# ── Page cleanup ──────────────────────────────────────────────────────────────

def test_clean_page_text_collapses_layer_artifact():
    # The classic 4×-styled-text artifact (seen in real CLC PDFs).
    assert textbook.clean_page_text(
        "jóu  sàhnjóu  sàhnjóu  sàhnjóu  sàhn") == "jóu  sàhn"
    assert textbook.clean_page_text("wáiwáiwáiwái") == "wái"
    # Nested repeats collapse layer by layer.
    assert textbook.clean_page_text("néih hónéih hónéih hónéih hóuuuu") == "néih hóu"


def test_clean_page_text_preserves_numbers_and_prose():
    assert textbook.clean_page_text("10000 dollars") == "10000 dollars"
    assert textbook.clean_page_text("in 2000, the year 2000") == "in 2000, the year 2000"
    prose = "the cat sat on the mat and then the cat left"
    assert textbook.clean_page_text(prose) == prose
    # Under 4 repetitions is left alone (could be legitimate).
    assert textbook.clean_page_text("very, very good") == "very, very good"


def test_strip_repeated_lines_drops_headers_and_page_numbers():
    words = ["apple", "boat", "cloud", "door", "echo", "fern", "gate",
             "hill", "iris", "jade", "kelp", "lark", "moss", "nest",
             "opal", "pine", "quill", "reef", "sage", "tide"]
    pages = [
        f"http://example.com  P a g e {i}\nreal content about {words[i - 1]}\n{i}\n"
        f"Unit {i % 3}    {i}"
        for i in range(1, 21)
    ]
    out = textbook.strip_repeated_lines(pages)
    joined = "\n".join(out)
    assert "example.com" not in joined            # watermark gone
    assert "Unit" not in joined                   # running header gone
    assert "real content about hill" in joined    # content survives
    for line in out[6].splitlines():              # bare page numbers gone
        assert line.strip() != "7"


def test_strip_repeated_lines_short_books_untouched():
    pages = ["Title\ncontent", "Title\nmore content"]   # 2 pages: no stats
    assert textbook.strip_repeated_lines(pages) == pages


# ── Chapter-detection skeleton + normalization ────────────────────────────────

def test_build_skeleton_tags_pages_and_picks_headings():
    pages = ["\n".join(["some prose line one", "more prose", "even more",
                        "yet more prose", "UNIT SEVEN"]),
             "", "second page text"]
    sk = textbook.build_skeleton(pages)
    assert "[p.1]" in sk and "[p.3]" in sk
    assert "UNIT SEVEN" in sk            # heading-like line beyond the top lines
    assert "[p.2]" not in sk             # empty pages skipped


def test_build_skeleton_adapts_to_cap_without_losing_the_tail():
    pages = ["\n".join(f"line {j} on page {i}" for j in range(6))
             for i in range(1, 301)]
    sk = textbook.build_skeleton(pages, cap=9000)
    assert len(sk) <= 9000
    assert "[p.300]" in sk               # the tail survives (fewer lines/page)


def test_norm_chapters_clamps_sorts_and_resolves_overlaps():
    parsed = {"chapters": [
        {"title": "B", "start": 8, "end": 30},          # end clamped to book
        {"title": "A", "start": -5, "end": 9},          # start clamped to 1, overlaps B
        {"title": "", "start": 1, "end": 2},            # no title → dropped
        {"title": "bad", "start": "x", "end": 2},       # malformed → dropped
        {"title": "C", "start": 12, "end": 11},         # empty range → dropped
        "nope",
    ]}
    out = textbook._norm_chapters(parsed, num_pages=20)
    assert [(c["title"], c["start"], c["end"]) for c in out] == [
        ("A", 1, 9), ("B", 10, 20)]      # sorted; overlap resolved forward
    assert all(c["status"] == "" for c in out)


def test_norm_chapters_preserves_valid_status():
    out = textbook._norm_chapters({"chapters": [
        {"title": "Done", "start": 1, "end": 3, "status": "queued"},
        {"title": "Hacked", "start": 4, "end": 5, "status": "evil"},
    ]}, num_pages=10)
    assert out[0]["status"] == "queued"
    assert out[1]["status"] == ""


@pytest.mark.asyncio
async def test_detect_chapters_normalizes(monkeypatch):
    async def fake_call(prompt, **kw):
        assert "[p.1]" in prompt
        return json.dumps({"chapters": [
            {"title": "Ch 1", "start": 1, "end": 2, "skip_hint": True},
            {"title": "Ch 2", "start": 3, "end": 99},
        ]})
    monkeypatch.setattr(textbook.llm, "call", fake_call)
    chapters = await textbook.detect_chapters(
        ["page one text", "page two", "page three"], "fr", api_key="x", model="m")
    assert [(c["start"], c["end"]) for c in chapters] == [(1, 2), (3, 3)]
    assert chapters[0]["skip_hint"] is True


# ── Lesson-spec normalization ─────────────────────────────────────────────────

def test_norm_lessons_drops_malformed():
    parsed = {"lessons": [
        {"title": "Good", "skill": {"kind": "grammar", "key": "k1", "label": "L", "gloss": "g"},
         "target_items": [{"label": "un", "gloss": "one"}], "source": "s"},
        {"title": "No skill or items", "skill": {}, "target_items": []},   # dropped
        "not a dict",                                                      # dropped
        {"title": "", "skill": {"kind": "bogus_kind", "label": ""},
         "target_items": [{"label": "deux", "gloss": "two"}]},  # label falls back to item
    ]}
    lessons = textbook._norm_lessons(parsed)
    assert len(lessons) == 2
    assert lessons[0]["skill"]["kind"] == "grammar"
    # Unknown kind clamps to vocab; missing label falls back to the first item.
    assert lessons[1]["skill"]["kind"] == "vocab"
    assert lessons[1]["skill"]["label"] == "deux"
    assert lessons[1]["title"] == "deux"


# ── Chunking ──────────────────────────────────────────────────────────────────

def test_chunk_text_short_is_one_chunk():
    assert textbook.chunk_text("hello world") == ["hello world"]
    assert textbook.chunk_text("") == []


def test_chunk_text_splits_on_line_boundaries():
    text = "\n".join(f"line {i} " + "x" * 40 for i in range(400))   # ~19k chars
    chunks = textbook.chunk_text(text, size=9000)
    assert len(chunks) >= 2
    assert all(len(c) <= 9000 for c in chunks)
    got = [ln for c in chunks for ln in c.split("\n")]
    assert [ln.strip() for ln in got] == [ln.strip() for ln in text.split("\n")]


# ── Per-chapter segmentation (fake LLM) ───────────────────────────────────────

def _lesson(i):
    return {"title": f"L{i}", "skill": {"kind": "vocab", "key": f"k{i}",
                                        "label": f"w{i}", "gloss": "g"},
            "target_items": [{"label": f"w{i}", "gloss": "g"}], "source": f"s{i}"}


@pytest.mark.asyncio
async def test_segment_chapter_single_unit_from_chapter_title(monkeypatch):
    prompts = []

    async def fake_call(prompt, **kw):
        prompts.append(prompt)
        return json.dumps({"lessons": [_lesson(1), _lesson(2)]})
    monkeypatch.setattr(textbook.llm, "call", fake_call)

    pages = ["ignored front", "chapter body A", "chapter body B", "ignored back"]
    seg = await textbook.segment_chapter(pages, 2, 3, "fr", "Greetings",
                                         api_key="x", model="m")
    assert seg["llm_calls"] == 1
    # Only the chapter's pages reach the prompt.
    assert "chapter body A" in prompts[0] and "ignored front" not in prompts[0]
    assert '"Greetings"' in prompts[0]
    # One unit per chapter, titled after it, sized by its lesson count.
    assert [(i["unit_title"], i["unit_size"]) for i in seg["items"]] == [
        ("📕 Greetings", 2), ("📕 Greetings", 2)]
    assert seg["items"][0]["source"] == "s1"
    assert seg["items"][1]["spec"]["skill"]["key"] == "k2"


@pytest.mark.asyncio
async def test_segment_chapter_caps_lessons(monkeypatch):
    async def fake_call(prompt, **kw):
        return json.dumps({"lessons": [_lesson(i) for i in range(30)]})
    monkeypatch.setattr(textbook.llm, "call", fake_call)
    seg = await textbook.segment_chapter(["text"], 1, 1, "fr", "Big",
                                         api_key="x", model="m")
    assert len(seg["items"]) == textbook.MAX_LESSONS_PER_CHAPTER


@pytest.mark.asyncio
async def test_segment_chapter_empty_result(monkeypatch):
    async def fake_call(prompt, **kw):
        return json.dumps({"lessons": []})
    monkeypatch.setattr(textbook.llm, "call", fake_call)
    seg = await textbook.segment_chapter(["nothing teachable"], 1, 1, "fr", "X",
                                         api_key="x", model="m")
    assert seg["items"] == [] and seg["llm_calls"] == 1


@pytest.mark.asyncio
async def test_plan_source_lesson_uses_reviewed_text_and_guidance(monkeypatch):
    prompts = []

    async def fake_call(prompt, **kw):
        prompts.append(prompt)
        return json.dumps({"lesson": _lesson(7)})

    monkeypatch.setattr(textbook.llm, "call", fake_call)
    lesson = await textbook.plan_source_lesson(
        "— PDF page 12 —\nselected classifier examples", "yue",
        "Everyday Cantonese", 12, 14, "Focus on the classifiers",
        api_key="x", model="m")

    assert lesson["title"] == "L7"
    assert lesson["skill"]["key"] == "k7"
    assert "selected classifier examples" in prompts[0]
    assert "Focus on the classifiers" in prompts[0]
    assert "pages 12-14" in prompts[0]


@pytest.mark.asyncio
async def test_plan_source_lesson_rejects_empty_model_result(monkeypatch):
    async def fake_call(prompt, **kw):
        return json.dumps({"lesson": {}})

    monkeypatch.setattr(textbook.llm, "call", fake_call)
    with pytest.raises(ValueError, match="did not produce a teachable lesson"):
        await textbook.plan_source_lesson(
            "some reviewed text", "fr", "Book", 1, 1,
            api_key="x", model="m")


# ── textbooks table round-trip ────────────────────────────────────────────────

@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


@pytest.mark.asyncio
async def test_textbook_crud_roundtrip(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "yue", "A1")
    tb_id = await db.create_textbook(uid, cid, "Basic Cantonese", "bc.pdf",
                                     ["p1 text", "p2 text", "p3 text"])
    chapters = [{"title": "Ch 1", "start": 1, "end": 2, "skip_hint": False, "status": ""}]
    await db.update_textbook_chapters(tb_id, chapters)

    books = await db.list_textbooks(uid, cid)
    assert len(books) == 1
    assert books[0]["title"] == "Basic Cantonese"
    assert books[0]["num_pages"] == 3
    assert books[0]["chapters"][0]["title"] == "Ch 1"
    assert books[0]["chapters"][0]["queued"] == 0
    assert "pages" not in books[0]          # the list stays light

    book = await db.get_textbook(uid, tb_id)
    assert book["pages"] == ["p1 text", "p2 text", "p3 text"]

    # Queue two lessons tagged with the book's chapter 0.
    items = [{"unit_title": "📕 Ch 1", "unit_size": 2,
              "spec": {"title": f"T{i}"}, "source": ""} for i in range(2)]
    await db.add_lesson_queue(cid, items, textbook_id=tb_id, chapter_idx=0)
    books = await db.list_textbooks(uid, cid)
    assert books[0]["chapters"][0]["queued"] == 2

    # Ownership check: another user sees nothing.
    assert await db.get_textbook(uid + 1, tb_id) is None
    assert await db.delete_textbook(uid + 1, tb_id) is False

    # Deleting the book drops its still-queued lessons.
    assert await db.delete_textbook(uid, tb_id) is True
    assert await db.count_lesson_queue(cid) == 0
    assert await db.list_textbooks(uid, cid) == []


@pytest.mark.asyncio
async def test_one_off_textbook_lesson_goes_ahead_of_legacy_queue(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "yue", "A1")
    await db.add_lesson_queue(cid, [{
        "unit_title": "Old batch", "unit_size": 1,
        "spec": {"title": "Old"}, "source": "old",
    }])
    await db.add_lesson_queue(cid, [{
        "unit_title": "Approved source", "unit_size": 1,
        "spec": {"title": "New"}, "source": "new",
    }], front=True)

    first = await db.peek_lesson_queue(cid)
    assert first["unit_title"] == "Approved source"
    assert first["spec"]["title"] == "New"


@pytest.mark.asyncio
async def test_create_textbook_lesson_uses_edited_source_and_consumes_queue(
        fresh_db, monkeypatch):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    tb_id = await db.create_textbook(
        uid, cid, "My Book", "book.pdf", ["raw page one", "raw page two"])
    await db.update_textbook_chapters(tb_id, [{
        "title": "Greetings", "start": 1, "end": 2,
        "skip_hint": False, "status": "",
    }])
    access = SimpleNamespace(
        api_key="key", anthropic_key=None, model_reader="reader")

    async def fake_access(*args, **kwargs):
        return access

    async def fake_meter(*args, **kwargs):
        return False

    async def fake_model(*args, **kwargs):
        return "author"

    async def fake_plan(source, target_lang, book_title, start, end, guidance,
                        **kwargs):
        assert source == "edited approved text"
        assert guidance == "Focus on polite greetings"
        assert (target_lang, book_title, start, end) == (
            "yue", "My Book", 1, 2)
        return _lesson(9)

    async def fake_author(course, resolved_access, model, user_id):
        queued = await db.peek_lesson_queue(course["id"])
        assert queued["unit_title"] == "📕 Greetings"
        assert "edited approved text" in queued["source"]
        assert "Focus on polite greetings" in queued["source"]
        lesson_id = await db.create_lesson(
            course["id"], 1, "Polite greetings", "Say hello", [],
            {"segments": []}, "Greeting lesson")
        await db.pop_lesson_queue(queued["id"])
        return lesson_id

    monkeypatch.setattr(main, "_resolve_gemini", fake_access)
    monkeypatch.setattr(main, "_textbook_metering", fake_meter)
    monkeypatch.setattr(main, "_resolve_lesson_model", fake_model)
    monkeypatch.setattr(textbook, "plan_source_lesson", fake_plan)
    monkeypatch.setattr(main, "_author_next_lesson", fake_author)

    response = await main.create_textbook_lesson.__wrapped__(
        None, tb_id, {
            "start": 1, "end": 2, "source_text": "edited approved text",
            "guidance": "Focus on polite greetings",
            "section_title": "Greetings",
        }, user)

    assert response["title"] == "Polite greetings"
    assert response["source"]["start"] == 1
    assert await db.count_lesson_queue(cid) == 0

    async def failed_author(*args, **kwargs):
        assert await db.peek_lesson_queue(cid) is not None
        raise RuntimeError("author unavailable")

    monkeypatch.setattr(main, "_author_next_lesson", failed_author)
    with pytest.raises(RuntimeError, match="author unavailable"):
        await main.create_textbook_lesson.__wrapped__(
            None, tb_id, {
                "start": 1, "end": 2, "source_text": "edited approved text",
                "guidance": "Focus on polite greetings",
                "section_title": "Greetings",
            }, user)
    assert await db.count_lesson_queue(cid) == 0


@pytest.mark.asyncio
async def test_textbooks_survive_restart_but_not_course_delete(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "yue", "A1")
    tb_id = await db.create_textbook(uid, cid, "Book", "b.pdf", ["p1"])
    await db.update_textbook_chapters(
        tb_id, [{"title": "Ch", "start": 1, "end": 1, "skip_hint": False,
                 "status": "queued"}])
    await db.add_lesson_queue(cid, [{"unit_title": "📕 Ch", "unit_size": 1,
                                     "spec": {}, "source": ""}],
                              textbook_id=tb_id, chapter_idx=0)

    # Restart ("Reset lessons"): queue wiped, book kept, chapter status reset
    # so the user can re-generate from the stored parse.
    await db.delete_ai_lessons(cid)
    assert await db.count_lesson_queue(cid) == 0
    books = await db.list_textbooks(uid, cid)
    assert len(books) == 1
    assert books[0]["chapters"][0]["status"] == ""

    # Deleting the whole course removes its books too (no orphans).
    await db.delete_course(uid, cid)
    assert await db.list_textbooks(uid, cid) == []
