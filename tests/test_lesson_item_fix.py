"""Two ways a lesson stops being a dead end.

  * "Correct — that works too!" used to be the whole reply: the learner was told
    their sentence worked, but never shown the one the lesson had in mind, so
    they learned nothing about the alternative they didn't write. The expected
    answer now sits beside the verdict, and the judge is asked to say what the
    difference actually is.
  * A lesson is authored in ONE call, so a single bad drill (an answer that
    isn't right, two options that both are, a cloze two words fit) left the
    learner replaying a lesson they knew was broken or deleting the whole thing.
    Now they can point at that item, optionally say what's wrong, and have just
    that item re-authored — through the same deterministic assembly as the
    original, so the replacement's answer key is still correct by construction.
"""
import json
import os
import shutil
import subprocess
import time

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DB_PATH", "/tmp/test_lesson_item_fix.db")
os.environ.setdefault("API_KEY_ENC_KEY", Fernet.generate_key().decode())

import auth
import db
import learning
import llm
import main
import tutor

LEARN_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "pages", "learn.js",
)


# ── The judge is asked to CONTRAST, not just to approve ──────────────────────

def test_judge_prompt_asks_what_differs_from_the_expected_answer():
    prompt = tutor.build_typed_answer_judge_prompt(
        "fr", "I eat bread", "Je mange du pain", "Je prends du pain",
        is_cloze=False, accept=[], level="A1",
    )
    low = prompt.lower()
    assert "differ" in low                      # explain the difference…
    assert "Je mange du pain" in prompt         # …against THIS answer specifically
    # The learner is shown the expected answer, so the note must not just say ok.
    assert "note" in low


# ── Re-authoring one item ────────────────────────────────────────────────────

CONCEPTS = [{"key": "er_present", "kind": "grammar", "label": "-er present",
             "gloss": "regular -er verbs"}]


def _stub_llm(monkeypatch, payload):
    """Stand in for the model, capturing the prompt it was given."""
    seen = {}

    async def fake_call(prompt, **kw):
        seen["prompt"] = prompt
        seen["model"] = kw.get("model")
        return payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(llm, "call", fake_call)
    return seen


@pytest.mark.asyncio
async def test_regenerating_a_drill_reassembles_it(monkeypatch):
    """The replacement goes through _assemble_drill like any authored drill, so
    we still own the answer key — the model never returns an index."""
    _stub_llm(monkeypatch, {"drill": {
        "kind": "production", "concept": "er_present", "tier": "core",
        "gloss": "we speak", "target": "nous parlons",
        "distractors": ["nous parlez", "nous parlent"],
    }})
    ex = await learning.regenerate_item(
        "fr", kind="drill", current={"type": "choice", "concept_key": "er_present"},
        concepts=CONCEPTS, api_key="k",
    )
    assert ex["type"] == "choice"
    assert ex["options"][ex["answer"]] == "nous parlons"
    assert ex["concept_key"] == "er_present"
    assert ex["tier"] == "core"


@pytest.mark.asyncio
async def test_a_replacement_that_fails_validation_is_rejected(monkeypatch):
    """Same contract as authoring: a drill we can't key is dropped, not shipped.
    Returning None leaves the (bad) original in place, which the caller reports —
    better than swapping in something worse."""
    _stub_llm(monkeypatch, {"drill": {
        "kind": "production", "concept": "er_present",
        "gloss": "we speak", "target": "nous parlons",
        "distractors": ["nous parlons", " Nous Parlons "],   # all == the answer
    }})
    assert await learning.regenerate_item(
        "fr", kind="drill", current={"type": "choice"}, concepts=CONCEPTS,
        api_key="k") is None


@pytest.mark.asyncio
async def test_a_drill_keeps_its_concept_when_the_model_forgets_it(monkeypatch):
    """Mastery is recorded per concept — an unkeyed replacement would silently
    stop counting toward the skill it practises."""
    _stub_llm(monkeypatch, {"drill": {
        "kind": "recognition", "target": "parler", "gloss": "to speak",
        "distractors": ["to eat", "to drink"],
    }})
    ex = await learning.regenerate_item(
        "fr", kind="drill", current={"type": "choice", "concept_key": "er_present",
                                     "tier": "extra"},
        concepts=CONCEPTS, api_key="k")
    assert ex["concept_key"] == "er_present"
    assert ex["tier"] == "extra"          # inherited from the item it replaces


@pytest.mark.asyncio
async def test_regenerating_a_teach_block(monkeypatch):
    _stub_llm(monkeypatch, {"block": {
        "type": "note", "text": "Drop the -er and add -ons for nous."}})
    block = await learning.regenerate_item(
        "fr", kind="teach", current={"type": "prose", "text": "old"},
        concepts=CONCEPTS, api_key="k")
    assert block["type"] == "note"
    assert "nous" in block["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["sorry, I can't do that", "[1, 2]", "{}"])
async def test_unusable_model_output_returns_none(monkeypatch, reply):
    """A model that answers in prose, or with the wrong shape, must leave the
    original item in place rather than blow up mid-lesson."""
    _stub_llm(monkeypatch, reply)
    assert await learning.regenerate_item(
        "fr", kind="drill", current={"type": "choice"}, concepts=CONCEPTS,
        api_key="k") is None


@pytest.mark.asyncio
async def test_the_learners_note_reaches_the_model(monkeypatch):
    seen = _stub_llm(monkeypatch, {"drill": {
        "kind": "recognition", "concept": "er_present", "target": "parler",
        "gloss": "to speak", "distractors": ["to eat"]}})
    await learning.regenerate_item(
        "fr", kind="drill", current={"type": "choice", "concept_key": "er_present"},
        concepts=CONCEPTS, title="Talking about routines",
        note="two of the options mean the same thing", api_key="k")
    assert "two of the options mean the same thing" in seen["prompt"]
    assert "Talking about routines" in seen["prompt"]
    # It must see the item it is replacing, or it can only guess.
    assert "choice" in seen["prompt"]


@pytest.mark.asyncio
async def test_a_missing_note_still_gets_a_useful_brief(monkeypatch):
    """Most reports will be a bare tap. The prompt has to carry the checklist."""
    seen = _stub_llm(monkeypatch, {"drill": {
        "kind": "recognition", "concept": "er_present", "target": "parler",
        "gloss": "to speak", "distractors": ["to eat"]}})
    await learning.regenerate_item(
        "fr", kind="drill", current={"type": "choice"}, concepts=CONCEPTS,
        api_key="k")
    assert "didn't say why" in seen["prompt"]


# ── Route ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_rate_limit():
    original = main.limiter.enabled
    main.limiter.enabled = False
    yield
    main.limiter.enabled = original
    main.limiter._storage.reset()


LESSON_CONTENT = {
    "segments": [{
        "title": "Step 1",
        "teach": {"intro": "", "blocks": [{"type": "prose", "text": "old rule"}]},
        "exercises": [
            {"type": "choice", "concept_key": "er_present", "prompt": "we speak",
             "prompt_lang": "english", "options": ["nous parlons", "nous parlez"],
             "answer": 0, "tier": "core"},
            {"type": "construction_drill", "construction": "-er present"},
        ],
    }],
}


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    user_id = await db.create_user("fixer", auth.hash_password("password1"))
    session = "lesson-fix-test-session"
    await db.create_session(session, user_id, time.time() + 3600)

    class _Access:
        api_key = "test-key"
        anthropic_key = None
        model_reader = "gemini-2.5-flash-lite"

    async def _access(_user, **_kw):
        return _Access()
    monkeypatch.setattr(main, "_resolve_gemini", _access)

    course_id = await db.create_course(user_id, "fr", "A1")
    lesson_id = await db.create_lesson(
        course_id, 1, "Talking about routines", "Use -er verbs",
        CONCEPTS, LESSON_CONTENT, "-er verbs in the present")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as ac:
        ac.cookies.set("session", session)
        ac._user_id = user_id
        ac._lesson_id = lesson_id
        yield ac


@pytest.mark.asyncio
async def test_regenerating_persists_the_fix(client, monkeypatch):
    """The point of persisting: the learner never meets the broken drill again,
    in this run or any replay."""
    async def fake(*a, **kw):
        return {"type": "choice", "concept_key": "er_present", "prompt": "we speak",
                "prompt_lang": "english", "options": ["nous parlons", "nous mangeons"],
                "answer": 0, "tier": "core"}
    monkeypatch.setattr(learning, "regenerate_item", fake)

    res = await client.post(f"/api/lessons/{client._lesson_id}/regenerate-item",
                            json={"segment": 0, "index": 0, "kind": "drill",
                                  "note": "both options are right"})
    assert res.status_code == 200, res.text
    assert res.json()["item"]["options"][1] == "nous mangeons"

    stored = await db.get_lesson(client._user_id, client._lesson_id)
    ex = stored["content"]["segments"][0]["exercises"][0]
    assert ex["options"][1] == "nous mangeons"
    # Everything else in the lesson is untouched.
    assert stored["content"]["segments"][0]["exercises"][1]["type"] == "construction_drill"
    assert stored["content"]["segments"][0]["teach"]["blocks"][0]["text"] == "old rule"


@pytest.mark.asyncio
async def test_regenerating_a_teach_block_through_the_route(client, monkeypatch):
    async def fake(*a, **kw):
        return {"type": "note", "text": "new rule"}
    monkeypatch.setattr(learning, "regenerate_item", fake)

    res = await client.post(f"/api/lessons/{client._lesson_id}/regenerate-item",
                            json={"segment": 0, "index": 0, "kind": "teach"})
    assert res.status_code == 200, res.text
    stored = await db.get_lesson(client._user_id, client._lesson_id)
    assert stored["content"]["segments"][0]["teach"]["blocks"][0]["text"] == "new rule"
    # The drills are left alone.
    assert stored["content"]["segments"][0]["exercises"][0]["answer"] == 0


@pytest.mark.asyncio
async def test_a_live_generated_drill_cannot_be_rewritten(client):
    """The AI Speak drill is generated turn by turn — there is nothing stored to
    rewrite, so say so instead of burning a call on it."""
    res = await client.post(f"/api/lessons/{client._lesson_id}/regenerate-item",
                            json={"segment": 0, "index": 1, "kind": "drill"})
    assert res.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"segment": 9, "index": 0, "kind": "drill"},
    {"segment": 0, "index": 9, "kind": "drill"},
    {"segment": 0, "index": 0, "kind": "nonsense"},
])
async def test_bad_targets_are_rejected(client, body):
    res = await client.post(f"/api/lessons/{client._lesson_id}/regenerate-item", json=body)
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_another_users_lesson_is_not_reachable(client):
    res = await client.post("/api/lessons/999999/regenerate-item",
                            json={"segment": 0, "index": 0, "kind": "drill"})
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_a_failed_rewrite_leaves_the_lesson_untouched(client, monkeypatch):
    async def fake(*a, **kw):
        return None
    monkeypatch.setattr(learning, "regenerate_item", fake)

    res = await client.post(f"/api/lessons/{client._lesson_id}/regenerate-item",
                            json={"segment": 0, "index": 0, "kind": "drill"})
    assert res.status_code == 502
    stored = await db.get_lesson(client._user_id, client._lesson_id)
    assert stored["content"]["segments"][0]["exercises"][0]["options"][1] == "nous parlez"


# ── Client: the "that works too" feedback ────────────────────────────────────

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available to run the player code")


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


@needs_node
def test_correct_feedback_shows_the_lessons_own_answer():
    src = open(LEARN_JS, encoding="utf-8").read()
    body = "\n".join(_extract(src, d) for d in
                     ("function _correctHead(", "function _expectedLine("))
    script = """
    const exp = '<span>Je mange du pain</span>';
    done({
      // Matched the accept-set offline: nothing to compare, nothing to add.
      exactHead: _correctHead({ exact: true }),
      exactLine: _expectedLine({ exact: true }, exp, false),
      // Judged as a valid variant: praise AND the answer they didn't write.
      variantHead: _correctHead({ exact: false }),
      variantLine: _expectedLine({ exact: false }, exp, false),
      // The "Better:" line is already that answer — don't print it twice.
      dupLine: _expectedLine({ exact: false }, exp, true),
      noExpected: _expectedLine({ exact: false }, '', false),
    });
    """
    harness = "function done(o){console.log(JSON.stringify(o));process.exit(0);}\n"
    out = subprocess.run(["node", "-e", harness + body + script],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["exactHead"] == "Correct!"
    assert res["exactLine"] == ""
    assert res["variantHead"] == "Correct — that works too!"
    assert "Je mange du pain" in res["variantLine"]
    assert res["dupLine"] == ""
    assert res["noExpected"] == ""


# ── Stored lessons heal themselves ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_english_drill_in_a_stored_lesson_is_stripped_and_the_fix_saved(client):
    """Lessons authored before the language check existed can hold a drill
    written entirely in English. Opening one drops it — and PERSISTS the drop,
    because the ⚑ regenerate flow addresses stored items by position, so serving
    a different list than we store would rewrite the wrong drill."""
    course_id = await db.create_course(client._user_id, "yue", "A1")
    good = {"type": "choice", "concept_key": "k", "prompt": "thank you",
            "prompt_lang": "english", "options": ["唔該", "早晨"], "answer": 0}
    english_only = {"type": "choice", "concept_key": "k",
                    "prompt": "Please include some coins in the change.",
                    "prompt_lang": "target",
                    "options": ["Please include some coins in the change.",
                                "I have no coins."], "answer": 0}
    lesson_id = await db.create_lesson(
        course_id, 1, "Asking for change", "Ask for coins",
        [{"kind": "vocab", "key": "k", "label": "唔該", "gloss": "thank you"}],
        {"segments": [{"teach": {"intro": "", "blocks": []},
                       "exercises": [good, english_only]}]},
        "asking for change")

    res = await client.get(f"/api/lessons/{lesson_id}")
    assert res.status_code == 200, res.text
    served = res.json()["content"]["segments"][0]["exercises"]
    assert len(served) == 1 and served[0]["options"] == ["唔該", "早晨"]

    stored = await db.get_lesson(client._user_id, lesson_id)
    assert len(stored["content"]["segments"][0]["exercises"]) == 1


@pytest.mark.asyncio
async def test_a_healthy_lesson_is_served_untouched(client):
    """The repair must not rewrite lessons that are fine — every open would be a
    pointless write, and a French lesson is indistinguishable from English by
    script, so nothing may be dropped on that basis."""
    stored_before = await db.get_lesson(client._user_id, client._lesson_id)
    res = await client.get(f"/api/lessons/{client._lesson_id}")
    assert res.status_code == 200
    exercises = res.json()["content"]["segments"][0]["exercises"]
    assert len(exercises) == len(stored_before["content"]["segments"][0]["exercises"])


# ── Textbook lessons must be resumable ───────────────────────────────────────

@needs_node
def test_a_textbook_lesson_row_opens_the_intro_sheet():
    """The intro sheet is the ONLY place "▶ Resume" is offered, so a lesson row
    that called openLesson() directly always restarted from the beginning — the
    resume snapshot was written on every quit and never read. Foundations rows
    stay direct: two-minute lessons the sheet's options don't apply to."""
    src = open(LEARN_JS, encoding="utf-8").read()
    body = "\n".join(_extract(src, d) for d in
                     ("function esc(", "function _lessonHtml("))
    harness = """
    const unlockAll = false, isAdmin = false;
    let resumeFor = null;
    function _loadResume(id) { return id === resumeFor ? { v: 1 } : null; }
    const document = { createElement: () => ({
      set textContent(v) { this._t = v == null ? '' : String(v); },
      get innerHTML() { return (this._t || '').replace(/&/g, '&amp;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }) };
    function done(o) { console.log(JSON.stringify(o)); process.exit(0); }
    """
    script = """
    const lesson = { id: 7, title: 'Unit 1 · Greetings', status: 'available' };
    const plain = _lessonHtml(lesson, 0, { deletable: true });
    const textbook = _lessonHtml(lesson, 0, { deletable: true, intro: true });
    resumeFor = 7;
    const withResume = _lessonHtml(lesson, 0, { intro: true });
    const noResumeHint = _lessonHtml(lesson, 0, {});     // foundations row
    done({
      plainOpensDirectly: plain.includes('openLesson(7)') && !plain.includes('openLessonIntro'),
      textbookOpensSheet: textbook.includes('openLessonIntro(7)'),
      showsResume: withResume.includes('Resume where you left off'),
      foundationsNoResume: !noResumeHint.includes('Resume where you left off'),
    });
    """
    out = subprocess.run(["node", "-e", harness + body + script],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["plainOpensDirectly"] is True
    assert res["textbookOpensSheet"] is True
    assert res["showsResume"] is True
    assert res["foundationsNoResume"] is True


@needs_node
def test_a_textbook_chapter_collapses_and_remembers():
    """A unit can hold a dozen lessons plus its build actions, and a book has
    many chapters — open at once they buried the page."""
    src = open(LEARN_JS, encoding="utf-8").read()
    body = "\n".join(_extract(src, d) for d in
                     ("function esc(", "function _tbChapterShell("))
    harness = """
    const store = {};
    const localStorage = { getItem: k => (k in store ? store[k] : null),
                           setItem: (k, v) => { store[k] = v; } };
    const document = { createElement: () => ({
      set textContent(v) { this._t = v == null ? '' : String(v); },
      get innerHTML() { return (this._t || '').replace(/&/g, '&amp;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;'); } }) };
    function done(o) { console.log(JSON.stringify(o)); process.exit(0); }
    """
    script = """
    const shut = _tbChapterShell('u1', 'Unit 1', '3 of 5 built', '1/5', '', false, '<p>x</p>');
    const open = _tbChapterShell('u2', 'Unit 2', '', '2/2', 'tb-pill-done', true, '<p>y</p>');
    store['tbch:u1'] = '1';                       // the learner opened it
    const remembered = _tbChapterShell('u1', 'Unit 1', '', '1/5', '', false, '<p>x</p>');
    store['tbch:u2'] = '0';                       // …and closed the default-open one
    const overridden = _tbChapterShell('u2', 'Unit 2', '', '2/2', '', true, '<p>y</p>');
    done({
      shutClosed: !shut.includes('tb-chapter open'),
      currentOpen: open.includes('tb-chapter open'),
      remembered: remembered.includes('tb-chapter open'),
      overridden: !overridden.includes('tb-chapter open'),
      keepsBody: shut.includes('<p>x</p>'),      // collapsed, not dropped
    });
    """
    out = subprocess.run(["node", "-e", harness + body + script],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res == {"shutClosed": True, "currentOpen": True, "remembered": True,
                   "overridden": True, "keepsBody": True}
