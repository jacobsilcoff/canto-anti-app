"""Core-text extraction for the reader's URL / PDF import.

Two deterministic (no-LLM) extractors that pull the *pertinent* body text out of
a source so the reader can render it like any other story:

- `fetch_and_extract_url(url)` — fetch a web page (SSRF-guarded) and pull the
  main article text out of the HTML boilerplate via trafilatura.
- `extract_pdf(pdf_bytes)`   — pull selectable text out of a PDF via pypdf.

Both return `{"title": str, "text": str}`. They raise `ExtractError` (mapped to
a 4xx by the caller) on anything the user can fix — a bad URL, a blocked host, a
scanned PDF with no selectable text, an empty article, etc.

The heavy parsers are imported lazily so a missing optional dependency degrades
to a clean error message instead of crashing app import.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

# Bound how much we keep — articles can be huge, and the per-sentence reader
# pipeline (translate + TTS) is the real cost. ~12k chars is a long read already.
MAX_TEXT_CHARS = 12_000
MAX_FETCH_BYTES = 8 * 1024 * 1024  # 8 MB of HTML is plenty; refuse larger.
FETCH_TIMEOUT = 15.0

# A real browser UA — many news sites 403 the default httpx agent.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class ExtractError(Exception):
    """User-fixable extraction failure (bad URL, blocked host, empty body…)."""


# ── URL fetching (SSRF-guarded) ────────────────────────────────────────────────

def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


def _validate_public_host(host: str) -> None:
    """Resolve `host` and reject if any address is private/loopback/etc.

    A best-effort SSRF guard: we fetch user-supplied URLs server-side, so block
    obvious attempts to reach the internal network / cloud metadata endpoints.
    """
    if not host:
        raise ExtractError("Invalid URL — no host.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ExtractError(f"Couldn't resolve host: {host}")
    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            raise ExtractError("That URL points to a private/blocked address.")


async def fetch_url(url: str) -> str:
    """Fetch a public web page and return its HTML (SSRF-guarded, size-capped)."""
    url = (url or "").strip()
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            current_url = url
            for _ in range(6):
                parsed = urlparse(current_url)
                if parsed.scheme not in ("http", "https"):
                    raise ExtractError("Enter a full http(s):// URL.")
                _validate_public_host(parsed.hostname or "")

                async with client.stream("GET", current_url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise ExtractError("The page returned an invalid redirect.")
                        # Validate the target before issuing the next request. Letting
                        # httpx auto-follow would contact an internal redirect target
                        # before we had a chance to apply the SSRF guard.
                        current_url = urljoin(str(resp.url), location)
                        continue

                    if resp.status_code >= 400:
                        raise ExtractError(
                            f"The page returned HTTP {resp.status_code}."
                        )
                    ctype = resp.headers.get("content-type", "")
                    if (ctype and "html" not in ctype and "xml" not in ctype
                            and "text" not in ctype):
                        raise ExtractError(
                            f"That link isn't a web page (content-type: {ctype})."
                        )

                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_FETCH_BYTES:
                            raise ExtractError("That page is too large to import.")
                    encoding = resp.charset_encoding or "utf-8"
                    return bytes(body).decode(encoding, errors="replace")
            raise ExtractError("The page redirected too many times.")
    except httpx.HTTPError as exc:
        raise ExtractError(f"Couldn't fetch the page: {exc}")


def extract_article_html(html: str, url: str | None = None) -> dict:
    """Pull the main article title + body text out of raw HTML.

    Uses trafilatura (deterministic, no LLM). Returns {"title", "text"}.
    """
    try:
        import trafilatura
        from trafilatura.metadata import extract_metadata
    except ImportError:
        raise ExtractError("Article extraction is unavailable on this server.")

    text = trafilatura.extract(
        html, url=url, favor_recall=True,
        include_comments=False, include_tables=False,
        include_formatting=True,
    ) or ""
    text = clean_text(text)
    if len(text) < 80:
        raise ExtractError(
            "Couldn't find readable article text on that page "
            "(it may be paywalled or rendered by JavaScript)."
        )
    title = ""
    try:
        meta = extract_metadata(html)
        if meta and meta.title:
            title = meta.title.strip()
    except Exception:
        pass
    return {"title": title, "text": text[:MAX_TEXT_CHARS]}


async def fetch_and_extract_url(url: str) -> dict:
    """Fetch + extract in one call. Returns {"title", "text"}."""
    html = await fetch_url(url)
    return extract_article_html(html, url=url)


# ── PDF extraction ─────────────────────────────────────────────────────────────

def _matrix_mult(m: list[float], n: list[float]) -> list[float]:
    """Multiply two compressed PDF transformation matrices."""
    return [
        m[0] * n[0] + m[1] * n[2],
        m[0] * n[1] + m[1] * n[3],
        m[2] * n[0] + m[3] * n[2],
        m[2] * n[1] + m[3] * n[3],
        m[4] * n[0] + m[5] * n[2] + n[4],
        m[4] * n[1] + m[5] * n[3] + n[5],
    ]


def _extract_visible_page_text(page) -> str:
    """Extract only text positioned inside a page's visible crop box.

    Split-spread PDFs often create two logical pages from one shared content
    stream by assigning each copy a different CropBox. ``pypdf.extract_text``
    reads the entire stream and ignores that box, making both logical pages
    contain both halves. Detect meaningful out-of-box text and, only then,
    rebuild the visible lines from pypdf's positioned text callbacks. Ordinary
    PDFs retain pypdf's normal extraction unchanged.
    """
    positioned: list[tuple[str, float, float]] = []
    outside_chars = 0
    try:
        box = page.cropbox
        left, right = float(box.left), float(box.right)
        bottom, top = float(box.bottom), float(box.top)

        def visit(text, cm, tm, _font, _size):
            nonlocal outside_chars
            if not text or not text.strip():
                return
            matrix = _matrix_mult([float(v) for v in tm],
                                  [float(v) for v in cm])
            x, y = matrix[4], matrix[5]
            if left - 2 <= x <= right + 2 and bottom - 2 <= y <= top + 2:
                positioned.append((text.replace("\r", ""), x, y))
            else:
                outside_chars += len(text.strip())

        raw = page.extract_text(visitor_text=visit) or ""
    except Exception:
        return page.extract_text() or ""

    visible_chars = sum(len(text.strip()) for text, _, _ in positioned)
    # A few out-of-bounds page numbers or printer marks are not enough to
    # replace pypdf's usually-superior default line assembly.
    if outside_chars < 40 or outside_chars < visible_chars * 0.05:
        return raw
    if not positioned:
        return ""

    rebuilt: list[str] = []
    previous_y: float | None = None
    previous_x: float | None = None
    for text, x, y in positioned:
        if previous_y is not None and abs(y - previous_y) > 2:
            if rebuilt and not rebuilt[-1].endswith("\n"):
                rebuilt.append("\n")
        elif (rebuilt and previous_x is not None and x > previous_x and
              rebuilt[-1] and not rebuilt[-1][-1].isspace() and
              text and not text[0].isspace()):
            rebuilt.append(" ")
        rebuilt.append(text)
        previous_y, previous_x = y, x
    return "".join(rebuilt)


def extract_pdf(pdf_bytes: bytes, max_chars: int = MAX_TEXT_CHARS) -> dict:
    """Pull selectable text out of a PDF. Returns {"title", "text"}.

    Scanned/image-only PDFs have no text layer — we raise a clear error rather
    than returning gibberish (OCR is out of scope). `max_chars` bounds the kept
    text: the reader default is small (per-sentence translate/TTS is costly),
    while the textbook import passes a much larger cap.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ExtractError("PDF import is unavailable on this server.")

    import io
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise ExtractError(f"Couldn't read that PDF: {exc}")

    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(_extract_visible_page_text(page))
        except Exception:
            continue
    text = clean_text("\n".join(parts))
    if len(text) < 80:
        raise ExtractError(
            "No selectable text found in that PDF "
            "(it may be a scanned image — OCR isn't supported yet)."
        )
    title = ""
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip()
    except Exception:
        pass
    return {"title": title, "text": text[:max_chars]}


MAX_PDF_PAGES = 600
MAX_PDF_VISUALS = 80
MAX_VISUALS_PER_PAGE = 4
MIN_VISUAL_WIDTH = 180
MIN_VISUAL_HEIGHT = 120
MIN_VISUAL_AREA = 40_000
MAX_VISUAL_EDGE = 1400


def _extract_page_visuals(page, page_num: int) -> list[dict]:
    """Extract useful embedded raster images from one PDF page.

    Tiny icons, tracking pixels, decorative rules, and extreme-aspect assets are
    ignored. Images are normalized to bounded JPEGs so textbook storage and the
    source-review UI remain predictable. Vector-only diagrams are not exposed by
    pypdf's image API and therefore are not captured here.
    """
    try:
        from PIL import Image
    except ImportError:
        return []

    import io
    out: list[dict] = []
    try:
        image_files = page.images
    except Exception:
        return out
    for image_file in image_files[:12]:
        try:
            im = image_file.image
            if im is None:
                im = Image.open(io.BytesIO(image_file.data))
            im.load()
            width, height = im.size
            if (width < MIN_VISUAL_WIDTH or height < MIN_VISUAL_HEIGHT or
                    width * height < MIN_VISUAL_AREA):
                continue
            ratio = max(width / max(1, height), height / max(1, width))
            if ratio > 8:
                continue
            if im.mode in ("RGBA", "LA") or "transparency" in im.info:
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, "white")
                bg.paste(rgba, mask=rgba.getchannel("A"))
                im = bg
            else:
                im = im.convert("RGB")
            im.thumbnail((MAX_VISUAL_EDGE, MAX_VISUAL_EDGE), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=84, optimize=True)
            data = buf.getvalue()
            out.append({
                "page": page_num, "width": im.width, "height": im.height,
                "data": data,
            })
            if len(out) >= MAX_VISUALS_PER_PAGE:
                break
        except Exception:
            continue
    return out


def extract_pdf_pages(pdf_bytes: bytes, max_pages: int = MAX_PDF_PAGES,
                      include_images: bool = False) -> dict:
    """Pull selectable text out of a PDF, KEEPING page boundaries.

    Returns {"title", "pages": [str, ...]} — one entry per page (empty pages
    stay as "" so page numbers line up with the source PDF). With
    ``include_images=True``, also returns deduplicated, page-linked ``visuals``
    as bounded JPEG bytes. The textbook import uses both in its source review.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ExtractError("PDF import is unavailable on this server.")

    import io
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise ExtractError(f"Couldn't read that PDF: {exc}")

    if len(reader.pages) > max_pages:
        raise ExtractError(f"That PDF has too many pages (max {max_pages}).")

    pages: list[str] = []
    visuals: list[dict] = []
    visual_by_hash: dict[str, dict] = {}
    for page_num, page in enumerate(reader.pages, 1):
        try:
            pages.append(clean_text(_extract_visible_page_text(page)))
        except Exception:
            pages.append("")
        if include_images and len(visuals) < MAX_PDF_VISUALS:
            import hashlib
            for visual in _extract_page_visuals(page, page_num):
                digest = hashlib.sha256(visual["data"]).hexdigest()
                existing = visual_by_hash.get(digest)
                if existing:
                    if page_num not in existing["pages"]:
                        existing["pages"].append(page_num)
                    continue
                visual["pages"] = [visual.pop("page")]
                visual["sha256"] = digest
                visuals.append(visual)
                visual_by_hash[digest] = visual
                if len(visuals) >= MAX_PDF_VISUALS:
                    break
    if sum(len(p) for p in pages) < 80:
        raise ExtractError(
            "No selectable text found in that PDF "
            "(it may be a scanned image — OCR isn't supported yet)."
        )
    title = ""
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip()
    except Exception:
        pass
    result = {"title": title, "pages": pages}
    if include_images:
        result["visuals"] = visuals
    return result


# ── Page rendering (for vision re-extraction) ──────────────────────────────────

# Render target: long edge ~2000 px is plenty for the vision model to read dense
# interlinear text while keeping the JPEG small enough to send per page.
RENDER_LONG_EDGE = 2000
RENDER_MAX_SCALE = 4.0
RENDER_JPEG_QUALITY = 85


def _open_for_render(source):
    """Open a PDF for rasterizing. ``source`` is raw bytes OR a filesystem path.

    Prefer the path form: pdfium reads the file itself, so a 25 MB textbook
    doesn't have to sit in Python memory for the lifetime of every single page
    request (several page turns at once used to mean several full copies).
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise ExtractError(
            "PDF page rendering is unavailable on this server "
            "(the PDF renderer is not installed)."
        )
    try:
        from PIL import Image  # noqa: F401  (pdfium.to_pil needs Pillow)
    except ImportError:
        raise ExtractError("PDF page rendering is unavailable on this server.")
    try:
        return pdfium.PdfDocument(source)
    except Exception as exc:
        raise ExtractError(f"Couldn't open that PDF for rendering: {exc}")


def pdf_page_count(source) -> int:
    """How many pages the RENDERER sees in a PDF (bytes or path).

    Text extraction (pypdf) and rendering (pdfium) walk the page tree
    independently, and a damaged or unusual book can leave them disagreeing —
    which shows up as "every page past N fails to render" while the extracted
    text for those pages is right there. Callers use this to say so plainly.
    """
    doc = _open_for_render(source)
    try:
        return len(doc)
    finally:
        doc.close()


def render_pdf_pages(source, page_numbers: list[int] | None = None,
                     long_edge: int = RENDER_LONG_EDGE) -> dict[int, bytes]:
    """Rasterize selected PDF pages to JPEG bytes, keyed by 1-based page number.

    ``source`` is raw PDF bytes or a path to one. Used by the textbook reader
    (screen-size renders, cached on disk) and by the vision re-extraction path:
    deterministic ``pypdf`` text loses the structure of 2-up / interlinear /
    romanized books (and returns nothing for scanned pages), so we render those
    pages and let the model transcribe them.

    ``page_numbers`` is 1-based (out-of-range entries are ignored); ``None`` means
    every page. Raises ``ExtractError`` if the renderer is unavailable or the PDF
    can't be opened.
    """
    import io
    doc = _open_for_render(source)

    out: dict[int, bytes] = {}
    try:
        total = len(doc)
        wanted = (sorted({int(p) for p in page_numbers})
                  if page_numbers is not None else range(1, total + 1))
        for pno in wanted:
            if pno < 1 or pno > total:
                continue
            page = doc[pno - 1]
            try:
                width, height = page.get_size()
                scale = min(RENDER_MAX_SCALE,
                            max(1.0, long_edge / max(1.0, max(width, height))))
                pil = page.render(scale=scale).to_pil().convert("RGB")
                buf = io.BytesIO()
                pil.save(buf, format="JPEG", quality=RENDER_JPEG_QUALITY,
                         optimize=True)
                out[pno] = buf.getvalue()
            except Exception:
                # Per-page best-effort: one damaged page in a batch must not
                # cost the caller every other page it asked for. The page is
                # simply absent from the result.
                continue
            finally:
                page.close()
    finally:
        doc.close()
    return out


# ── Extraction quality heuristic (when to suggest AI re-reading) ────────────────

def page_text_quality(text: str) -> float:
    """Rough 0..1 confidence that a page's *extracted* text is clean and usable.

    Low scores flag the failure modes deterministic PDF text hits on the books
    this feature targets: a near-empty page (scanned / image-only), text whose
    word boundaries collapsed (2-up columns merged into one run), or a page that
    is mostly punctuation/symbol noise. It is intentionally conservative — a plain
    short vocab list should still score well — because it only *suggests* AI
    re-reading, which the user can always invoke regardless.
    """
    s = (text or "").strip()
    if not s:
        return 1.0  # a genuinely blank page (spacer) isn't a failure
    letters = sum(c.isalpha() for c in s)
    if letters < 4:
        # Almost no letters but not blank → stray marks on an image/scanned page
        # the text layer couldn't read. (A short-but-clean vocab line has more.)
        return 0.2
    non_space = [c for c in s if not c.isspace()]
    alpha_ratio = letters / max(1, len(non_space))
    # Word-boundary health: CJK/Thai run together legitimately, so only penalise
    # when the text is largely Latin yet has almost no spaces (merged columns).
    latin = sum(1 for c in s if ("a" <= c.lower() <= "z"))
    spaces = s.count(" ")
    score = 1.0
    if alpha_ratio < 0.45:
        score -= 0.4  # dominated by symbols/numbers → noisy extraction
    if latin > 200 and spaces / max(1, len(s)) < 0.06:
        score -= 0.4  # long Latin text with no spaces → boundaries lost
    return max(0.0, min(1.0, score))


def native_script_ratio(text: str) -> float:
    """Fraction of non-space chars that are non-Latin *native script*.

    Romanization (basic Latin + combining tone diacritics, U+0300–036F, and the
    Latin Extended blocks) counts as 0; Greek/Cyrillic/Arabic/Indic/Thai/Hangul/
    kana/CJK (U+0370 and up) count as native. Used to catch a non-Latin-language
    book whose text layer carries only romanization — vision can reconstruct the
    native script the planner needs.
    """
    non_space = [c for c in (text or "") if not c.isspace()]
    if not non_space:
        return 0.0
    native = sum(1 for c in non_space if ord(c) >= 0x0370)
    return native / len(non_space)


def assess_page_quality(pages: list[str], min_content_pages: int = 4, *,
                        expect_native_script: bool = False) -> dict:
    """Aggregate ``page_text_quality`` across a book.

    Returns ``{"low_quality": bool, "poor_pages": [1-based, ...], "score": float,
    "reason": str}``. ``low_quality`` is a *hint* for the UI to surface AI
    re-reading; it never blocks the deterministic flow. Flagged when: pages are
    garbled/merged, the book extracts to almost nothing (scanned), OR — for a
    non-Latin target language — the extracted text is almost all romanization
    with no native script (``expect_native_script``).
    """
    scored = [(i + 1, page_text_quality(p)) for i, p in enumerate(pages)]
    content = [(n, sc) for (n, sc), p in zip(scored, pages) if (p or "").strip()]
    poor = [n for n, sc in scored if sc < 0.6]
    total_chars = sum(len((p or "").strip()) for p in pages)
    n_content = max(1, len(content))
    avg = sum(sc for _, sc in content) / n_content if content else 1.0
    sparse = len(content) >= min_content_pages and total_chars < 120 * len(content)
    no_native = False
    if expect_native_script and total_chars >= 400:
        no_native = native_script_ratio("\n".join(pages)) < 0.03
    low = (len(poor) >= max(2, 0.25 * n_content)) or sparse or (avg < 0.7) or no_native
    reason = ("missing_native_script" if no_native else
              "sparse_text" if sparse else
              "garbled_text" if low else "")
    return {"low_quality": bool(low),
            "poor_pages": poor[:200],
            "score": round(avg, 3),
            "reason": reason}


# ── Shared cleanup ─────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    """Normalize extracted text: fix hyphenated line breaks, collapse blank runs,
    strip markdown heading markers from trafilatura's ``include_formatting`` output.
    """
    if not text:
        return ""
    import re
    # Join words split across a line break with a hyphen: "inter-\nnational".
    text = text.replace("-\n", "")
    # Keep markdown heading markers from trafilatura (e.g. "## History") — the
    # reader frontend uses them to style headings.  Only strip [edit] wiki links.
    text = re.sub(r'\[edit\]', '', text)
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(" ".join(ln.split()))
    return "\n".join(out).strip()
