"""Audio must not go randomly silent.

Two defects made a single transient TTS failure look like "the audio sometimes
just doesn't play":

  * SERVER — `audio.generate` made ONE attempt against edge-tts, a free
    unofficial Microsoft endpoint that throttles and drops sockets. One hiccup
    became a 502 (or, on `/api/audio/{id}`, an unhandled 500).
  * CLIENT — `_prewarmTTS` cached the `Audio` element whether or not its fetch
    succeeded. An <audio> whose load errored is permanently dead, so every later
    `play()` on it rejected, and `playTTS` swallowed that with `.catch(() => {})`.
    The clip stayed silent for the whole session while every other clip worked.

The client half runs the REAL functions out of static/pages/learn.js under node
with a stubbed Audio/DOM, so it checks the shipped source rather than a
transcription of it.
"""
import asyncio
import json
import os
import shutil
import subprocess

import pytest

import audio

LEARN_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "pages", "learn.js",
)


# ── Server: retry, empty-guard, timeout ──────────────────────────────────────

@pytest.fixture
def stub_tts(monkeypatch):
    """Replace the edge-tts call and speed the backoff up so tests stay fast."""
    monkeypatch.setattr(audio, "_BACKOFF", (0.001, 0.001))
    calls = {"n": 0}

    def install(fn):
        async def wrapped(text, lang):
            calls["n"] += 1
            return await fn(calls["n"])
        monkeypatch.setattr(audio, "_generate_once", wrapped)
        return calls

    return install


def test_generate_retries_a_transient_failure(stub_tts):
    async def flaky(n):
        if n < 3:
            raise RuntimeError("websocket dropped")
        return b"MP3"
    calls = stub_tts(flaky)
    assert asyncio.run(audio.generate("x", "yue")) == b"MP3"
    assert calls["n"] == 3


def test_generate_gives_up_after_the_attempt_budget(stub_tts):
    async def dead(n):
        raise RuntimeError("403 forbidden")
    calls = stub_tts(dead)
    with pytest.raises(RuntimeError, match="403"):
        asyncio.run(audio.generate("x", "yue"))
    assert calls["n"] == audio._ATTEMPTS


def test_generate_never_returns_empty_audio(stub_tts):
    """Some edge-tts versions end an empty stream without raising. Returning b""
    would store silence as the card's audio, which plays as nothing forever."""
    async def empty(n):
        return b""
    calls = stub_tts(empty)
    with pytest.raises(Exception):
        asyncio.run(audio.generate("x", "yue"))
    assert calls["n"] == audio._ATTEMPTS


def test_generate_retries_past_an_empty_response(stub_tts):
    async def empty_then_ok(n):
        return b"" if n == 1 else b"MP3"
    stub_tts(empty_then_ok)
    assert asyncio.run(audio.generate("x", "yue")) == b"MP3"


def test_generate_does_not_hang_on_a_stuck_socket(stub_tts, monkeypatch):
    monkeypatch.setattr(audio, "_TIMEOUT_S", 0.05)

    async def never(n):
        await asyncio.sleep(30)
    calls = stub_tts(never)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(audio.generate("x", "yue"))
    assert calls["n"] == audio._ATTEMPTS


# ── Client: the prewarm cache must not keep a dead element ───────────────────

pytestmark_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available to run the player code")


def _extract(src: str, decl: str) -> str:
    """Pull one `function name(...) {...}` out of the file by brace matching."""
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
    """Pull one top-level `const/let <name> = …;` line out of the file, so the
    test runs against the shipped values (concurrency, timeouts) not copies."""
    for line in src.splitlines():
        stripped = line.strip()
        for kw in ("const ", "let "):
            if stripped.startswith(kw + name + " ="):
                return stripped
    raise AssertionError(f"declaration for {name} not found")


def _run_tts(script: str):
    """Run the real prewarm/play functions from learn.js against a stub Audio."""
    src = open(LEARN_JS, encoding="utf-8").read()
    decls = "\n".join(_extract_decl(src, n) for n in (
        "_ttsCache", "_ttsQueue", "_ttsPending", "_ttsInFlight",
        "TTS_PREWARM_CONCURRENCY", "TTS_PREWARM_TIMEOUT_MS"))
    fns = decls + "\n" + "\n".join(_extract(src, d) for d in (
        "function _ttsKey(", "function _ttsUrl(", "function _makeTTSAudio(",
        "function _pumpTTSQueue(", "function _prewarmTTS(", "function playTTS("))

    harness = """
    // ── Stub Audio: records every element built, and lets a test decide which
    //    URLs "fail to load" so we can drive the error path deterministically.
    const built = [];
    const failing = new Set();
    let _audio = null;

    class Audio {
      constructor(url) {
        this.src = url; this.error = null; this.paused = true;
        this.currentTime = 0; this.preload = '';
        this._handlers = {};
        built.push(this);
      }
      addEventListener(ev, fn) { (this._handlers[ev] = this._handlers[ev] || []).push(fn); }
      _emit(ev) { (this._handlers[ev] || []).slice().forEach(fn => fn()); }
      load() {
        // Resolve synchronously so the queue drains within the test.
        if (failing.has(this.src)) { this.error = { code: 4 }; this._emit('error'); }
        else { this._emit('loadeddata'); }
      }
      pause() { this.paused = true; }
      play() {
        if (this.error) return Promise.reject(new Error('dead element'));
        if (failing.has(this.src)) {
          this.error = { code: 4 }; this._emit('error');
          return Promise.reject(new Error('load failed'));
        }
        this.paused = false;
        return Promise.resolve();
      }
    }
    // The shipped playTTS asks the app shell to route the element through the
    // volume-boost gain graph. `window` exists in a browser; the harness is what
    // was incomplete. A test can install a CantoShell to exercise that path.
    const window = { CantoShell: null };
    const CantoShell = null;
    const setTimeoutReal = setTimeout;
    // Report and exit immediately: the real prewarm timers are 15s and would
    // otherwise keep node's event loop alive long past the assertion.
    function done(o) { console.log(JSON.stringify(o)); process.exit(0); }
    """

    prog = harness + "\n" + fns + "\n" + script
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytestmark_node
def test_prewarm_evicts_a_clip_that_failed_to_load():
    """The core bug: a failed prewarm must not stay in the cache, or the clip is
    silent for the rest of the session no matter how often the learner taps."""
    res = _run_tts("""
      const url = _ttsUrl('hello', 'yue');
      failing.add(url);
      _prewarmTTS('hello', 'yue');
      const cachedAfterFail = !!_ttsCache[_ttsKey('hello', 'yue')];
      // The clip recovers once the server does.
      failing.delete(url);
      playTTS('hello', 'yue');
      setTimeoutReal(() => {
        done({
          cachedAfterFail,
          playing: built.some(a => a.src === url && !a.paused),
        });
      }, 10);
    """)
    assert res["cachedAfterFail"] is False   # evicted, not kept as a corpse
    assert res["playing"] is True            # and it plays once the server is back


@pytestmark_node
def test_play_retries_once_on_a_poisoned_element():
    """Even if a dead element is somehow current, one tap must recover."""
    res = _run_tts("""
      const url = _ttsUrl('hi', 'yue');
      const key = _ttsKey('hi', 'yue');
      const dead = new Audio(url);
      dead.error = { code: 4 };
      _ttsCache[key] = dead;
      playTTS('hi', 'yue');
      setTimeoutReal(() => {
        done({
          rebuilt: _ttsCache[key] !== dead,
          playing: built.some(a => a !== dead && a.src === url && !a.paused),
        });
      }, 10);
    """)
    assert res["rebuilt"] is True
    assert res["playing"] is True


@pytestmark_node
def test_autoplay_block_does_not_evict_a_healthy_clip():
    """NotAllowedError is the browser's autoplay policy, not a broken clip —
    retrying fails identically and throwing the cache entry away is pure waste."""
    res = _run_tts("""
      const key = _ttsKey('ok', 'yue');
      _prewarmTTS('ok', 'yue');
      const el = _ttsCache[key];
      el.play = () => { const e = new Error('blocked'); e.name = 'NotAllowedError';
                        return Promise.reject(e); };
      playTTS('ok', 'yue');
      setTimeoutReal(() => {
        done({ same: _ttsCache[key] === el, built: built.length });
      }, 10);
    """)
    assert res["same"] is True     # kept
    assert res["built"] == 1       # and no pointless rebuild


@pytestmark_node
def test_prewarm_is_throttled():
    """The whole-lesson prefetch used to fire every clip at once; each one is a
    separate edge-tts websocket on the server, and that burst is what provoked
    the throttling in the first place."""
    res = _run_tts("""
      // Nothing resolves, so everything started stays in flight.
      Audio.prototype.load = function () {};
      for (let i = 0; i < 25; i++) _prewarmTTS('word' + i, 'yue');
      done({ started: built.length, queued: _ttsQueue.length });
    """)
    assert res["started"] == 3      # TTS_PREWARM_CONCURRENCY
    assert res["queued"] == 22      # the rest wait their turn


@pytestmark_node
def test_prewarm_queue_drains_and_dedupes():
    res = _run_tts("""
      for (let i = 0; i < 10; i++) _prewarmTTS('w' + i, 'yue');
      _prewarmTTS('w0', 'yue');            // already cached — no second request
      done({ built: built.length, queued: _ttsQueue.length });
    """)
    assert res["built"] == 10       # all drained (loads resolve synchronously)
    assert res["queued"] == 0


@pytestmark_node
def test_a_broken_volume_booster_cannot_silence_a_clip():
    """The boost routes playback through Web Audio. If anything there throws,
    the clip must still play — louder is a nicety, audible is the product."""
    res = _run_tts("""
      window.CantoShell = { prepareAudio() { throw new Error('no AudioContext'); } };
      let threw = false;
      try { playTTS('hi', 'yue'); } catch (e) { threw = true; }
      setTimeoutReal(() => {
        done({ threw, playing: built.some(a => !a.paused) });
      }, 10);
    """)
    assert res["threw"] is False
    assert res["playing"] is True
