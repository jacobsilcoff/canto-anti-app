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

def extract_pdf(pdf_bytes: bytes) -> dict:
    """Pull selectable text out of a PDF. Returns {"title", "text"}.

    Scanned/image-only PDFs have no text layer — we raise a clear error rather
    than returning gibberish (OCR is out of scope).
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
            parts.append(page.extract_text() or "")
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
    return {"title": title, "text": text[:MAX_TEXT_CHARS]}


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
    # Strip markdown heading markers (trafilatura include_formatting=True adds
    # "# ", "## ", etc.).  Keep the heading text on its own line.
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Strip wiki [edit] links that trafilatura leaves in.
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
