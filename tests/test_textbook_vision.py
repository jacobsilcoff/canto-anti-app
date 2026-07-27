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

    # No rasterizer on the server at all says nothing about the book: stay
    # optimistic and let individual page requests fail, rather than declaring a
    # perfectly good book text-only (and "repairing" it).
    def unavailable(source):
        raise extract.RendererUnavailable("not installed")
    monkeypatch.setattr(main.extract, "pdf_page_count", unavailable)
    assert (await main.textbook_reader(tb_id, user))["renderable_pages"] == 60

    # Any other failure IS this book failing to open — repair it, and report 0
    # when it can't be salvaged so the reader shows text instead of broken
    # images. (The fixture is a stub, so there's nothing to salvage.)
    def boom(source):
        raise RuntimeError("Data format error")
    monkeypatch.setattr(main.extract, "pdf_page_count", boom)
    main._unrepairable_pdfs.discard(str(tmp_path / "book.pdf"))
    assert (await main.textbook_reader(tb_id, user))["renderable_pages"] == 0


# ── Mid-page split markers (locating the boundary for the reader) ─────────────

def _text_pdf(lines: list, positioned: bool = False) -> bytes:
    """A minimal one-page PDF with each line drawn at a known baseline.

    Hand-built rather than via a PDF writer so the geometry these tests check
    (where a line sits on the page) is fixed by the fixture itself, and so they
    need no extra dependency to run.
    """
    runs = lines if positioned else [(t, y, 72) for t, y in lines]
    content = ("BT /F1 12 Tf\n" + "".join(
        f"1 0 0 1 {x} {y} Tm ({t}) Tj\n" for t, y, x in runs) + "ET\n").encode("latin-1")
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


def _words_pdf() -> bytes:
    """A page whose words are each positioned separately — extremely common in
    real textbook PDFs, and the case where no space glyphs exist to extract."""
    lines = []
    y = 700
    for i in range(6):
        x = 72
        for word in ("Unit", "1", "line", str(i)):
            lines.append((word, y, x))
            x += 34
        y -= 20
    x = 72
    for word in ("Unit", "2:", "Ordering", "food"):
        lines.append((word, y - 14, x))
        x += 42
    return _text_pdf(lines, positioned=True)


def test_locate_page_lines_matches_word_positioned_text():
    """pypdf rebuilds such a page as e.g. "Unit 2: Orderingfood"; the stored
    anchor has the space. Whitespace can't be trusted between the two."""
    found = extract.locate_page_lines(_words_pdf(), 1, ["Unit 2: Ordering food"])
    assert 0.2 < found["Unit 2: Ordering food"] < 0.5


def test_locate_page_lines_matches_through_the_page_cleaner():
    """The stored text (and so the anchor) has the repeated-text-layer artifact
    collapsed; the raw page still carries it. The caller passes the same cleaner
    so the two representations can meet."""
    raw = "Unit 2: OrderingUnit 2: OrderingUnit 2: OrderingUnit 2: Ordering"
    stored_anchor = textbook.clean_page_text(raw)
    assert stored_anchor == "Unit 2: Ordering"          # what got stored
    pdf = _text_pdf([(f"Unit 1 line {i}", 700 - i * 20) for i in range(5)]
                    + [(raw, 586)])
    found = extract.locate_page_lines(pdf, 1, [stored_anchor],
                                      textbook.clean_page_text)
    assert 0.1 < found[stored_anchor] < 0.5


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


# ── Repairing a PDF the rasterizer refuses to open ─────────────────────────────
#
# pypdf finds the `%PDF-` header at any offset and rebuilds a broken xref by
# scanning for objects; pdfium checks only the start of the file and gives up
# with "Data format error". So a book can extract every page of its text — proof
# we parsed it — and still render nothing at all.

def _junk_header_pdf() -> bytes:
    """A valid PDF with 1.4 KB of junk in front of its header."""
    return b"\r\n<!-- generated -->\r\n" * 64 + _split_pdf()


def test_junk_before_the_header_extracts_but_will_not_rasterize():
    pdf = _junk_header_pdf()
    assert pdf.find(b"%PDF-") > 1024              # past pdfium's search window
    # Text extraction is unaffected — this is exactly the confusing case.
    assert "Unit 2: Ordering food" in extract.extract_pdf_pages(pdf)["pages"][0]
    assert extract.can_render(pdf) is False


def test_repair_trims_junk_before_the_header():
    repaired = extract.repair_pdf(_junk_header_pdf())
    assert repaired is not None
    assert repaired.startswith(b"%PDF-")
    assert extract.can_render(repaired) is True
    # Byte-preserving: the original document is kept intact, only re-based.
    assert repaired == _split_pdf()


def test_repair_rebuilds_a_pdf_with_no_usable_xref():
    """Nothing to trim, so the repair falls through to re-serializing."""
    good = _split_pdf()
    broken = good.replace(b"xref\n0 6", b"xref\n0 9", 1)
    broken = broken[:broken.rfind(b"startxref")] + b"startxref\n17\n%%EOF\n"
    repaired = extract.repair_pdf(broken)
    if repaired is None:
        pytest.skip("pdfium recovered this xref on its own")
    assert extract.can_render(repaired) is True
    assert repaired.startswith(b"%PDF-")


def test_repair_reads_from_a_path_and_gives_up_on_a_non_pdf(tmp_path):
    path = tmp_path / "book.pdf"
    path.write_bytes(_junk_header_pdf())
    assert extract.can_render(extract.repair_pdf(str(path))) is True

    (tmp_path / "nope.pdf").write_bytes(b"this is not a PDF at all")
    assert extract.repair_pdf(str(tmp_path / "nope.pdf")) is None
    assert extract.repair_pdf(str(tmp_path / "missing.pdf")) is None


@pytest.mark.asyncio
async def test_opening_a_book_repairs_its_unrenderable_pdf(fresh_db, monkeypatch,
                                                           tmp_path):
    """A book uploaded before the check existed heals on its next open, instead
    of asking the learner to find a repaired copy."""
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    pdf_path = tmp_path / "book.pdf"
    pdf_path.write_bytes(_junk_header_pdf())
    tb_id = await db.create_textbook(uid, cid, "Book", "b.pdf", ["p1"], [],
                                     pdf_media_id="book")
    main._unrepairable_pdfs.discard(str(pdf_path))

    res = await main.textbook_reader(tb_id, user)
    assert res["renderable_pages"] == 1            # not 0, and not an error
    assert extract.can_render(str(pdf_path)) is True   # fixed on disk, in place

    # And the page it now renders really is the book's page.
    await main.textbook_page_image(tb_id, 1, user)
    assert (tmp_path / f"tbpage_{tb_id}_1.jpg").exists()


@pytest.mark.asyncio
async def test_an_unrepairable_book_reads_as_text_instead_of_erroring(
        fresh_db, monkeypatch, tmp_path):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    pdf_path = tmp_path / "book.pdf"
    pdf_path.write_bytes(b"not a PDF, and not fixable")
    tb_id = await db.create_textbook(uid, cid, "Book", "b.pdf", ["p1", "p2"], [],
                                     pdf_media_id="book")
    main._unrepairable_pdfs.discard(str(pdf_path))

    # 0 tells the reader to show extracted text for every page rather than
    # requesting images that can only fail.
    assert (await main.textbook_reader(tb_id, user))["renderable_pages"] == 0

    with pytest.raises(main.HTTPException) as err:
        await main.textbook_page_image(tb_id, 1, user)
    assert err.value.status_code == 409
    assert "reads as extracted text" in err.value.detail

    # The failed repair is remembered, so page requests stop re-attempting it.
    assert str(pdf_path) in main._unrepairable_pdfs


@pytest.mark.asyncio
async def test_upload_repairs_the_pdf_it_stores(fresh_db, monkeypatch, tmp_path):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    upload = main.UploadFile(filename="book.pdf",
                             file=io.BytesIO(_junk_header_pdf()))

    res = await main.upload_textbook.__wrapped__(None, cid, upload, "Book", user)
    assert res["can_transcribe"] is True
    book = await db.get_textbook(uid, res["id"])
    stored = tmp_path / f"{book['pdf_media_id']}.pdf"
    assert extract.can_render(str(stored)) is True    # stored already repaired


# ── Placing a split by dragging over the rendered page ────────────────────────
#
# A split is stored as a verbatim LINE (that's what the generator trims on), so a
# drag has to snap to a real line: the client needs every line's position.

def test_page_line_positions_are_ordered_and_match_the_rule_placement():
    pdf = _split_pdf()
    lines = extract.page_line_positions(pdf, 1)
    texts = [l["text"] for l in lines]
    assert "Unit 2: Ordering food" in texts
    ys = [l["y"] for l in lines]
    assert ys == sorted(ys)                       # top-down
    assert all(0.0 <= y <= 1.0 for y in ys)

    # The height offered for dragging is exactly where the rule for that anchor
    # is drawn — so what you drop is where the line lands.
    located = extract.locate_page_lines(pdf, 1, ["Unit 2: Ordering food"])
    dragged = next(l["y"] for l in lines if l["text"] == "Unit 2: Ordering food")
    assert dragged == pytest.approx(located["Unit 2: Ordering food"])


def test_split_rule_clears_the_glyphs_of_the_line_it_marks():
    """The midpoint between two baselines lands at the cap height of the line
    below, so a rule drawn there clips the tops of its letters — it has to sit
    well up into the whitespace instead."""
    spacing, first = 20.0, 700.0
    pdf = _text_pdf([(f"line {i}", first - i * spacing) for i in range(8)])
    lines = extract.page_line_positions(pdf, 1)
    page_h = 842.0                                  # _text_pdf's MediaBox

    for i in (3, 5):
        rule_y = page_h - lines[i]["y"] * page_h    # back to PDF user space
        baseline = first - i * spacing
        above = baseline + spacing
        assert baseline < rule_y < above            # inside the gap
        # Above a 13pt font's cap height (~9.4pt) with room to spare.
        assert rule_y - baseline > 11.0
        # And still below the previous line's descenders.
        assert above - rule_y > 5.0


def test_page_line_positions_apply_the_page_cleaner():
    """The anchor a drag produces must match the CLEANED text the trimmer reads."""
    raw = "Unit 2: Ordering" * 4         # the repeated-text-layer artifact
    pdf = _text_pdf([(f"line {i}", 700 - i * 20) for i in range(4)] + [(raw, 600)])
    lines = extract.page_line_positions(pdf, 1, textbook.clean_page_text)
    assert "Unit 2: Ordering" in [l["text"] for l in lines]


def test_page_line_positions_empty_for_a_page_with_no_text_layer():
    assert extract.page_line_positions(_text_pdf([]), 1) == []


@pytest.mark.asyncio
async def test_page_lines_route(fresh_db, monkeypatch, tmp_path):
    uid = fresh_db
    user = await db.get_user(uid)
    cid = await db.create_course(uid, "yue", "A1")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path)
    (tmp_path / "book.pdf").write_bytes(_split_pdf())
    tb_id = await db.create_textbook(uid, cid, "Book", "b.pdf", ["p1", "p2"], [],
                                     pdf_media_id="book")

    res = await main.textbook_page_lines(tb_id, 1, user)
    assert res["reason"] == ""
    assert "Unit 2: Ordering food" in [l["text"] for l in res["lines"]]

    with pytest.raises(main.HTTPException) as err:
        await main.textbook_page_lines(tb_id, 9, user)
    assert err.value.status_code == 404

    # A book with no stored PDF says so, rather than looking like an empty page.
    tb2 = await db.create_textbook(uid, cid, "No PDF", "x.pdf", ["p1"], [])
    assert (await main.textbook_page_lines(tb2, 1, user)) == {"lines": [],
                                                             "reason": "no_pdf"}
