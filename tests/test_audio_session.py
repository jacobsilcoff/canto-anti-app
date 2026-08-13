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


# ── Volume boost ─────────────────────────────────────────────────────────────
# 'transient' plays over music but the OS decides how much it ducks, and there
# is no web API to duck harder — so the only lever is our own level. An <audio>
# element's `volume` can't provide it (it only attenuates, and iOS ignores it),
# hence Web Audio: element → gain → limiter. The rule that matters is that the
# DEFAULT touches nothing: audio that works outranks audio that's louder.

def _gain(script, *, web_audio=True, throws=False, running=False):
    """Run the real gain helpers out of app-shell.js against a stubbed context."""
    src = open(SHELL_JS, encoding="utf-8").read()
    decls = "let _actx = null, _gainNode = null, _routed = null, _audioGain = 1;\n"
    decls += "const AUDIO_GAIN_MIN = 0.5, AUDIO_GAIN_MAX = 3;\n"
    fns = "\n".join(_extract(src, d) for d in (
        "function clampGain(", "function audioGraph(", "function prepareAudio(",
        "function resumeAudio(", "function setAudioGain(", "function keepAudioRunning("))
    fns = "let _keepArmed = false;\n" + fns

    harness = f"""
    const routedCalls = [];
    let resumed = 0;
    class GainNode {{ constructor() {{ this.gain = {{ value: 1 }}; }} connect() {{}} }}
    class Ctx {{
      constructor() {{ this.state = START_STATE; this.destination = {{}}; }}
      createGain() {{ return new GainNode(); }}
      createDynamicsCompressor() {{
        return {{ threshold: {{}}, knee: {{}}, ratio: {{}}, attack: {{}}, release: {{}},
                 connect() {{}} }};
      }}
      createMediaElementSource(el) {{
        if ({json.dumps(throws)}) throw new Error('already connected');
        routedCalls.push(el.id);
        return {{ connect() {{}} }};
      }}
      // Async on purpose: a real resume() settles later (and on iOS only inside
      // a gesture), which is exactly why a clip must not be routed on the
      // strength of having just asked for one.
      resume() {{ resumed++; const self = this;
                  return Promise.resolve().then(() => {{ self.state = 'running'; }}); }}
      addEventListener() {{}}
    }}
    const START_STATE = {json.dumps('running' if running else 'suspended')};
    const listeners = [];
    const document = {{ addEventListener: (ev, fn) => listeners.push(ev) }};
    const window = {json.dumps(web_audio)} ? {{ AudioContext: Ctx }} : {{}};
    {decls}
    {fns}
    function done(o) {{ console.log(JSON.stringify(o)); }}
    {script}
    """
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_default_volume_routes_nothing():
    """1× must leave the audio path exactly as it was — no context, no source
    node, nothing new that can fail between a clip and the speaker."""
    res = _gain("""
      setAudioGain(1);
      prepareAudio({ id: 'clip' });
      done({ routed: routedCalls.length, ctx: !!_actx });
    """)
    assert res == {"routed": 0, "ctx": False}


def test_a_boost_routes_each_element_once():
    """createMediaElementSource throws if an element is routed twice, and clips
    are cached and replayed all session."""
    res = _gain("""
      setAudioGain(1.8);
      const el = { id: 'clip' };
      prepareAudio(el);
      prepareAudio(el);
      prepareAudio({ id: 'other' });
      done({ routed: routedCalls, gain: _gainNode.gain.value });
    """, running=True)
    assert res["routed"] == ["clip", "other"]
    assert res["gain"] == 1.8


def test_a_clip_is_never_routed_into_a_sleeping_context():
    """The bug this rule exists for: a routed element's sound goes ONLY through
    the graph, so routing into a suspended context makes the tap silent while the
    element plays on — and every clip stacked up that way arrives at once when a
    later gesture wakes it ("the speaker did nothing, then leaving the lesson
    played several sounds over each other"). Play it natively instead."""
    res = _gain("""
      setAudioGain(2);
      prepareAudio({ id: 'clip' });
      done({ routed: routedCalls.length, resumed, state: _actx.state });
    """)
    assert res["routed"] == 0        # unboosted this time…
    assert res["resumed"] >= 1       # …and awake for the next one


def test_the_next_clip_is_routed_once_the_context_wakes():
    res = _gain("""
      setAudioGain(2);
      prepareAudio({ id: 'first' });          // context asleep — plays natively
      Promise.resolve().then(() => {          // resume() lands
        prepareAudio({ id: 'second' });
        done({ routed: routedCalls, state: _actx.state });
      });
    """)
    assert res["routed"] == ["second"]
    assert res["state"] == "running"


def test_changing_the_volume_moves_the_gain_live():
    res = _gain("""
      setAudioGain(2);
      prepareAudio({ id: 'clip' });
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


def test_a_browser_without_web_audio_still_plays():
    """No AudioContext (or an exception building the graph) must leave the
    element to play natively rather than swallow the clip."""
    assert _gain("""
      setAudioGain(2);
      prepareAudio({ id: 'clip' });
      done({ ctx: !!_actx });
    """, web_audio=False) == {"ctx": False}


def test_a_failed_route_never_blocks_playback():
    res = _gain("""
      setAudioGain(2);
      prepareAudio({ id: 'clip' });
      done({ routed: routedCalls.length, threw: false });
    """, throws=True, running=True)
    assert res == {"routed": 0, "threw": False}


def test_a_boost_keeps_the_context_awake_on_every_gesture():
    """Not a one-shot: iOS suspends a context on any audio interruption, and a
    context that goes back to sleep silences every element routed through it."""
    res = _gain("""
      setAudioGain(2.2);
      done({ listeners: listeners.slice(), armed: _keepArmed, ctx: !!_actx });
    """)
    assert res["armed"] is True
    assert "pointerdown" in res["listeners"]
    assert res["ctx"] is True          # built up front, ready for the first tap


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
