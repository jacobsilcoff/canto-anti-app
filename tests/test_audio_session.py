"""Playing a clip must not stop the user's music.

iOS treats a page playing an <audio> element as exclusive playback, so every
flashcard clip and lesson prompt paused whatever the learner was listening to —
and podcasts don't reliably resume. The Audio Session API lets the page declare
what kind of audio it is; 'transient' plays over other audio (ducking it) the
way turn-by-turn directions do.

'ambient' would also mix, but is silenced by the Ring/Silent switch — that would
quietly break listening drills for anyone whose phone is on silent, which is a
worse bug than the one being fixed. These tests pin that choice.

Runs the real applyAudioSession out of static/app-shell.js under node.
"""
import json
import os
import shutil
import subprocess

import pytest

SHELL_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "app-shell.js",
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available to run the shell code")


def _extract(src: str, decl: str) -> str:
    start = src.index(decl)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces extracting {decl}")


def _apply(settings, *, supported=True, throws=False):
    """Run applyAudioSession against a stubbed navigator; return the type set."""
    src = open(SHELL_JS, encoding="utf-8").read()
    fn = _extract(src, "function applyAudioSession(")

    harness = f"""
    let assigned = null;
    const session = {{
      _t: 'auto',
      get type() {{ return this._t; }},
      set type(v) {{
        if ({json.dumps(throws)}) throw new TypeError('unsupported');
        assigned = v; this._t = v;
      }},
    }};
    const navigator = {json.dumps(supported)} ? {{ audioSession: session }} : {{}};
    {fn}
    applyAudioSession({json.dumps(settings)});
    console.log(JSON.stringify({{ assigned }}));
    """
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["assigned"]


def test_audio_mixes_with_other_playback_by_default():
    """The default has to be the non-destructive one — a flashcard clip killing
    someone's podcast is never what they wanted."""
    assert _apply({}) == "transient"
    assert _apply({"audio_mix": True}) == "transient"
    assert _apply(None) == "transient"


def test_never_picks_ambient():
    """'ambient' mixes too, but the Ring/Silent switch mutes it — listening
    drills would go silent with no explanation."""
    for settings in ({}, {"audio_mix": True}, {"audio_mix": False}):
        assert _apply(settings) != "ambient"


def test_turning_the_setting_off_restores_exclusive_playback():
    assert _apply({"audio_mix": False}) == "playback"


def test_browsers_without_the_api_are_left_alone():
    """Progressive enhancement: anything but Safari 16.4+ today. Must not throw
    — this runs on every page load."""
    assert _apply({}, supported=False) is None


def test_a_rejected_value_does_not_break_page_load():
    """A UA that exposes audioSession but rejects the value must not take the
    shell down with it."""
    assert _apply({}, throws=True) is None
