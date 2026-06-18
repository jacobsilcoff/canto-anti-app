"""Tests for the reader URL/PDF import extractor.

These cover the deterministic, no-network pieces: text cleanup and the SSRF
host guard. The heavy parsers (trafilatura/pypdf) and live fetching are not
exercised here (network + optional deps).
"""

import pytest

import extract


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
