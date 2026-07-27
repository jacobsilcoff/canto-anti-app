"""Tests for the vision-assisted PDF re-extraction path.

Covers the deterministic pieces (extraction-quality heuristic, PDF page
rendering, transcription orchestration with the LLM faked) plus the DB/route
wiring that stores the source PDF and overwrites garbled pages with clean
native-script transcripts."""
import io

import pytest
import pytest_asyncio
from pypdf import PdfWriter

import auth
import db
import extract
import main
import textbook


# ── Extraction-quality heuristic ──────────────────────────────────────────────

def test_page_quality_flags_failure_modes_not_clean_short_text():
    assert extract.page_text_quality("") == 1.0                     # blank spacer
    assert extract.page_text_quality("néih hóu — hello") > 0.9      # short but clean
    assert extract.page_text_quality("·|·| 12 //// ····" * 20) < 0.5  # symbol noise
    assert extract.page_text_quality("a" * 400) < 0.7               # merged Latin run
    assert extract.page_text_quality("· 9 ·") == 0.2                # near-empty scan


def test_native_script_ratio():
    assert extract.native_script_ratio("jóu sàhn hello") < 0.05     # romanization only
    assert extract.native_script_ratio("早晨你好 zou2") > 0.4          # mostly hanzi
    assert extract.native_script_ratio("") == 0.0


def test_assess_flags_romanization_only_book_for_non_latin_language():
    # A phrasebook that extracted cleanly but carries no native script: only the
    # language-aware check catches it (byte-level quality looks fine). Pages are
    # realistically long so the sparse-text heuristic doesn't fire instead.
    page = "\n".join(f"joi{i} gin{i} see you {i}, dou si gin later" for i in range(12))
    pages = [page] * 8
    latin = extract.assess_page_quality(pages)
    assert latin["low_quality"] is False
    yue = extract.assess_page_quality(pages, expect_native_script=True)
    assert yue["low_quality"] is True
    assert yue["reason"] == "missing_native_script"


def test_assess_leaves_native_script_book_alone():
    page = "\n".join(f"再見{i} zoi3 gin3 see you，到時見 dou3 si4 gin3 later {i}"
                     for i in range(10))
    pages = [page] * 8
    res = extract.assess_page_quality(pages, expect_native_script=True)
    assert res["low_quality"] is False
    assert res["reason"] == ""


# ── PDF page rendering ────────────────────────────────────────────────────────

def _blank_pdf(n_pages: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=300, height=400)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_render_pdf_pages_returns_jpeg_and_ignores_out_of_range():
    pytest.importorskip("pypdfium2")
    images = extract.render_pdf_pages(_blank_pdf(2), [1, 2, 99])
    assert set(images) == {1, 2}                     # page 99 ignored
    assert images[1][:3] == b"\xff\xd8\xff"          # JPEG magic
    # None means "all pages".
    assert set(extract.render_pdf_pages(_blank_pdf(3))) == {1, 2, 3}


def test_render_pdf_pages_accepts_a_path(tmp_path):
    """The reader renders straight off the stored file so a big PDF isn't copied
    into memory for every page request."""
    pytest.importorskip("pypdfium2")
    path = tmp_path / "book.pdf"
    path.write_bytes(_blank_pdf(3))
    images = extract.render_pdf_pages(str(path), [2])
    assert images[2][:3] == b"\xff\xd8\xff"
    assert extract.pdf_page_count(str(path)) == 3


# ── Vision transcription orchestration ────────────────────────────────────────

@pytest.mark.asyncio
async def test_transcribe_page_image_strips_fences_and_cleans(monkeypatch):
    async def fake_img(prompt, image_bytes, **kw):
        # Model wrapped output in a fence and emitted the layer-repeat artifact.
        return "```\n早晨早晨早晨早晨 zou2 san4\n```"

    monkeypatch.setattr(textbook.llm, "call_with_image", fake_img)
    text = await textbook.transcribe_page_image(
        b"img", "yue", api_key="k", model="reader")
    assert text == "早晨 zou2 san4"                   # fence stripped, artifact collapsed


@pytest.mark.asyncio
async def test_transcribe_pages_best_effort_and_counts_calls(monkeypatch):
    calls = []

    async def fake_img(prompt, image_bytes, **kw):
        calls.append(image_bytes)
        if image_bytes == b"boom":
            raise RuntimeError("vision failed")
        return f"native {image_bytes.decode()}"

    monkeypatch.setattr(textbook.llm, "call_with_image", fake_img)
    images = {1: b"p1", 2: b"boom", 3: b"p3"}
    out, n = await textbook.transcribe_pages(images, "yue", api_key="k", model="r")
    assert n == 3                                     # every page attempted
    assert out == {1: "native p1", 3: "native p3"}    # failed page omitted


@pytest.mark.asyncio
async def test_transcribe_pages_respects_max_vision_pages(monkeypatch):
    async def fake_img(prompt, image_bytes, **kw):
        return "x"

    monkeypatch.setattr(textbook.llm, "call_with_image", fake_img)
    images = {i: b"p" for i in range(1, textbook.MAX_VISION_PAGES + 5)}
    out, n = await textbook.transcribe_pages(images, "yue", api_key="k", model="r")
    assert n == textbook.MAX_VISION_PAGES


# ── DB: source PDF stored, pages overwritten ──────────────────────────────────

@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


@pytest.mark.asyncio
async def test_create_textbook_stores_pdf_id_and_update_pages_roundtrip(fresh_db):
    uid = fresh_db
    cid = await db.create_course(uid, "yue", "A1")
    tb_id = await db.create_textbook(
        uid, cid, "Phrasebook", "pb.pdf", ["garbled p1", "garbled p2"],
        [], pdf_media_id="f" * 32)
    book = await db.get_textbook(uid, tb_id)
    assert book["pdf_media_id"] == "f" * 32
    # Course-delete cleanup lists the PDF id alongside any visuals.
    assert "f" * 32 in await db.list_textbook_visual_ids(uid, cid)

    assert await db.update_textbook_pages(uid, tb_id, ["早晨", "你好", "多謝"]) is True
    refreshed = await db.get_textbook(uid, tb_id)
    assert refreshed["pages"] == ["早晨", "你好", "多謝"]
    assert refreshed["num_pages"] == 3                # kept in sync
    # Ownership enforced.
    assert await db.update_textbook_pages(uid + 1, tb_id, ["x"]) is False


# ── Route: /transcribe overwrites the selected range ──────────────────────────

@pytest.mark.asyncio
async def test_transcribe_route_overwrites_pages(fresh_db, monkeypatch, tmp_path):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    (tmp_path / "book.pdf").write_bytes(b"%PDF-fake")
    tb_id = await db.create_textbook(
        uid, cid, "Phrasebook", "pb.pdf",
        ["romanization only p1", "romanization only p2", "keep me p3"],
        [], pdf_media_id="book")

    monkeypatch.setattr(main.extract, "render_pdf_pages",
                        lambda pdf_bytes, pages: {p: b"img" for p in pages})

    async def fake_transcribe(images, lang, **kw):
        return {p: f"native page {p}" for p in images}, len(images)

    monkeypatch.setattr(main.textbook, "transcribe_pages", fake_transcribe)

    from types import SimpleNamespace

    async def fake_access(*a, **k):
        return SimpleNamespace(api_key="k", anthropic_key=None, model_reader="r")

    async def fake_meter(*a, **k):
        return False

    monkeypatch.setattr(main, "_resolve_gemini", fake_access)
    monkeypatch.setattr(main, "_textbook_metering", fake_meter)

    res = await main.transcribe_textbook_pages.__wrapped__(
        None, tb_id, {"start": 1, "end": 2}, user)
    assert res["updated_pages"] == [1, 2]
    saved = await db.get_textbook(uid, tb_id)
    assert saved["pages"] == ["native page 1", "native page 2", "keep me p3"]


@pytest.mark.asyncio
async def test_transcribe_route_409_without_stored_pdf(fresh_db, monkeypatch, tmp_path):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    tb_id = await db.create_textbook(uid, cid, "Old book", "old.pdf",
                                     ["p1", "p2"], [], pdf_media_id=None)
    with pytest.raises(main.HTTPException) as exc:
        await main.transcribe_textbook_pages.__wrapped__(
            None, tb_id, {"start": 1, "end": 1}, user)
    assert exc.value.status_code == 409


# ── Route: /page/{n}.jpg — caching + failure diagnosis ────────────────────────

@pytest.mark.asyncio
async def test_page_image_rerenders_a_truncated_cache_file(fresh_db, monkeypatch,
                                                           tmp_path):
    """A half-written cache file (e.g. the disk filled mid-write) must not be
    served forever as a broken image — it is treated as absent."""
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    (tmp_path / "book.pdf").write_bytes(b"%PDF-fake")
    tb_id = await db.create_textbook(uid, cid, "Book", "b.pdf", ["p1", "p2"],
                                     [], pdf_media_id="book")
    (tmp_path / f"tbpage_{tb_id}_1.jpg").write_bytes(b"\xff\xd8")   # truncated

    rendered = []

    def fake_render(source, pages, long_edge=None):
        rendered.append(pages)
        return {pages[0]: b"x" * 4096}

    monkeypatch.setattr(main.extract, "render_pdf_pages", fake_render)
    res = await main.textbook_page_image(tb_id, 1, user)
    assert rendered == [[1]]                       # re-rendered, not served stale
    assert (tmp_path / f"tbpage_{tb_id}_1.jpg").stat().st_size == 4096
    assert res.media_type == "image/jpeg"
    assert not list(tmp_path.glob("*.part"))       # atomic write leaves no scrap


@pytest.mark.asyncio
async def test_page_image_explains_pages_the_renderer_cannot_reach(
        fresh_db, monkeypatch, tmp_path):
    """Text extraction and the renderer can disagree about a damaged book's page
    count; the learner gets told that rather than a bare failure."""
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    (tmp_path / "book.pdf").write_bytes(b"%PDF-fake")
    tb_id = await db.create_textbook(uid, cid, "Book", "b.pdf",
                                     [f"p{i}" for i in range(1, 61)], [],
                                     pdf_media_id="book")
    monkeypatch.setattr(main.extract, "render_pdf_pages",
                        lambda *a, **k: {})
    monkeypatch.setattr(main.extract, "pdf_page_count", lambda source: 40)

    with pytest.raises(main.HTTPException) as exc:
        await main.textbook_page_image(tb_id, 45, user)
    assert exc.value.status_code == 404
    assert "only renders 40 of its 60 pages" in exc.value.detail


@pytest.mark.asyncio
async def test_reader_reports_a_short_renderable_page_count(fresh_db, monkeypatch,
                                                            tmp_path):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    (tmp_path / "book.pdf").write_bytes(b"%PDF-fake")
    tb_id = await db.create_textbook(uid, cid, "Book", "b.pdf",
                                     [f"p{i}" for i in range(1, 61)], [],
                                     pdf_media_id="book")
    monkeypatch.setattr(main.extract, "pdf_page_count", lambda source: 40)
    res = await main.textbook_reader(tb_id, user)
    assert (res["num_pages"], res["renderable_pages"]) == (60, 40)

    # A renderer that can't answer must not break opening the book.
    def boom(source):
        raise RuntimeError("no renderer")
    monkeypatch.setattr(main.extract, "pdf_page_count", boom)
    assert (await main.textbook_reader(tb_id, user))["renderable_pages"] == 60


# ── Mid-page split markers (locating the boundary for the reader) ─────────────

def _text_pdf(lines: list[tuple[str, float]]) -> bytes:
    """A minimal one-page PDF with each line drawn at a known baseline.

    Hand-built rather than via a PDF writer so the geometry these tests check
    (where a line sits on the page) is fixed by the fixture itself, and so they
    need no extra dependency to run.
    """
    content = ("BT /F1 12 Tf\n" + "".join(
        f"1 0 0 1 72 {y} Tm ({t}) Tj\n" for t, y in lines) + "ET\n").encode("latin-1")
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]"
        b"/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>",
        b"<</Length %d>>stream\n" % len(content) + content + b"endstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj" % i + body + b"endobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)


def _split_pdf() -> bytes:
    """Two blocks of text on one page with a unit heading between them."""
    lines = [(f"Unit 1 line {i}", 780 - i * 20) for i in range(10)]
    lines.append(("Unit 2: Ordering food", 566))          # after a wider gap
    lines += [(f"Unit 2 line {i}", 542 - i * 20) for i in range(6)]
    return _text_pdf(lines)


def test_locate_page_lines_finds_the_anchor_between_its_neighbours():
    pdf = _split_pdf()
    found = extract.locate_page_lines(pdf, 1, ["Unit 2: Ordering food"])
    y = found["Unit 2: Ordering food"]
    # Ten lines of unit 1 above, six of unit 2 below → roughly a third down, and
    # strictly between the last unit-1 line and the heading itself.
    above = extract.locate_page_lines(pdf, 1, ["Unit 1 line 9"])["Unit 1 line 9"]
    below = extract.locate_page_lines(pdf, 1, ["Unit 2 line 0"])["Unit 2 line 0"]
    assert above < y < below
    assert 0.2 < y < 0.5


def test_locate_page_lines_is_whitespace_tolerant_and_omits_misses():
    pdf = _split_pdf()
    found = extract.locate_page_lines(
        pdf, 1, ["  unit 2:   Ordering   food ", "not on this page", ""])
    assert list(found) == ["  unit 2:   Ordering   food "]
    # A page that doesn't exist degrades to "no marker", never an exception.
    assert extract.locate_page_lines(pdf, 9, ["Unit 2: Ordering food"]) == {}


def test_split_marks_dedupes_the_two_sides_of_one_boundary():
    chapters = [
        {"title": "Unit 1", "start": 1, "end": 3, "start_anchor": "",
         "end_anchor": "Unit 2: Ordering food"},
        {"title": "Unit 2", "start": 3, "end": 6,
         "start_anchor": "Unit 2: Ordering food", "end_anchor": ""},
    ]
    marks = main._split_marks(chapters)
    assert len(marks) == 1                      # one boundary, not two
    assert marks[0]["page"] == 3
    assert (marks[0]["above"], marks[0]["below"]) == ("Unit 1", "Unit 2")
    assert marks[0]["y"] is None                # filled in by the route

    # An unanchored book has no markers at all.
    assert main._split_marks([{"title": "A", "start": 1, "end": 3},
                              {"title": "B", "start": 4, "end": 6}]) == []


@pytest.mark.asyncio
async def test_splits_route_locates_the_boundary(fresh_db, monkeypatch, tmp_path):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    (tmp_path / "book.pdf").write_bytes(_split_pdf())
    tb_id = await db.create_textbook(uid, cid, "Book", "b.pdf",
                                     ["p1", "p2", "p3"], [], pdf_media_id="book")
    await db.update_textbook_chapters(tb_id, [
        {"title": "Unit 1", "start": 1, "end": 1,
         "end_anchor": "Unit 2: Ordering food", "start_anchor": "",
         "skip_hint": False, "status": ""},
        {"title": "Unit 2", "start": 1, "end": 3,
         "start_anchor": "Unit 2: Ordering food", "end_anchor": "",
         "skip_hint": False, "status": ""}])

    res = await main.textbook_splits(tb_id, user)
    assert len(res["splits"]) == 1
    mark = res["splits"][0]
    assert mark["line"] == "Unit 2: Ordering food"
    assert 0.2 < mark["y"] < 0.5                # located on the rendered page

    # No stored PDF (or an unlocatable anchor) → the marker survives with y=None
    # so the reader still flags it in the extracted text.
    monkeypatch.setattr(main.extract, "locate_page_lines", lambda *a, **k: {})
    assert (await main.textbook_splits(tb_id, user))["splits"][0]["y"] is None
