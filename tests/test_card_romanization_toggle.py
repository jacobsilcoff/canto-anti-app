"""The pronunciation-card romanization control uses the real account setting."""
import json
import os
import shutil
import subprocess

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS_JS = os.path.join(ROOT, "static", "pages", "cards.js")

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available to run card-page code")


def _extract(src: str, decl: str) -> str:
    start = src.index(decl)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced function: {decl}")


@needs_node
def test_audio_card_toggle_updates_the_setting_and_current_card():
    src = open(CARDS_JS, encoding="utf-8").read()
    fn = _extract(src, "async function toggleAudioRomanization(")
    script = f"""
      let settings = {{ audio_show_romanization: true }};
      let _savingAudioRomanization = false;
      let refreshes = 0, toast = '', request = null;
      function _refreshCardDisplay() {{ refreshes++; }}
      function showToast(s) {{ toast = s; }}
      async function fetch(url, opts) {{
        request = {{ url, method: opts.method, body: JSON.parse(opts.body) }};
        return {{ ok: true }};
      }}
      {fn}
      toggleAudioRomanization().then(() => console.log(JSON.stringify({{
        shown: settings.audio_show_romanization, refreshes, toast, request
      }})));
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.strip())
    assert result["shown"] is False
    assert result["refreshes"] == 2
    assert result["request"] == {
        "url": "/api/settings", "method": "PUT",
        "body": {"audio_show_romanization": False},
    }
    assert "hidden" in result["toast"]


def test_audio_card_renderer_contains_the_contextual_control():
    src = open(CARDS_JS, encoding="utf-8").read()
    assert "romanization-card-toggle" in src
    assert "Hide romanization" in src and "Show romanization" in src
    assert "romanToggle.textContent = 'Aa'" in src
    assert "romanToggle.setAttribute('aria-label', romanToggle.title)" in src
    assert "romanToggle.onclick = toggleAudioRomanization" in src
