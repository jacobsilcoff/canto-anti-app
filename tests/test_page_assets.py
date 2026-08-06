"""Page JS/CSS must live in /static/pages/ and be fingerprinted.

The HTML documents are served `Cache-Control: no-cache` so they always pick up
current asset URLs — which means anything inline in them is re-downloaded on
every single navigation. /learn was shipping 82 KB gzipped per page view that
way, none of it changing between deploys.

Two rules keep that from creeping back:
  1. no large inline <script>/<style> in a page (the theme pre-paint snippet is
     the deliberate exception — it must run before first paint)
  2. every /static/pages/ URL _html emits carries ?v=<ASSET_VERSION>, or it
     would be cached immutably and never update again
"""
import glob
import os
import re
import tempfile

import pytest

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "assets.db"))

import main

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
PAGES = sorted(glob.glob(os.path.join(STATIC, "*.html")))

# The pre-paint theme snippet is ~184 bytes. Anything materially larger than
# that belongs in a cacheable file.
MAX_INLINE = 4096

BLOCK = re.compile(r"<(script|style)([^>]*)>(.*?)</\1>", re.S)
PAGE_ASSET = re.compile(r"/static/pages/[A-Za-z0-9_.-]+\.(?:js|css)")


@pytest.mark.parametrize("path", PAGES, ids=lambda p: os.path.basename(p))
def test_no_large_inline_blocks(path):
    html = open(path, encoding="utf-8").read()
    for m in BLOCK.finditer(html):
        tag, attrs, body = m.group(1), m.group(2), m.group(3)
        if "src=" in attrs:
            continue
        if re.search(r"\{\{[A-Z_]+\}\}", body):
            continue  # _html substitutes into it; it has to stay inline
        assert len(body) <= MAX_INLINE, (
            f"{os.path.basename(path)} has a {len(body)}-byte inline <{tag}>. "
            f"Move it to static/pages/ so it caches instead of re-downloading "
            f"on every navigation."
        )


@pytest.mark.parametrize("path", PAGES, ids=lambda p: os.path.basename(p))
def test_page_assets_are_fingerprinted_and_exist(path):
    name = os.path.basename(path)
    html = main._html(name).body.decode()

    unversioned = re.findall(PAGE_ASSET.pattern + r"(?!\?v=)", html)
    assert not unversioned, f"{name} emits un-fingerprinted assets: {unversioned}"

    for ref in re.findall(r'"(/static/pages/[^"?]+)', html):
        assert os.path.exists(ref.lstrip("/")), f"{name} references missing {ref}"


def test_prepaint_theme_snippet_stays_inline():
    """An external file could let the page paint before the theme is applied,
    flashing the wrong colours. It must not be extracted."""
    missing = [
        os.path.basename(p)
        for p in PAGES
        if "localStorage.getItem('theme')" not in open(p, encoding="utf-8").read()
    ]
    assert not missing, f"pages lost their pre-paint theme snippet: {missing}"
