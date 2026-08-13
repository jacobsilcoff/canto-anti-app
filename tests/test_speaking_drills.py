"""Dead audio must LOOK dead, and speaking drills must be cheap and forgiving.

Two behaviours are pinned here.

  * A clip that can't be fetched greys its 🔊 button out, and a drill whose only
    question IS the sound (listening, audio blitz, "which tone did you hear?")
    is dropped from the run instead of leaving the learner staring at a button
    that silently does nothing. A drill removed that way must not count against
    the score or strand the progress bar short of the end.
  * Speaking drills are assembled from the course's OWN material (no LLM) and
    graded by comparison, not by a model. The comparison is deliberately
    permissive: a recogniser routinely returns a homophone of what was said —
    near-universal for tonal languages — and a learner who pronounced the phrase
    correctly must not be marked wrong for the machine's choice of character.

The client half runs the REAL functions out of static/pages/learn.js under node
with a stubbed DOM, so it checks the shipped source rather than a transcription.
"""
import json
import os
import shutil
import subprocess

import pytest

import audio
import main

LEARN_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "pages", "learn.js",
)

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available to run the player code")


# ── Server: the speech locale comes from the voice table ─────────────────────

def test_speech_locale_is_derived_from_the_voice():
    """Adding a language stays 'LANG_INFO entry + a voice' — no second table."""
    assert audio.locale_for("yue") == "zh-HK"
    assert audio.locale_for("fr") == "fr-FR"
    assert audio.locale_for("tl") == "fil-PH"


def test_every_supported_language_has_a_speech_locale():
    import translation
    for code in translation.LANG_INFO:
        assert audio.locale_for(code), f"{code} has no speech locale"


def test_an_unknown_language_gets_no_locale():
    """Falling back to Cantonese would transcribe into the wrong language
    entirely, which is worse than offering no speaking drill at all."""
    assert audio.locale_for("xx") == ""


# ── Server: speaking sets are recombination, not generation ──────────────────

def test_speak_prompt_only_takes_target_language_answers():
    # Production drill: English prompt, target options → the option is speakable.
    assert main._speak_prompt({
        "type": "choice", "prompt_lang": "english", "prompt": "the book",
        "options": ["本書", "枝筆"], "answer": 0,
    }) == ("本書", "the book", "")
    # Recognition drill: target prompt, ENGLISH options — the "answer" is English.
    assert main._speak_prompt({
        "type": "choice", "prompt_lang": "target", "prompt": "本書",
        "options": ["the book", "the pen"], "answer": 0,
    }) is None
    # Reorder drills store the whole sentence, so there's no re-joining to botch.
    assert main._speak_prompt({
        "type": "word_bank", "audio": "我today好攰", "prompt": "I'm tired today",
        "answer_tokens": ["我", "today", "好攰"], "answer_roman": "ngo5 ...",
    }) == ("我today好攰", "I'm tired today", "ngo5 ...")


def test_speak_prompt_rejects_a_broken_answer_index():
    assert main._speak_prompt({"type": "choice", "prompt_lang": "english",
                               "options": ["a"], "answer": 7}) is None
    assert main._speak_prompt({"type": "match", "pairs": []}) is None


def _speaking(pool, vocab=(), lang="fr"):
    return main._speaking_items(list(pool), list(vocab), lang)


def test_speaking_items_dedupe_and_cap():
    pool = [{"type": "type_answer", "answer": f"phrase {i}", "prompt": f"gloss {i}"}
            for i in range(30)]
    pool += [{"type": "type_answer", "answer": "phrase 0", "prompt": "dupe"}]
    items = _speaking(pool)
    assert len(items) == main._SPEAK_MAX
    targets = [i["target"] for i in items]
    assert len(set(targets)) == len(targets)
    assert all(i["type"] == "speak" and i["target"] for i in items)


def test_speaking_items_fall_back_to_taught_vocabulary():
    """A young course has few drilled sentences; its words still make a round."""
    items = _speaking([{"type": "type_answer", "answer": "bonjour", "prompt": "hello"}],
                      vocab=[{"label": "merci", "gloss": "thank you"},
                             {"label": "chat", "gloss": "cat"}])
    assert {i["target"] for i in items} == {"bonjour", "merci", "chat"}
    assert next(i for i in items if i["target"] == "merci")["prompt"] == "thank you"


def test_speaking_items_skip_a_paragraph():
    long_text = "x" * (main._SPEAK_MAX_CHARS + 1)
    items = _speaking([{"type": "type_answer", "answer": long_text, "prompt": "n/a"},
                       {"type": "type_answer", "answer": "court", "prompt": "short"}])
    assert [i["target"] for i in items] == ["court"]


def test_speaking_items_prefer_sentences_over_single_words():
    pool = [{"type": "type_answer", "answer": f"sentence {i}", "prompt": "g"}
            for i in range(20)]
    vocab = [{"label": f"word{i}", "gloss": "g"} for i in range(20)]
    items = _speaking(pool, vocab)
    words = [i for i in items if i["target"].startswith("word")]
    assert len(items) == main._SPEAK_MAX
    assert 0 < len(words) <= main._SPEAK_MAX // 3


# ── Client: dead audio, and the drills that depend on it ─────────────────────

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
    for line in src.splitlines():
        stripped = line.strip()
        for kw in ("const ", "let "):
            if stripped.startswith(kw + name + " ="):
                return stripped
    raise AssertionError(f"declaration for {name} not found")


_TTS_FNS = ("function _ttsKey(", "function _ttsUrl(", "function _setTTSHealth(",
            "function ttsHealth(", "function onTTSHealth(", "function _retryTTS(",
            "function _makeTTSAudio(", "function _pumpTTSQueue(",
            "function _prewarmTTS(", "function playTTS(", "function bindAudioBtn(")
_TTS_DECLS = ("_ttsCache", "_ttsQueue", "_ttsPending", "_ttsInFlight",
              "TTS_PREWARM_CONCURRENCY", "TTS_PREWARM_TIMEOUT_MS",
              "AUDIO_READY_TIMEOUT_MS", "_ttsHealth", "_ttsWatchers", "_ttsOkCount")


def _run(script: str, fns=(), decls=()):
    src = open(LEARN_JS, encoding="utf-8").read()
    body = "\n".join(_extract_decl(src, n) for n in decls)
    body += "\n" + "\n".join(_extract(src, d) for d in fns)

    harness = """
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
        if (failing.has(this.src)) { this.error = { code: 4 }; this._emit('error'); }
        else { this._emit('loadeddata'); }
      }
      pause() { this.paused = true; }
      play() {
        if (this.error) return Promise.reject(new Error('dead element'));
        this.paused = false;
        return Promise.resolve();
      }
    }
    // Just enough of a button for bindAudioBtn to work on.
    function stubBtn() {
      const cls = new Set();
      return { isConnected: true, disabled: false, title: '', onclick: null,
               cls,
               classList: { toggle: (c, on) => { on ? cls.add(c) : cls.delete(c); },
                            add: c => cls.add(c), remove: c => cls.delete(c),
                            contains: c => cls.has(c) } };
    }
    const window = { CantoShell: null };     // playTTS asks the shell for the gain graph
    const setTimeoutReal = setTimeout;
    function done(o) { console.log(JSON.stringify(o)); process.exit(0); }
    """
    prog = harness + "\n" + body + "\n" + script
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_a_clip_that_fails_to_load_is_marked_dead():
    res = _run("""
      failing.add(_ttsUrl('hello', 'yue'));
      _prewarmTTS('hello', 'yue');
      const dead = ttsHealth('hello', 'yue');
      _prewarmTTS('ok', 'yue');
      done({ dead, ok: ttsHealth('ok', 'yue'), unknown: ttsHealth('never', 'yue') });
    """, fns=_TTS_FNS, decls=_TTS_DECLS)
    assert res == {"dead": "fail", "ok": "ok", "unknown": "unknown"}


@needs_node
def test_a_speaker_button_is_not_tappable_until_its_clip_is_there():
    """Reported as "I clicked the speaker and nothing happened". A button that
    looks live before its clip has arrived is the same lie as one whose clip is
    dead — it just resolves differently."""
    res = _run("""
      Audio.prototype.load = function () {};       // hold the fetch open
      const btn = stubBtn();
      bindAudioBtn(btn, 'hello', 'yue');
      const loading = { disabled: btn.disabled, cls: [...btn.cls], title: btn.title };
      built[built.length - 1]._emit('loadeddata'); // the clip arrives
      done({ loading, readyDisabled: btn.disabled, readyCls: [...btn.cls] });
    """, fns=_TTS_FNS, decls=_TTS_DECLS)
    assert res["loading"]["disabled"] is True
    assert "audio-loading" in res["loading"]["cls"]
    assert res["loading"]["title"]                 # says why it's waiting
    # …and it goes live the moment the clip is there.
    assert res["readyDisabled"] is False
    assert res["readyCls"] == []


@needs_node
def test_a_stuck_clip_does_not_disable_the_button_forever():
    """A browser that defers loading despite load() must not leave a permanently
    dead-looking speaker — after the timeout the learner can tap and the play
    path does its own retry."""
    res = _run("""
      Audio.prototype.load = function () {};       // never settles
      const btn = stubBtn();
      bindAudioBtn(btn, 'hello', 'yue');
      done({ stuck: btn.disabled, timeout: AUDIO_READY_TIMEOUT_MS });
    """, fns=_TTS_FNS, decls=_TTS_DECLS)
    assert res["stuck"] is True
    assert res["timeout"] > 0        # …but bounded


@needs_node
def test_a_play_button_greys_out_and_comes_back():
    """A live-looking button that does nothing reads as the app being broken."""
    res = _run("""
      const url = _ttsUrl('hi', 'yue');
      failing.add(url);
      const btn = stubBtn();
      bindAudioBtn(btn, 'hi', 'yue');
      const afterFail = { disabled: btn.disabled, titled: !!btn.title };
      // The server recovers: the button must not stay dead forever.
      failing.delete(url);
      _retryTTS('hi', 'yue');
      done({ afterFail, disabledAfterRecovery: btn.disabled });
    """, fns=_TTS_FNS, decls=_TTS_DECLS)
    assert res["afterFail"] == {"disabled": True, "titled": True}
    assert res["disabledAfterRecovery"] is False


@needs_node
def test_which_drills_cannot_be_answered_without_sound():
    res = _run("""
      const cases = {
        listening:   _audioOnly({ type: 'listening', audio: '你好' }),
        blitz:       _audioOnly({ type: 'audio_blitz', items: [{ audio: 'a' }] }),
        soundMemory: _audioOnly({ type: 'memory_match', audio_mode: true, pairs: [] }),
        cardMemory:  _audioOnly({ type: 'memory_match', pairs: [] }),
        toneChoice:  _audioOnly({ type: 'choice', audio: '詩', hide_roman: true }),
        blockBuild:  _audioOnly({ type: 'block_build', audio: '가', hide_roman: true }),
        readable:    _audioOnly({ type: 'choice', audio: '你好', prompt: 'hello' }),
        spelled:     _audioOnly({ type: 'block_build', audio: '가', roman: 'ga' }),
        silent:      _audioOnly({ type: 'choice', prompt: 'hello' }),
        clips:       _exClips({ type: 'audio_blitz', items: [{ audio: 'a' }, { audio: 'b' }] }).length,
      };
      done(cases);
    """, fns=("function _exClips(", "function _audioOnly("))
    assert res == {"listening": True, "blitz": True, "soundMemory": True,
                   "cardMemory": False, "toneChoice": True, "blockBuild": True,
                   "readable": False, "spelled": False, "silent": False, "clips": 2}


_SKIP_FNS = _TTS_FNS + ("function _dropFromTotals(", "function _skipExercise(",
                        "function _exClips(", "function _audioOnly(",
                        "function _guardAudioExercise(")

_SKIP_HARNESS = """
    let rendered = 0, finished = 0, afterSeg = 0, toasts = [];
    function renderExercise() { rendered++; if (player.queue[player.idx]) _guardAudioExercise(player.queue[player.idx]); }
    function afterSegment() { afterSeg++; }
    function finishLesson() { finished++; }
    function updateBar() {}
    function learnToast(m) { toasts.push(m); }
"""


@needs_node
def test_an_unanswerable_listening_drill_is_dropped_after_one_retry():
    """One /api/tts hiccup shouldn't cost a drill — but a clip that stays dead
    must not leave the learner stuck on a question they cannot hear."""
    res = _run(_SKIP_HARNESS + """
      failing.add(_ttsUrl('你好', 'yue'));
      const listen = { type: 'listening', audio: '你好' };
      const readable = { type: 'choice', prompt: 'hello', options: ['a', 'b'], answer: 0 };
      const player = { lang: 'yue', queue: [listen, readable], idx: 0, segIdx: 0,
                       total: 2, segTotals: [2], graded: false, reviewStarted: false };
      globalThis.player = player;
      _guardAudioExercise(listen);
      const afterFirst = { dropped: player.queue.indexOf(listen) < 0,
                           retried: !!listen._audioRetried, total: player.total };
      done({ afterFirst, dropped: player.queue.indexOf(listen) < 0,
             left: player.queue.length, total: player.total,
             segTotal: player.segTotals[0], toasts: toasts.length,
             onScreen: player.queue[player.idx] === readable });
    """, fns=_SKIP_FNS, decls=_TTS_DECLS)
    # The first pass retries rather than dropping; the retry fails and it goes.
    assert res["afterFirst"]["retried"] is True
    assert res["dropped"] is True
    assert res["left"] == 1
    assert res["onScreen"] is True
    assert res["toasts"] == 1
    # It was never asked, so it must not count against the score or the bar.
    assert res["total"] == 1 and res["segTotal"] == 1


@needs_node
def test_a_working_clip_keeps_its_listening_drill():
    res = _run(_SKIP_HARNESS + """
      const listen = { type: 'listening', audio: '你好' };
      const player = { lang: 'yue', queue: [listen], idx: 0, segIdx: 0,
                       total: 1, segTotals: [1], graded: false, reviewStarted: false };
      globalThis.player = player;
      const dropped = _guardAudioExercise(listen);
      done({ dropped, left: player.queue.length, total: player.total });
    """, fns=_SKIP_FNS, decls=_TTS_DECLS)
    assert res == {"dropped": False, "left": 1, "total": 1}


@needs_node
def test_dropping_the_last_drill_ends_the_segment():
    res = _run(_SKIP_HARNESS + """
      failing.add(_ttsUrl('你好', 'yue'));
      const listen = { type: 'listening', audio: '你好' };
      const player = { lang: 'yue', queue: [listen], idx: 0, segIdx: 0,
                       total: 1, segTotals: [1], graded: false, reviewStarted: false };
      globalThis.player = player;
      _guardAudioExercise(listen);          // retries
      _guardAudioExercise(listen);          // gives up
      done({ left: player.queue.length, afterSeg, finished, total: player.total });
    """, fns=_SKIP_FNS, decls=_TTS_DECLS)
    assert res["left"] == 0
    assert res["afterSeg"] == 1 and res["finished"] == 0
    assert res["total"] == 0


# ── Client: permissive speech grading ────────────────────────────────────────

_SPEECH_FNS = ("function _speechNorm(", "function _stripTones(",
               "function _editDistance(", "function _speechClose(")


def _extract_type(src: str, name: str) -> str:
    """Pull one `name: { render(...) {...} },` entry out of EXERCISE_TYPES."""
    start = src.index(f"\n    {name}: {{")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return "const SPEAK = " + src[i:j + 1] + ";"
    raise AssertionError(f"unbalanced braces extracting {name}")


@needs_node
def test_the_speaking_drill_runs_end_to_end():
    """Drive the SHIPPED renderer: tap the mic, feed it a transcript, grade it.

    A homophone of the target has to pass — the recogniser picks the character,
    the learner only supplies the sound.
    """
    src = open(LEARN_JS, encoding="utf-8").read()
    body = "\n".join(_extract(src, d) for d in (
        "function esc(", "function _speechNorm(", "function _stripTones(",
        "function _editDistance(", "function _speechClose(", "function _isLatin(",
        "async function _spokenRoman(", "async function gradeSpoken(",
        "function speechSupported(", "function speechLangFor(")) + "\n" + \
        _extract_type(src, "speak")

    harness = """
    const LANGS = [{ code: 'yue', speech_lang: 'zh-HK' }];
    const player = { graded: false, lang: 'yue' };
    const els = {};
    function el() {
      return { innerHTML: '', textContent: '', className: '', disabled: false,
               title: '', onclick: null, _cls: [],
               classList: { add(c) { this._cls = (this._cls || []).concat(c); },
                            remove() {}, toggle() {} } };
    }
    ['sp-mic', 'sp-label', 'sp-heard', 'sp-listen'].forEach(id => els[id] = el());
    const document = {
      getElementById: id => els[id],
      // esc() builds a throwaway node to escape text.
      createElement: () => ({ set textContent(v) { this._t = v == null ? '' : String(v); },
                              get innerHTML() { return (this._t || '')
                                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }),
    };
    // Ruby lookups would hit the network; the offline oracle is tested elsewhere.
    const RUBY_LANGS = new Set(['yue']);
    function needsRuby(l) { return RUBY_LANGS.has(l); }
    function scriptClassFor() { return 'script-chinese'; }
    function cleanForTTS(t) { return t; }
    function targetSpan(t) { return esc(t); }
    function bindAudioBtn() {}
    function updateAction() {}
    const _roman = { '唔該借支筆': 'm4 goi1 ze3 zi1 bat1', '唔該借枝筆': 'm4 goi1 ze3 zi1 bat1',
                     '早晨': 'zou2 san4' };
    function _fetchTokens(text) {
      return Promise.resolve([{ roman: _roman[text] || text }]);
    }

    // Stub recogniser: a test feeds it transcripts.
    let live = null;
    class _SpeechRec {
      constructor() { live = this; this.lang = ''; }
      start() { this.onstart && this.onstart(); }
      stop() { this.onend && this.onend(); }
      _hear(text, ...alts) {
        this.onresult({ resultIndex: 0, results: [Object.assign(
          [{ transcript: text }, ...alts.map(a => ({ transcript: a }))],
          { isFinal: true })] });
      }
    }
    function done(o) { console.log(JSON.stringify(o)); process.exit(0); }
    """

    script = """
    const root = { innerHTML: '' };
    const ex = { type: 'speak', target: '唔該借支筆', prompt: 'excuse me, lend me a pen',
                 target_roman: 'm4 goi1 ze3 zi1 bat1' };
    const c = SPEAK.render(ex, root, 'yue');
    const before = c.isReady();
    els['sp-mic'].onclick();                       // tap the mic
    const langUsed = live.lang;
    live._hear('唔該借枝筆');                        // a homophone, wrong character
    const after = c.isReady();
    // Captured now: the second drill below re-renders and clears this line.
    const heardShown = els['sp-heard'].innerHTML.includes('枝');
    c.grade().then(ok => {
      const fb = c.feedback();
      // A genuinely different phrase must still be wrong.
      const ex2 = { type: 'speak', target: '早晨', prompt: 'good morning' };
      const c2 = SPEAK.render(ex2, root, 'yue');
      els['sp-mic'].onclick();
      live._hear('唔該借支筆');
      c2.grade().then(bad => done({
        before, after, langUsed, ok, checked: fb.checked, bad, heardShown,
      }));
    });
    """
    prog = harness + "\n" + body + "\n" + script
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["before"] is False        # nothing said yet — Check stays disabled
    assert res["after"] is True
    assert res["langUsed"] == "zh-HK"    # BCP-47 tag, not our internal code
    assert res["heardShown"] is True     # the learner sees what was transcribed
    assert res["ok"] is True and res["checked"] is True   # homophone accepted
    assert res["bad"] is False           # but not a different phrase


@needs_node
def test_speech_grading_is_permissive_but_not_blind():
    res = _run("""
      done({
        exact:       _speechClose('你好', '你好'),
        punctuation: _speechClose('Bonjour !', 'bonjour'),
        spacing:     _speechClose('唔 該 曬', '唔該曬'),
        casing:      _speechClose('JE SUIS FATIGUÉ', 'je suis fatigué'),
        // A recogniser hearing the phrase inside a longer utterance still heard it.
        embedded:    _speechClose('euh je suis fatigué', 'je suis fatigué'),
        // One character out of a long phrase: pronounced right, transcribed wrong.
        homophone:   _speechClose('唔該借枝筆', '唔該借支筆'),
        // Nothing like it.
        wrong:       _speechClose('bonsoir tout le monde', 'je suis fatigué'),
        empty:       _speechClose('', '你好'),
        // Two-character words are not a free pass on both characters.
        halfWrong:   _speechClose('再見', '你好'),
        tones:       _stripTones('nei5 hou2') === 'nei hou',
      });
    """, fns=_SPEECH_FNS)
    assert res["exact"] and res["punctuation"] and res["spacing"] and res["casing"]
    assert res["embedded"] and res["homophone"] and res["tones"]
    assert not res["wrong"] and not res["empty"] and not res["halfWrong"]


@needs_node
def test_leaving_the_drill_screens_stops_the_audio():
    """A prompt that follows you out to the course map reads as the app talking
    to itself — and with the volume boost it was worse: clips routed into a
    not-yet-running audio context all arrived at once when it woke."""
    src = open(LEARN_JS, encoding="utf-8").read()
    body = "\n".join(_extract_decl(src, n) for n in ("_ttsCache",))
    body += "\n" + "\n".join(_extract(src, d) for d in
                             ("function _ttsKey(", "function _ttsUrl(",
                              "function _makeTTSAudio(", "function stopTTS("))
    script = """
    const el = new Audio(_ttsUrl('hi', 'yue'));
    el.paused = false;
    _audio = el;
    stopTTS();
    done({ paused: el.paused, cleared: _audio === null });
    """
    harness = """
    const built = [];
    let _audio = null;
    class Audio {
      constructor(url) { this.src = url; this.paused = true; this.currentTime = 5;
                         this._handlers = {}; built.push(this); }
      addEventListener(ev, fn) { (this._handlers[ev] = this._handlers[ev] || []).push(fn); }
      pause() { this.paused = true; }
    }
    function _setTTSHealth() {}
    function done(o) { console.log(JSON.stringify(o)); process.exit(0); }
    """
    out = subprocess.run(["node", "-e", harness + body + script],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res == {"paused": True, "cleared": True}
