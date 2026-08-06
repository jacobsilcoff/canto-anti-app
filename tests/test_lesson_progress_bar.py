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
    const fill = bar.match(/class="step-fill" style="width:(\\d+)%"/);
    const ticks = [...bar.matchAll(/class="step-tick" style="left:([\\d.]+)%"/g)]
      .map(m => +m[1]);
    console.log(JSON.stringify({{
      pct: fill ? +fill[1] : null, ticks, tag: els['step-tag'].textContent,
    }}));
    """
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _step(n_blocks=0, title="", speak=False):
    teach = {"blocks": [{"type": "prose", "text": f"b{i}"} for i in range(n_blocks)]} if n_blocks else None
    return {"title": title, "teach": teach, "speak": speak, "exercises": []}


def test_progress_is_proportional_to_the_work_each_step_holds():
    """A step with 8 drills must consume more of the bar than a step with 2 —
    otherwise the fill advances at wildly different rates through the lesson."""
    # 9 units then 3 units: the boundary tick sits at 9/12 = 75%.
    res = _run_bar([_step(1, "A"), _step(1, "B")], [8, 2], seg_idx=0, seg_answered=0)
    assert res["ticks"] == [75.0]


def test_finishing_the_first_step_fills_exactly_its_share():
    """Arriving at step 2's TEACH screen means exactly step 1 is behind you."""
    res = _run_bar([_step(1, "A"), _step(1, "B")], [8, 2],
                   seg_idx=1, seg_answered=0, state="teach", teach_idx=0)
    assert res["pct"] == 75          # 9 of 12 units

    # Once past that teach card and into step 2's drills, it counts too.
    drilling = _run_bar([_step(1, "A"), _step(1, "B")], [8, 2],
                        seg_idx=1, seg_answered=0, state="player")
    assert drilling["pct"] == 83     # 10 of 12


def test_teach_cards_count_toward_progress():
    """Paging through 4 teach cards used to move the bar not at all."""
    seg = _step(4, "Grammar")
    at_start = _run_bar([seg], [4], seg_idx=0, seg_answered=0,
                        state="teach", teach_idx=0)
    mid_teach = _run_bar([seg], [4], seg_idx=0, seg_answered=0,
                         state="teach", teach_idx=2)
    assert at_start["pct"] == 0
    # 2 of 8 units (4 teach cards + 4 drills) done.
    assert mid_teach["pct"] == 25


def test_drills_continue_from_where_teach_left_off():
    """Once drilling, every teach card is behind the learner — the bar must not
    reset when the exercises start."""
    res = _run_bar([_step(4, "Grammar")], [4], seg_idx=0, seg_answered=2, state="player")
    assert res["pct"] == 75          # 4 teach cards + 2 drills of 8


def test_the_bar_only_ever_moves_forward():
    """Walk a whole two-step lesson and assert the fill is monotonic — this is
    the property the segmented pills could not offer."""
    segs = [_step(2, "A"), _step(1, "B", speak=True)]
    totals = [3, 1]                  # step A: 2 teach + 3 drills; step B: 1 teach + 1 drill
    seen = []
    for t in (0, 1, 2):              # paging step A's teach cards
        seen.append(_run_bar(segs, totals, seg_idx=0, seg_answered=0,
                             state="teach", teach_idx=t)["pct"])
    for a in (0, 1, 2, 3):           # answering step A's drills
        seen.append(_run_bar(segs, totals, seg_idx=0, seg_answered=a)["pct"])
    for a in (0, 1):                 # step B
        seen.append(_run_bar(segs, totals, seg_idx=1, seg_answered=a)["pct"])
    assert seen == sorted(seen), seen
    assert seen[0] == 0 and seen[-1] == 100


def test_a_step_with_nothing_left_to_do_is_not_counted():
    """_trimForLength can strip every drill from a step. It must not reserve a
    slice of the bar that nothing will ever fill."""
    res = _run_bar([_step(1, "A"), _step(0, "Emptied"), _step(1, "C")],
                   [3, 0, 3], seg_idx=0, seg_answered=0)
    assert res["ticks"] == [50.0]    # two visible steps of equal weight


def test_step_numbering_counts_only_visible_steps():
    """'Step 3 of 3' with two ticks-worth of lesson is its own kind of wrong."""
    res = _run_bar([_step(1, "A"), _step(0, "Emptied"), _step(1, "C")],
                   [3, 0, 3], seg_idx=2, seg_answered=0)
    assert res["tag"].startswith("Step 2 of 2")


def test_mistake_review_fills_everything():
    res = _run_bar([_step(1, "A"), _step(1, "B")], [2, 2],
                   seg_idx=0, seg_answered=0, review=True)
    assert res["pct"] == 100


def test_progress_never_exceeds_full():
    """The end-of-lesson lap can push segAnswered past the step's own total."""
    res = _run_bar([_step(1, "A")], [2], seg_idx=0, seg_answered=99)
    assert res["pct"] == 100


def test_foundations_teach_is_one_card_not_a_pager():
    """The reading track renders its teach as a single scroll page, so the bar
    must count it as one unit however many blocks it holds."""
    res = _run_bar([_step(5, "Letters"), _step(0, "B")], [3, 4],
                   seg_idx=0, seg_answered=0, theme="foundations")
    # step 1 = 1 teach page + 3 drills = 4 of 8 total → tick at 50%.
    assert res["ticks"] == [50.0]


def test_no_tick_is_drawn_at_the_end_of_the_bar():
    res = _run_bar([_step(1, "A"), _step(1, "B")], [1, 1], seg_idx=0, seg_answered=0)
    assert res["ticks"] == [50.0]    # one boundary between two steps, not two
