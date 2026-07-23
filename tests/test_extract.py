"""Tests for the reader URL/PDF import extractor.

These cover the deterministic, no-network pieces: text cleanup and the SSRF
host guard. The heavy parsers (trafilatura/pypdf) and live fetching are not
exercised here (network + optional deps).
"""

import pytest
import httpx
from PIL import Image
from types import SimpleNamespace

import extract


class _Box:
    left = 100
    right = 200
    bottom = 0
    top = 300


class _CroppedPage:
    cropbox = _Box()

    def extract_text(self, visitor_text=None):
        fragments = [
            ("hidden left and deliberately long enough", 20, 250),
            ("Visible", 120, 250),
            (" heading", 155, 250),
            ("Second line", 120, 230),
            ("hidden right and deliberately long enough", 220, 220),
        ]
        if visitor_text:
            for text, x, y in fragments:
                visitor_text(text, [1, 0, 0, 1, 0, 0],
                             [1, 0, 0, 1, x, y], None, 12)
        return "hidden left Visible heading Second line hidden right"


def test_visible_page_extraction_respects_cropbox_and_rebuilds_lines():
    assert extract._extract_visible_page_text(_CroppedPage()) == (
        "Visible heading\nSecond line"
    )


def test_visible_page_extraction_keeps_normal_pypdf_output_without_leakage():
    page = _CroppedPage()
    page.extract_text = lambda visitor_text=None: "pypdf assembled text"
    assert extract._extract_visible_page_text(page) == "pypdf assembled text"


def test_pdf_visual_filter_keeps_useful_images_and_drops_decorations():
    page = SimpleNamespace(images=[
        SimpleNamespace(image=Image.new("RGB", (640, 420), "navy"), data=b""),
        SimpleNamespace(image=Image.new("RGB", (40, 40), "red"), data=b""),
        SimpleNamespace(image=Image.new("RGB", (1200, 80), "black"), data=b""),
    ])
    visuals = extract._extract_page_visuals(page, 7)
    assert len(visuals) == 1
    assert visuals[0]["page"] == 7
    assert visuals[0]["width"] == 640
    assert visuals[0]["data"].startswith(b"\xff\xd8")


def test_clean_text_joins_hyphenated_linebreaks():
    assert extract.clean_text("inter-\nnational") == "international"


def test_clean_text_collapses_blank_runs_and_whitespace():
    assert extract.clean_text("foo   bar\n\n\n\nbaz") == "foo bar\n\nbaz"


def test_clean_text_empty():
    assert extract.clean_text("") == ""
    assert extract.clean_text(None) == ""


def test_is_blocked_ip_private_and_special():
    for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254",
               "0.0.0.0", "::1", "fe80::1"):
        assert extract._is_blocked_ip(ip), ip


def test_is_blocked_ip_allows_public():
    for ip in ("8.8.8.8", "1.1.1.1", "93.184.216.34"):
        assert not extract._is_blocked_ip(ip), ip


def test_is_blocked_ip_rejects_garbage():
    assert extract._is_blocked_ip("not-an-ip")


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_http_scheme():
    with pytest.raises(extract.ExtractError):
        await extract.fetch_url("file:///etc/passwd")
    with pytest.raises(extract.ExtractError):
        await extract.fetch_url("ftp://example.com/x")


@pytest.mark.asyncio
async def test_fetch_url_blocks_loopback_host():
    with pytest.raises(extract.ExtractError):
        await extract.fetch_url("http://localhost/admin")
    with pytest.raises(extract.ExtractError):
        await extract.fetch_url("http://127.0.0.1:8000/")


@pytest.mark.asyncio
async def test_fetch_url_blocks_redirect_before_requesting_private_target(monkeypatch):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "http://127.0.0.1/admin"}, request=request,
        )

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(extract.httpx, "AsyncClient", client_factory)
    with pytest.raises(extract.ExtractError, match="private|blocked"):
        await extract.fetch_url("http://93.184.216.34/article")
    assert requested == ["http://93.184.216.34/article"]


@pytest.mark.asyncio
async def test_fetch_url_stops_streaming_at_size_limit(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"x" * (extract.MAX_FETCH_BYTES + 1),
            request=request,
        )

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(extract.httpx, "AsyncClient", client_factory)
    with pytest.raises(extract.ExtractError, match="too large"):
        await extract.fetch_url("http://93.184.216.34/article")
