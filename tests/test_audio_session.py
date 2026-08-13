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


def _extract_decl(src: str, name: str) -> str:
    """Pull one top-level `const/let <name> = …;` line, so the test runs against
    the shipped values rather than copies of them."""
    for line in src.splitlines():
        stripped = line.strip()
        for kw in ("const ", "let "):
            if stripped.startswith(kw + name + " ="):
                return stripped
    raise AssertionError(f"declaration for {name} not found")


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


# ── Volume boost ─────────────────────────────────────────────────────────────
# 'transient' plays over music but the OS decides how much it ducks, and there
# is no web API to duck harder — so the only lever is our own level. An <audio>
# element's `volume` can't provide it (it only attenuates, and iOS ignores it),
# hence Web Audio: element → gain → limiter. The rule that matters is that the
# DEFAULT touches nothing: audio that works outranks audio that's louder.

def _gain(script, *, web_audio=True, running=False, decode_fails=False,
          fetch_fails=False):
    """Run the real boost helpers out of app-shell.js against a stubbed context."""
    src = open(SHELL_JS, encoding="utf-8").read()
    decls = "let _actx = null, _gainNode = null, _audioGain = 1;\n"
    decls += "const AUDIO_GAIN_MIN = 0.5, AUDIO_GAIN_MAX = 3;\n"
    decls += "let _keepArmed = false;\n"
    decls += "\n".join(_extract_decl(src, n) for n in
                       ("_bufCache", "BUF_CACHE_MAX", "_playing")) + "\n"
    fns = "\n".join(_extract(src, d) for d in (
        "function clampGain(", "function audioGraph(", "function resumeAudio(",
        "function decodeAudio(", "function playBoosted(", "function stopBoosted(",
        "function setAudioGain(", "function keepAudioRunning("))

    harness = f"""
    let started = [], resumed = 0, fetched = [];
    class GainNode {{ constructor() {{ this.gain = {{ value: 1 }}; }} connect() {{}} }}
    class Ctx {{
      constructor() {{ this.state = START_STATE; this.destination = {{}}; }}
      createGain() {{ return new GainNode(); }}
      createDynamicsCompressor() {{
        return {{ threshold: {{}}, knee: {{}}, ratio: {{}}, attack: {{}}, release: {{}},
                 connect() {{}} }};
      }}
      createBufferSource() {{
        const self = this;
        return {{ buffer: null, onended: null, connect() {{}},
                  start() {{ started.push(this.buffer); }}, stop() {{}} }};
      }}
      decodeAudioData(bytes, ok, fail) {{
        if ({json.dumps(decode_fails)}) {{ fail(new Error('bad mp3')); return; }}
        ok({{ id: bytes }});
      }}
      resume() {{ resumed++; const self = this;
                  return Promise.resolve().then(() => {{ self.state = 'running'; }}); }}
      addEventListener() {{}}
    }}
    const START_STATE = {json.dumps('running' if running else 'suspended')};
    const listeners = [];
    const document = {{ addEventListener: (ev, fn) => listeners.push(ev) }};
    const window = {json.dumps(web_audio)} ? {{ AudioContext: Ctx }} : {{}};
    function nativeFetch(url) {{
      fetched.push(url);
      if ({json.dumps(fetch_fails)}) return Promise.resolve({{ ok: false }});
      return Promise.resolve({{ ok: true, arrayBuffer: () => Promise.resolve('bytes:' + url) }});
    }}
    {decls}
    {fns}
    function done(o) {{ console.log(JSON.stringify(o)); }}
    {script}
    """
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_default_volume_plays_nothing_through_the_graph():
    """1× must leave the audio path exactly as it was — no context, no fetch,
    nothing new that can fail between a clip and the speaker."""
    res = _gain("""
      setAudioGain(1);
      const p = playBoosted('/api/tts?text=hi');
      done({ handled: p !== null, ctx: !!_actx, fetched: fetched.length });
    """)
    assert res == {"handled": False, "ctx": False, "fetched": 0}


def test_a_boost_decodes_and_plays_the_clip():
    """Decoding into a buffer, NOT capturing the <audio> element: capture is
    permanent and silently produces nothing on iOS Safari, which is what "I
    turned the volume up and now hear nothing" was."""
    res = _gain("""
      setAudioGain(1.8);
      playBoosted('/api/tts?text=hi').then(() => done({
        started: started.length, fetched, gain: _gainNode.gain.value,
      }));
    """, running=True)
    assert res["started"] == 1
    assert res["fetched"] == ["/api/tts?text=hi"]
    assert res["gain"] == 1.8


def test_a_clip_is_decoded_once_and_replayed_from_the_buffer():
    res = _gain("""
      setAudioGain(2);
      playBoosted('/api/tts?text=hi')
        .then(() => playBoosted('/api/tts?text=hi'))
        .then(() => done({ started: started.length, fetched: fetched.length }));
    """, running=True)
    assert res["started"] == 2      # played twice…
    assert res["fetched"] == 1      # …fetched and decoded once


def test_a_sleeping_context_hands_the_clip_back():
    """Returning null is how the caller knows to play it natively — unboosted,
    but audible. Silence is never the right answer."""
    res = _gain("""
      setAudioGain(2);
      const p = playBoosted('/api/tts?text=hi');
      done({ handled: p !== null, resumed, fetched: fetched.length });
    """)
    assert res["handled"] is False
    assert res["resumed"] >= 1      # …and awake for the next one
    assert res["fetched"] == 0


def test_a_failed_fetch_or_decode_rejects_so_the_caller_falls_back():
    for kw in ({"fetch_fails": True}, {"decode_fails": True}):
        res = _gain("""
          setAudioGain(2);
          playBoosted('/api/tts?text=hi').then(
            () => done({ rejected: false }),
            () => done({ rejected: true, started: started.length }));
        """, running=True, **kw)
        assert res["rejected"] is True
        assert res.get("started", 0) == 0


def test_a_browser_without_web_audio_hands_the_clip_back():
    assert _gain("""
      setAudioGain(2);
      done({ handled: playBoosted('/api/tts?text=hi') !== null, ctx: !!_actx });
    """, web_audio=False) == {"handled": False, "ctx": False}


def test_changing_the_volume_moves_the_gain_live():
    res = _gain("""
      setAudioGain(2);
      audioGraph();
      setAudioGain(0.7);
      done({ gain: _gainNode.gain.value });
    """, running=True)
    assert res["gain"] == 0.7


def test_the_multiplier_is_clamped():
    res = _gain("""
      done({
        high: clampGain(50), low: clampGain(0.01), off: clampGain(0),
        junk: clampGain('loud'), missing: clampGain(undefined), ok: clampGain(1.5),
      });
    """)
    assert res == {"high": 3, "low": 0.5, "off": 1, "junk": 1, "missing": 1, "ok": 1.5}


def test_a_boost_keeps_the_context_awake_on_every_gesture():
    """Not a one-shot: iOS suspends a context on any audio interruption, and a
    sleeping context can play nothing at all."""
    res = _gain("""
      setAudioGain(2.2);
      done({ listeners: listeners.slice(), armed: _keepArmed, ctx: !!_actx });
    """)
    assert res["armed"] is True
    assert "pointerdown" in res["listeners"]
    assert res["ctx"] is True


def test_stopping_silences_the_current_clip():
    res = _gain("""
      setAudioGain(2);
      playBoosted('/api/tts?text=hi').then(() => {
        const before = _playing !== null;
        stopBoosted();
        done({ before, after: _playing !== null });
      });
    """, running=True)
    assert res == {"before": True, "after": False}


# ── Server: the stored multiplier ────────────────────────────────────────────

def test_audio_volume_setting_is_clamped_server_side():
    import main
    assert main._audio_volume(1.5) == 1.5
    assert main._audio_volume("2") == 2.0
    assert main._audio_volume(99) == main.AUDIO_VOLUME_MAX
    assert main._audio_volume(0.01) == main.AUDIO_VOLUME_MIN
    # Anything unreadable means "as recorded", never silence.
    for junk in (None, "", "loud", 0, float("nan")):
        assert main._audio_volume(junk) == 1.0
