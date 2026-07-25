"""Tests for the textbook import v2 (textbook.py + db textbook CRUD).

Deterministic parts only: page cleanup (repeated-text-layer collapse, running
header/footer stripping), the chapter-detection skeleton + normalization,
lesson-spec normalization, per-chapter segmentation orchestration (LLM faked),
and the textbooks table round-trip."""
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from PIL import Image
import io

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


# ── Mid-page unit split boundaries ────────────────────────────────────────────

def test_norm_chapters_preserves_and_cleans_anchors():
    out = textbook._norm_chapters({"chapters": [
        {"title": "A", "start": 1, "end": 3,
         "start_anchor": "  Lesson\tOne  ", "end_anchor": "x" * 400},
    ]}, num_pages=10)
    assert out[0]["start_anchor"] == "Lesson One"        # whitespace collapsed
    assert len(out[0]["end_anchor"]) == textbook._ANCHOR_CAP


def test_norm_chapters_allows_anchored_boundary_page_share():
    out = textbook._norm_chapters({"chapters": [
        {"title": "A", "start": 1, "end": 12, "end_anchor": "Lesson 4"},
        {"title": "B", "start": 12, "end": 18, "start_anchor": "Lesson 4"},
    ]}, num_pages=30)
    # Both units keep page 12 (each trims its own half via the anchor).
    assert [(c["start"], c["end"]) for c in out] == [(1, 12), (12, 18)]


def test_norm_chapters_shares_boundary_only_when_anchored():
    # No anchor → the shared boundary page is still resolved forward as before.
    out = textbook._norm_chapters({"chapters": [
        {"title": "A", "start": 1, "end": 12},
        {"title": "B", "start": 12, "end": 18},
    ]}, num_pages=30)
    assert [(c["start"], c["end"]) for c in out] == [(1, 12), (13, 18)]


def test_split_page_trims_head_and_tail_by_anchor():
    page = "Unit 3 tail\nmore unit 3\nUnit 4 title\nunit 4 body"
    head, kept, tail = textbook.split_page(page, head_anchor="Unit 4 title")
    assert head == "Unit 3 tail\nmore unit 3"
    assert kept == "Unit 4 title\nunit 4 body"
    assert tail == ""
    head, kept, tail = textbook.split_page(page, tail_anchor="Unit 4 title")
    assert kept == "Unit 3 tail\nmore unit 3"
    assert tail == "Unit 4 title\nunit 4 body"


def test_split_page_whitespace_tolerant_and_missing_anchor():
    page = "First   line\nSecond line"
    _, kept, _ = textbook.split_page(page, head_anchor="second   line")
    assert kept == "Second line"
    # Unknown anchor keeps the whole page rather than crashing.
    head, kept, tail = textbook.split_page(page, head_anchor="nope")
    assert head == "" and kept == page and tail == ""


def test_unit_pages_only_trims_first_and_last():
    pages = ["p1 top\nUnit here\np1 more", "p2 all", "p3 keep\nNext unit\np3 tail"]
    segs = textbook.unit_pages(pages, 1, 3, start_anchor="Unit here",
                               end_anchor="Next unit")
    assert segs[0]["text"] == "Unit here\np1 more"
    assert segs[0]["head_trim"] == "p1 top"
    assert segs[1]["text"] == "p2 all"          # middle page untouched
    assert segs[2]["text"] == "p3 keep"
    assert segs[2]["tail_trim"] == "Next unit\np3 tail"


def test_unit_pages_single_page_carries_both_anchors():
    pages = ["prev tail\nMine starts\nmine body\nNext unit\nnext body"]
    segs = textbook.unit_pages(pages, 1, 1, start_anchor="Mine starts",
                               end_anchor="Next unit")
    assert segs[0]["head_trim"] == "prev tail"
    assert segs[0]["text"] == "Mine starts\nmine body"
    assert segs[0]["tail_trim"] == "Next unit\nnext body"


def test_format_unit_source_labels_and_trims():
    pages = ["intro\nUnit 4\nbody", "second page"]
    src = textbook.format_unit_source(pages, 1, 2, start_anchor="Unit 4")
    assert src == "— PDF page 1 —\nUnit 4\nbody\n\n— PDF page 2 —\nsecond page"


def test_chapter_anchors_falls_back_to_matching_chapter():
    # The /textbooks "Turn into lessons" action sends only {start,end}; a split
    # saved on the matching chapter must still reach generation.
    book = {"chapters": [
        {"title": "A", "start": 1, "end": 12, "end_anchor": "Lesson 4"},
        {"title": "B", "start": 12, "end": 18, "start_anchor": "Lesson 4"},
    ]}
    assert main._chapter_anchors(book, 12, 18) == ("Lesson 4", "")
    assert main._chapter_anchors(book, 1, 12) == ("", "Lesson 4")
    assert main._chapter_anchors(book, 3, 9) == ("", "")     # no chapter matches


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


@pytest.mark.asyncio
async def test_plan_source_lesson_sends_selected_visuals(monkeypatch):
    seen = {}

    async def fake_call_with_image(prompt, image_bytes, **kw):
        seen["prompt"] = prompt
        seen["image"] = image_bytes
        return json.dumps({"lesson": _lesson(8)})

    monkeypatch.setattr(textbook.llm, "call_with_image", fake_call_with_image)
    buf = io.BytesIO()
    Image.new("RGB", (500, 300), "green").save(buf, format="JPEG")
    lesson = await textbook.plan_source_lesson(
        "diagram explanation", "yue", "Visual Book", 4, 4,
        visuals=[{"pages": [4], "data": buf.getvalue()}],
        api_key="x", model="m")

    assert lesson["title"] == "L8"
    assert "contact sheet" in seen["prompt"]
    assert seen["image"].startswith(b"\xff\xd8")


def _source_unit_plan():
    first = _lesson(1)
    first.update({
        "covers": ["c1"],
        "source_excerpts": ["Good morning"],
        "teaching_notes": "Keep the book's greeting order.",
    })
    second = _lesson(2)
    second.update({
        "covers": ["c2"],
        "source_excerpts": ["see you again"],
    })
    return {
        "concept_inventory": [
            {"id": "c1", "label": "Morning greetings"},
            {"id": "c2", "label": "Goodbyes"},
        ],
        "lessons": [first, second],
    }


def test_source_unit_requires_exact_complete_coverage_and_verbatim_quotes():
    source = "Good morning\nSome explanation\nsee you again"
    plan = textbook._norm_source_unit(_source_unit_plan(), source)
    assert len(plan["lessons"]) == 2
    assert "Good morning" in plan["lessons"][0]["source"]
    assert plan["lessons"][1]["covers"] == ["c2"]

    incomplete = _source_unit_plan()
    incomplete["lessons"][1]["covers"] = ["c1"]
    with pytest.raises(ValueError, match="did not map.*cleanly"):
        textbook._norm_source_unit(incomplete, source)

    invented_quote = _source_unit_plan()
    invented_quote["lessons"][0]["source_excerpts"] = ["not in the book"]
    with pytest.raises(ValueError, match="did not map.*cleanly"):
        textbook._norm_source_unit(invented_quote, source)


@pytest.mark.asyncio
async def test_plan_source_unit_prompts_for_one_complete_multi_lesson_unit(monkeypatch):
    seen = {}

    async def fake_call(prompt, **kw):
        seen["prompt"] = prompt
        return json.dumps(_source_unit_plan())

    monkeypatch.setattr(textbook.llm, "call", fake_call)
    plan = await textbook.plan_source_unit(
        "Good morning\nSome explanation\nsee you again", "yue", "Daily",
        "Useful Expressions", 2, 10, "Follow every expression",
        api_key="x", model="m")
    assert len(plan["lessons"]) == 2
    assert "maps 1:1" in seen["prompt"]
    assert "exactly one lesson" in seen["prompt"]
    assert "VERBATIM" in seen["prompt"]
    assert "Follow every expression" in seen["prompt"]


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
    visuals = [{"id": "a" * 32, "pages": [2], "width": 640, "height": 420}]
    tb_id = await db.create_textbook(uid, cid, "Basic Cantonese", "bc.pdf",
                                     ["p1 text", "p2 text", "p3 text"], visuals)
    chapters = [{"title": "Ch 1", "start": 1, "end": 2, "skip_hint": False, "status": ""}]
    await db.update_textbook_chapters(tb_id, chapters)

    books = await db.list_textbooks(uid, cid)
    assert len(books) == 1
    assert books[0]["title"] == "Basic Cantonese"
    assert books[0]["num_pages"] == 3
    assert books[0]["chapters"][0]["title"] == "Ch 1"
    assert books[0]["chapters"][0]["queued"] == 0
    assert books[0]["visual_count"] == 1
    assert "pages" not in books[0]          # the list stays light

    book = await db.get_textbook(uid, tb_id)
    assert book["pages"] == ["p1 text", "p2 text", "p3 text"]
    assert book["visuals"] == visuals
    assert await db.rename_textbook(uid + 1, tb_id, "Nope") is False
    assert await db.rename_textbook(uid, tb_id, "Renamed Cantonese") is True
    assert (await db.get_textbook(uid, tb_id))["title"] == "Renamed Cantonese"
    assert await db.list_textbook_visual_ids(uid, cid) == ["a" * 32]

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
async def test_upload_saves_book_before_retryable_unit_detection(
        fresh_db, monkeypatch, tmp_path):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(
        main.extract, "extract_pdf_pages",
        lambda *args: {"title": "PDF title", "pages": ["page one", "page two"],
                       "visuals": []})
    monkeypatch.setattr(main.textbook, "clean_pages", lambda pages: pages)

    async def must_not_detect_during_upload(*args, **kwargs):
        raise AssertionError("AI detection must not block or discard the upload")

    monkeypatch.setattr(main.textbook, "detect_chapters", must_not_detect_during_upload)
    upload = main.UploadFile(filename="book.pdf", file=io.BytesIO(b"pdf"))
    result = await main.upload_textbook.__wrapped__(
        None, cid, upload, "My saved book", user)
    assert result["needs_analysis"] is True
    assert result["chapters"] == []
    saved = await db.get_textbook(uid, result["id"])
    assert saved["title"] == "My saved book"
    assert saved["pages"] == ["page one", "page two"]

    access = SimpleNamespace(
        api_key="key", anthropic_key=None, model_reader="reader")

    async def fake_access(*args, **kwargs):
        return access

    async def fake_meter(*args, **kwargs):
        return False

    async def detect_after_save(pages, *args, **kwargs):
        assert pages == ["page one", "page two"]
        return [{"title": "Unit one", "start": 1, "end": 2,
                 "skip_hint": False, "status": ""}]

    monkeypatch.setattr(main, "_resolve_gemini", fake_access)
    monkeypatch.setattr(main, "_textbook_metering", fake_meter)
    monkeypatch.setattr(main.textbook, "detect_chapters", detect_after_save)
    analyzed = await main.analyze_textbook.__wrapped__(None, result["id"], user)
    assert analyzed["chapters"][0]["title"] == "Unit one"
    saved = await db.get_textbook(uid, result["id"])
    assert saved["chapters"][0]["start"] == 1


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
        fresh_db, monkeypatch, tmp_path):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    visual_id = "b" * 32
    visual_buf = io.BytesIO()
    Image.new("RGB", (500, 300), "blue").save(visual_buf, format="JPEG")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    (tmp_path / f"{visual_id}.jpg").write_bytes(visual_buf.getvalue())
    tb_id = await db.create_textbook(
        uid, cid, "My Book", "book.pdf", ["raw page one", "raw page two"],
        [{"id": visual_id, "pages": [2], "width": 500, "height": 300}])
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
        assert len(kwargs["visuals"]) == 1
        assert kwargs["visuals"][0]["data"].startswith(b"\xff\xd8")
        return _lesson(9)

    async def fake_author(course, unit_id, resolved_access, model, user_id):
        queued = await db.peek_lesson_queue_for_unit(unit_id)
        assert queued["unit_title"] == "Greetings"
        assert "edited approved text" in queued["source"]
        assert "Focus on polite greetings" in queued["source"]
        lesson_id = await db.create_lesson(
            course["id"], 1, "Polite greetings", "Say hello", [],
            {"segments": []}, "Greeting lesson", unit_id=unit_id)
        await db.pop_lesson_queue(queued["id"])
        return lesson_id

    monkeypatch.setattr(main, "_resolve_gemini", fake_access)
    monkeypatch.setattr(main, "_textbook_metering", fake_meter)
    monkeypatch.setattr(main, "_resolve_lesson_model", fake_model)
    monkeypatch.setattr(textbook, "plan_source_lesson", fake_plan)
    monkeypatch.setattr(main, "_author_textbook_lesson", fake_author)

    response = await main.create_textbook_lesson.__wrapped__(
        None, tb_id, {
            "start": 1, "end": 2, "source_text": "edited approved text",
            "guidance": "Focus on polite greetings",
            "visual_ids": [visual_id],
            "section_title": "Greetings",
        }, user)

    assert response["title"] == "Polite greetings"
    assert response["source"]["start"] == 1
    assert response["source"]["visual_count"] == 1
    assert response["unit_id"]
    assert await db.count_lesson_queue(cid) == 0
    # The authored lesson landed in its own textbook unit.
    full = await db.get_course(uid, cid)
    assert [u for u in full["units"] if u.get("theme") == "textbook"]

    async def failed_author(*args, **kwargs):
        assert await db.count_lesson_queue(cid) > 0
        raise RuntimeError("author unavailable")

    monkeypatch.setattr(main, "_author_textbook_lesson", failed_author)
    with pytest.raises(RuntimeError, match="author unavailable"):
        await main.create_textbook_lesson.__wrapped__(
            None, tb_id, {
                "start": 1, "end": 2, "source_text": "edited approved text",
                "guidance": "Focus on polite greetings",
                "visual_ids": [visual_id],
                "section_title": "Greetings",
            }, user)
    assert await db.count_lesson_queue(cid) == 0


@pytest.mark.asyncio
async def test_create_textbook_unit_queues_complete_unit_and_authors_first(
        fresh_db, monkeypatch):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    tb_id = await db.create_textbook(
        uid, cid, "Daily", "daily.pdf", ["Good morning", "see you again"])
    await db.update_textbook_chapters(tb_id, [{
        "title": "Useful Expressions", "start": 1, "end": 2,
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

    async def fake_plan(*args, **kwargs):
        lessons = []
        for i, excerpt in enumerate(("Good morning", "see you again"), 1):
            lesson = _lesson(i)
            lesson["source"] = f"Verbatim textbook excerpts:\n• {excerpt}"
            lesson["covers"] = [f"c{i}"]
            lessons.append(lesson)
        return {
            "concept_inventory": [
                {"id": "c1", "label": "Greetings"},
                {"id": "c2", "label": "Goodbyes"},
            ],
            "lessons": lessons,
        }

    async def fake_author(course, unit_id, resolved_access, model, user_id):
        queued = await db.peek_lesson_queue_for_unit(unit_id)
        assert queued["unit_title"] == "Useful Expressions"
        assert "Good morning" in queued["source"]
        lesson_id = await db.create_lesson(
            course["id"], 1, "Greetings", "Greet people", [],
            {"segments": []}, "Greeting lesson", unit_id=unit_id)
        await db.pop_lesson_queue(queued["id"])
        return lesson_id

    monkeypatch.setattr(main, "_resolve_gemini", fake_access)
    monkeypatch.setattr(main, "_textbook_metering", fake_meter)
    monkeypatch.setattr(main, "_resolve_lesson_model", fake_model)
    monkeypatch.setattr(textbook, "plan_source_unit", fake_plan)
    monkeypatch.setattr(main, "_author_textbook_lesson", fake_author)

    response = await main.create_textbook_unit.__wrapped__(
        None, tb_id, {
            "start": 1, "end": 2,
            "source_text": "— PDF page 1 —\nGood morning\n\n"
                           "— PDF page 2 —\nsee you again",
            "section_title": "Useful Expressions",
        }, user)

    up = response["unit_plan"]
    unit_id = up.pop("unit_id")
    assert up == {
        "title": "Useful Expressions", "lesson_count": 2,
        "remaining": 1, "concept_count": 2,
    }
    # The remaining lesson stays queued, scoped to the new textbook unit.
    assert await db.count_lesson_queue_for_unit(unit_id) == 1
    remaining = await db.peek_lesson_queue_for_unit(unit_id)
    assert remaining["spec"]["title"] == "L2"
    full = await db.get_course(uid, cid)
    tb = [u for u in full["units"] if u.get("theme") == "textbook"]
    assert len(tb) == 1 and tb[0]["id"] == unit_id
    assert tb[0]["queued_remaining"] == 1 and len(tb[0]["lessons"]) == 1
    book = await db.get_textbook(uid, tb_id)
    assert book["chapters"][0]["status"] == "queued"

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


@pytest.mark.asyncio
async def test_textbook_reading_progress_and_bookmarks(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "yue", "A1")
    tb_id = await db.create_textbook(uid, cid, "Book", "b.pdf",
                                     ["p1", "p2", "p3"], pdf_media_id="ab" * 16)

    book = await db.get_textbook(uid, tb_id)
    assert book["last_page"] == 0 and book["bookmarks"] == [] and book["has_pdf"] is True

    # Partial update: page only, then bookmarks only (deduped + sorted + sanitized).
    assert await db.set_textbook_reading(uid, tb_id, last_page=2)
    assert await db.set_textbook_reading(uid, tb_id, bookmarks=[3, 1, 1, 0, -5])
    book = await db.get_textbook(uid, tb_id)
    assert book["last_page"] == 2 and book["bookmarks"] == [1, 3]

    # list_textbooks surfaces the same reading state + has_pdf flag.
    row = (await db.list_textbooks(uid, cid))[0]
    assert row["has_pdf"] is True and row["bookmarks"] == [1, 3]

    # Ownership-checked: another user can't read or write this book's state.
    other = await db.create_user("other", auth.hash_password("x"))
    assert await db.set_textbook_reading(other, tb_id, last_page=9) is False
    assert await db.get_textbook(other, tb_id) is None

    # No-op update returns False.
    assert await db.set_textbook_reading(uid, tb_id) is False


@pytest.mark.asyncio
async def test_textbook_unit_row_and_scoped_queue(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "yue", "A1")
    unit_id = await db.create_textbook_unit_row(cid, "Chapter 1")

    # Unit is ownership-checked and typed.
    unit = await db.get_textbook_unit(uid, unit_id)
    assert unit and unit["theme"] == "textbook" and unit["course_id"] == cid
    other = await db.create_user("other2", auth.hash_password("x"))
    assert await db.get_textbook_unit(other, unit_id) is None

    # Empty unit is removable; with a lesson it's kept.
    await db.add_lesson_queue(cid, [{"unit_title": "Chapter 1", "unit_size": 2,
                                     "spec": {"title": "L1"}, "source": "s"}],
                              unit_id=unit_id)
    assert await db.count_lesson_queue_for_unit(unit_id) == 1
    peek = await db.peek_lesson_queue_for_unit(unit_id)
    assert peek["spec"]["title"] == "L1"

    lid = await db.create_lesson(cid, 1, "L1", "", [], {"segments": []}, "", unit_id=unit_id)
    await db.delete_course_unit_if_empty(unit_id)   # has a lesson → kept
    assert await db.get_textbook_unit(uid, unit_id) is not None

    # Clearing the queue then deleting an empty unit works.
    unit2 = await db.create_textbook_unit_row(cid, "Chapter 2")
    await db.add_lesson_queue(cid, [{"unit_title": "Chapter 2", "unit_size": 1,
                                     "spec": {}, "source": ""}], unit_id=unit2)
    assert await db.clear_lesson_queue_for_unit(unit2) == 1
    await db.delete_course_unit_if_empty(unit2)      # no lesson → removed
    assert await db.get_textbook_unit(uid, unit2) is None
