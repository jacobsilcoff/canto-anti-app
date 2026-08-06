"""The lesson step bar must reflect how much work is actually left.

It used to render one equal-width pill per step and fill it from drill answers
alone, which made it wrong in three ways at once: a step holding 8 drills crawled
while a step holding 1 jumped, paging through teach cards moved nothing, and a
step whose drills were all trimmed away for the chosen lesson length still drew a
pill that could never fill.

These tests run the REAL functions out of static/pages/learn.js under node with a
stubbed DOM, so they check the shipped source rather than a transcription of it.
"""
import json
import os
import shutil
import subprocess

import pytest

LEARN_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "pages", "learn.js",
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available to run the player code")


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


def _run_bar(segments, seg_totals, *, seg_idx, seg_answered,
             state="player", teach_idx=0, review=False, theme=""):
    """Execute the real updateBar against a stub DOM; return the pills it drew."""
    src = open(LEARN_JS, encoding="utf-8").read()
    fns = "\n".join(_extract(src, d) for d in
                    ("function _teachCards(", "function _segWeight(", "function updateBar("))

    harness = f"""
    const player = {{
      segments: {json.dumps(segments)},
      segTotals: {json.dumps(seg_totals)},
      segIdx: {seg_idx}, segAnswered: {seg_answered},
      teachIdx: {teach_idx}, reviewStarted: {json.dumps(review)},
      theme: {json.dumps(theme)},
    }};
    const _currentState = {json.dumps(state)};
    const els = {{}};
    function document_getElementById(id) {{
      if (!els[id]) els[id] = {{ innerHTML: '', textContent: '', style: {{}},
                                 classList: {{ toggle() {{}} }} }};
      return els[id];
    }}
    const document = {{ getElementById: document_getElementById }};
    {fns}
    updateBar();
    const bar = els['player-stepbar'].innerHTML;
    const pills = [...bar.matchAll(/flex-grow:(\\d+)[^]*?width:(\\d+)%/g)]
      .map(m => ({{ weight: +m[1], pct: +m[2] }}));
    console.log(JSON.stringify({{ pills, tag: els['step-tag'].textContent }}));
    """
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _step(n_blocks=0, title="", speak=False):
    teach = {"blocks": [{"type": "prose", "text": f"b{i}"} for i in range(n_blocks)]} if n_blocks else None
    return {"title": title, "teach": teach, "speak": speak, "exercises": []}


def test_pills_are_weighted_by_the_work_they_hold():
    """A step with 8 drills must be wider than a step with 2 — otherwise the bar
    advances at wildly different rates through equal-looking pills."""
    res = _run_bar([_step(1, "A"), _step(1, "B")], [8, 2], seg_idx=0, seg_answered=0)
    weights = [p["weight"] for p in res["pills"]]
    assert weights == [9, 3]          # 1 teach card + drills, each step


def test_teach_cards_count_toward_progress():
    """Paging through 4 teach cards used to move the bar not at all."""
    seg = _step(4, "Grammar")
    at_start = _run_bar([seg], [4], seg_idx=0, seg_answered=0,
                        state="teach", teach_idx=0)
    mid_teach = _run_bar([seg], [4], seg_idx=0, seg_answered=0,
                         state="teach", teach_idx=2)
    assert at_start["pills"][0]["pct"] == 0
    # 2 of 8 units (4 teach cards + 4 drills) done.
    assert mid_teach["pills"][0]["pct"] == 25


def test_drills_continue_from_where_teach_left_off():
    """Once drilling, every teach card is behind the learner — the bar must not
    reset to zero when the exercises start."""
    seg = _step(4, "Grammar")
    res = _run_bar([seg], [4], seg_idx=0, seg_answered=2, state="player")
    # 4 teach cards + 2 drills = 6 of 8.
    assert res["pills"][0]["pct"] == 75


def test_a_step_with_nothing_left_to_do_is_not_drawn():
    """_trimForLength can strip every drill from a step. Drawing a pill that can
    never fill is worse than not drawing it."""
    res = _run_bar([_step(1, "A"), _step(0, "Emptied"), _step(1, "C")],
                   [3, 0, 3], seg_idx=0, seg_answered=0)
    assert len(res["pills"]) == 2


def test_step_numbering_counts_only_visible_steps():
    """'Step 2 of 3' next to two pills is its own kind of wrong."""
    res = _run_bar([_step(1, "A"), _step(0, "Emptied"), _step(1, "C")],
                   [3, 0, 3], seg_idx=2, seg_answered=0)
    assert res["tag"].startswith("Step 2 of 2")


def test_past_steps_are_full_and_future_steps_empty():
    res = _run_bar([_step(1, "A"), _step(1, "B"), _step(1, "C")],
                   [2, 2, 2], seg_idx=1, seg_answered=0)
    assert [p["pct"] for p in res["pills"]] == [100, 33, 0]


def test_mistake_review_fills_everything():
    res = _run_bar([_step(1, "A"), _step(1, "B")], [2, 2],
                   seg_idx=0, seg_answered=0, review=True)
    assert [p["pct"] for p in res["pills"]] == [100, 100]


def test_progress_never_exceeds_full():
    """The end-of-lesson lap can push segAnswered past the step's own total."""
    res = _run_bar([_step(1, "A")], [2], seg_idx=0, seg_answered=99)
    assert res["pills"][0]["pct"] == 100


def test_foundations_teach_is_one_card_not_a_pager():
    """The reading track renders its teach as a single scroll page, so the bar
    must count it as one unit however many blocks it holds."""
    res = _run_bar([_step(5, "Letters")], [3], seg_idx=0, seg_answered=0,
                   theme="foundations")
    assert res["pills"][0]["weight"] == 4      # 1 teach page + 3 drills
