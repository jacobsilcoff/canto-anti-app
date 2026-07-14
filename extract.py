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
from urllib.parse import urlparse

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
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ExtractError("Enter a full http(s):// URL.")
    _validate_public_host(parsed.hostname or "")

    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise ExtractError(f"Couldn't fetch the page: {exc}")

    # A redirect could have bounced to an internal host — re-check the final one.
    final_host = resp.url.host or ""
    if final_host and final_host != (parsed.hostname or ""):
        _validate_public_host(final_host)

    if resp.status_code >= 400:
        raise ExtractError(f"The page returned HTTP {resp.status_code}.")
    if len(resp.content) > MAX_FETCH_BYTES:
        raise ExtractError("That page is too large to import.")
    ctype = resp.headers.get("content-type", "")
    if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
        raise ExtractError(f"That link isn't a web page (content-type: {ctype}).")
    return resp.text


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
