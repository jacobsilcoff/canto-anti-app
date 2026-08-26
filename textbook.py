"""Textbook PDF → reviewable source selection → interactive unit.

Pipeline (staged, so the user can verify the parse before spending LLM calls):

1. UPLOAD — the route extracts the PDF **per page** (extract.extract_pdf_pages,
   deterministic), cleans each page (`clean_page_text` collapses the 4×
   styled-text-layer artifact some PDFs produce; `strip_repeated_lines` drops
   running headers/footers/watermarks), and stores the book in the `textbooks`
   table. One cheap LLM call over a heading SKELETON of the book
   (`detect_chapters`) proposes chapters as PAGE RANGES.
2. REVIEW — "Add lesson → From a textbook" lets the user choose a detected
   section or a custom page range, inspect and edit ALL extracted source text,
   approve useful extracted images/diagrams, and add an optional instruction.
   Detected ranges remain editable and become the one-to-one app unit boundary.
   A contact sheet of selected visuals is included in source planning.
3. CREATE ONE UNIT — `plan_source_unit` inventories every teachable concept in
   the approved chapter, maps each concept to exactly one lesson, and verifies
   that each lesson carries verbatim excerpts from the approved source. The
   normal lesson author then builds the unit's lessons from those grounded specs.

The approved unit's grounded specs are put at the front of `lesson_queue` so
`main._author_next_lesson` can reuse established authoring, validation, and
course bookkeeping. The first lesson is authored immediately and the rest are
consumed just in time. `segment_chapter` remains for older imports/API clients.

All LLM output is strictly normalized here (same trust model as the planner):
malformed chapters/units/lessons are dropped, ranges are clamped, counts are
capped, and keys are synthesized downstream by main._concepts_from_spec when
missing.
"""
import asyncio
import re
import time

import llm
from translation import LANG_INFO, _parse_json
from learning import _lang_preamble

# A chapter is consumed as lesson SPECS, so we can afford a lot of text — but
# still bound it (~60k ≈ 25–40 book pages of prose; chapters are usually far
# smaller). The whole book no longer needs a global char cap: it's stored
# per-page and segmented chapter by chapter.
MAX_CHAPTER_CHARS = 60_000
CHUNK_CHARS = 9_000               # one segmentation LLM call per chunk
MAX_LESSONS_PER_CHAPTER = 12      # queue cap per chapter generation
MAX_LESSON_SOURCE_PAGES = 20      # one reviewed source selection
MAX_LESSON_SOURCE_CHARS = 24_000  # keep planner + author prompts responsive
MAX_ITEMS_PER_LESSON = 10
MAX_VOCAB_ITEMS = 80             # cap per chapter vocab-deck extraction
MAX_CHAPTERS = 60                 # structure-detection cap
_SOURCE_CAP = 4000                # per-lesson textbook grounding
_TITLE_CAP = 80
_SKELETON_CAP = 16_000            # chapter-detection prompt budget
_ANCHOR_CAP = 240                 # stored mid-page split marker (one line of text)


# ── Deterministic page cleanup ────────────────────────────────────────────────

# Some PDFs render styled/bold text as several overlapping layers, so extraction
# emits the same phrase repeated back to back — sometimes 3–5× ("jóu sàhnjóu
# sàhnjóu sàhnjóu sàhn"), but very often just DOUBLED, either with no separator
# ("jóu sàhnjóu sàhn") or as two identical space-separated halves ("jóu sàhnjóu
# sàhn jóu sàhnjóu sàhn"). All three are collapsed. This text feeds an LLM (and
# the source-review UI), not the learner's screen, and collapsing it is also what
# lets the unit planner's verbatim excerpt check match the book's clean wording.
#
# Short units (≤5 chars) collapse only at 4×+ repetition, so ordinary doublings
# ("bye bye", "couscous") survive; longer units (≥6 chars, i.e. whole phrases)
# collapse from a single doubling. Digit-only units are kept ("10000" must not
# become "10"); whitespace runs are left to clean_text.
_ARTIFACT_SHORT_RE = re.compile(r"(.{1,5}?)\1{3,}")   # short unit: needs 4×+
_ARTIFACT_LONG_RE = re.compile(r"(.{6,80}?)\1{1,}")   # long unit: 2×+ collapses


def _artifact_repl(m: "re.Match") -> str:
    unit = m.group(1)
    if unit.isdigit() or unit.isspace():
        return m.group(0)
    return unit


def _collapse_line_halves(line: str) -> str:
    """Collapse a whole line that is two identical halves (with an optional single
    space between them) into one — the space-separated ``X X`` layer artifact the
    doubled-substring regex can't reach across the gap."""
    s = line.strip()
    n = len(s)
    if n < 10:
        return line
    if n % 2 == 0:
        left, right = s[: n // 2], s[n // 2:]
    elif s[n // 2] == " ":
        left, right = s[: n // 2], s[n // 2 + 1:]
    else:
        return line
    if left == right and len(left) >= 5 and any(c.isalpha() for c in left):
        lead = line[: len(line) - len(line.lstrip())]
        trail = line[len(line.rstrip()):]
        return lead + left + trail
    return line


def clean_page_text(text: str) -> str:
    """Collapse the repeated-text-layer extraction artifact within one page."""
    out = []
    for line in (text or "").splitlines():
        prev = None
        # Fixed-point: nested/layered repeats collapse layer by layer
        # ("jóu sàhnjóu sàhn jóu sàhnjóu sàhn" → … → "jóu sàhn").
        for _ in range(8):
            if prev == line:
                break
            prev = line
            # Short before long: the long pass would otherwise collapse a 4×
            # short-unit run down to 2× before the short pass can finish it.
            line = _ARTIFACT_SHORT_RE.sub(_artifact_repl, line)
            line = _ARTIFACT_LONG_RE.sub(_artifact_repl, line)
            line = _collapse_line_halves(line)
        out.append(line)
    return "\n".join(out)


# A page holding at least this much text is never stripped down to nothing (see
# the salvage branch below) — losing a whole page of source is worse than leaving
# a running header on it.
_MIN_PAGE_CHARS_TO_PROTECT = 120


def strip_repeated_lines(pages: list[str]) -> list[str]:
    """Drop running headers/footers/watermarks: shortish lines whose normalized
    form (digits masked) recurs on a large share of the book's pages."""
    n_nonempty = sum(1 for p in pages if p.strip())
    if n_nonempty < 8:            # too few pages to call anything "recurring"
        return pages
    threshold = max(4, int(n_nonempty * 0.3))

    def norm(line: str) -> str:
        s = re.sub(r"\d+", "#", line).casefold()
        return re.sub(r"\s+", " ", s).strip()

    counts: dict[str, int] = {}
    for p in pages:
        seen = set()
        for line in p.splitlines():
            key = norm(line)
            if key and len(key) <= 80 and key not in seen:
                seen.add(key)
                counts[key] = counts.get(key, 0) + 1

    boiler = {k for k, c in counts.items() if c >= threshold}

    def keep(line: str) -> bool:
        key = norm(line)
        if key in boiler:
            return False
        # Bare page numbers ("9", "· 23 ·") are never teachable text.
        return not re.fullmatch(r"[\W#]*", key)

    def numbered_only(line: str) -> bool:
        return bool(re.fullmatch(r"[\W#]*", norm(line)))

    out = []
    for p in pages:
        lines = p.splitlines()
        kept = [l for l in lines if keep(l)]
        if not kept:
            # Every line looked like a running header. On a page with real
            # content that's a false positive of the digit masking (numbered
            # drills, a repeated table scaffold), and emptying the page silently
            # destroys the source every downstream planner reads. Keep it whole
            # apart from bare page numbers; a genuinely header-only page has too
            # little text to qualify and still cleans out.
            salvage = [l for l in lines if not numbered_only(l)]
            if sum(len(l.strip()) for l in salvage) >= _MIN_PAGE_CHARS_TO_PROTECT:
                kept = salvage
        out.append("\n".join(kept))
    return out


def clean_pages(pages: list[str]) -> list[str]:
    """The full deterministic cleanup applied once at upload."""
    return strip_repeated_lines([clean_page_text(p) for p in pages])


# ── Vision re-extraction (render page → transcribe with the model) ────────────
#
# Deterministic pypdf text mangles the books this feature targets: 2-up spreads
# interleave two logical pages, interlinear layouts scramble the romanization ↔
# gloss alignment, and romanization-only phrasebooks carry no native script at
# all (scanned pages carry no text layer whatsoever). For those, we render the
# page to an image and let the vision model transcribe it into clean, linear,
# native-script text that the existing chapter/lesson planners can consume.

MAX_VISION_PAGES = MAX_LESSON_SOURCE_PAGES   # bound one re-read request (≤20 pp.)
_VISION_PAGE_CHARS = 8000                    # clamp one page's transcript


def _build_transcription_prompt(target_lang: str) -> str:
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    return (
        f"You are transcribing ONE page of a {name} learning book (textbook, "
        f"phrasebook, or grammar) for a learner. Read the page image and output "
        f"its teachable content as clean, plain text.\n\n"
        f"{_lang_preamble(info)}"
        "── RULES ──\n"
        "• Transcribe in natural READING ORDER. If the page is laid out in two "
        "columns or as a two-page spread, read the whole left page top-to-bottom, "
        "then the whole right page — never interleave the columns.\n"
        f"• Preserve the native {name} script exactly as printed. Keep any "
        "romanization and English meaning next to the item it belongs to, on the "
        "same line, e.g. `native — romanization — English`.\n"
        f"• If an entry shows ONLY romanization (no native {name} script), add the "
        "correct native script yourself and keep the printed romanization beside "
        "it. Do not drop or invent vocabulary.\n"
        "• Keep the page's structure: unit/section headings, numbered items, and "
        "the pairing between a word/phrase and its meaning. One entry per line.\n"
        "• DROP page furniture: running headers/footers, page numbers, website "
        "URLs, watermarks, and purely decorative text.\n"
        "• Output ONLY the transcribed text — no commentary, no code fences, no "
        "notes about what you did. If the page has no teachable content, output an "
        "empty response.\n"
    )


async def transcribe_page_image(image_bytes: bytes, target_lang: str, *,
                                api_key: str, anthropic_key: str | None = None,
                                model: str) -> str:
    """Transcribe one rendered page image to clean text (best-effort).

    Reuses the deterministic page cleanup so the vision output goes through the
    same repeated-layer/whitespace normalization as pypdf text.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    prompt = _build_transcription_prompt(target_lang)
    raw = await llm.call_with_image(prompt, image_bytes, model=model,
                                    gemini_key=api_key, anthropic_key=anthropic_key)
    text = (raw or "").strip()
    # Strip an accidental ``` fence if the model added one despite instructions.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    return clean_page_text(text)[:_VISION_PAGE_CHARS]


async def transcribe_pages(images: dict[int, bytes], target_lang: str, *,
                           api_key: str, anthropic_key: str | None = None,
                           model: str) -> tuple[dict[int, str], int]:
    """Transcribe rendered page images → ``({page_no: text}, llm_calls)``.

    One vision call per page. Best-effort per page: a page that fails to
    transcribe is omitted from the result so the caller can keep its original
    extracted text. ``llm_calls`` counts only pages that were actually attempted
    (for usage metering).
    """
    out: dict[int, str] = {}
    calls = 0
    for page_no in sorted(images)[:MAX_VISION_PAGES]:
        calls += 1
        try:
            out[page_no] = await transcribe_page_image(
                images[page_no], target_lang, api_key=api_key,
                anthropic_key=anthropic_key, model=model)
        except Exception:
            # Leave this page's original extraction in place; keep going.
            continue
    return out, calls


# ── Chapter detection (1 LLM call over a heading skeleton) ────────────────────

_HEADING_RE = re.compile(
    r"^(unit|chapter|part|lesson|section|appendix)\b|^\d{1,2}[.)]\s", re.IGNORECASE)


def _heading_like(line: str) -> bool:
    if not (3 <= len(line) <= 60):
        return False
    if _HEADING_RE.search(line):
        return True
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.7


def build_skeleton(pages: list[str], lines_per_page: int = 3,
                   cap: int = _SKELETON_CAP) -> str:
    """A compact per-page outline (top lines + heading-looking lines) — enough
    for one LLM call to find the book's chapter boundaries in a 200-page PDF.
    Adaptive: if the outline overflows the prompt budget, retry with fewer
    lines per page rather than truncating (a cut tail would blind the detector
    to the whole back half of the book)."""
    sk = ""
    for lpp in range(lines_per_page, 0, -1):
        parts = []
        for i, p in enumerate(pages):
            lines = [l.strip() for l in p.splitlines() if l.strip()]
            if not lines:
                continue
            picked = lines[:lpp]
            picked += [l for l in lines[lpp:] if _heading_like(l)][:3]
            parts.append(f"[p.{i + 1}] " + " | ".join(l[:70] for l in picked))
        sk = "\n".join(parts)
        if len(sk) <= cap:
            return sk
    return sk[:cap]


def _build_chapter_prompt(target_lang: str, skeleton: str, num_pages: int) -> str:
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    return (
        f"Below is a per-page outline of a {num_pages}-page {name} textbook / "
        f"grammar book a learner uploaded ([p.N] = page N, showing that page's "
        f"top lines and heading-like lines). Split the book into CHAPTERS so our "
        f"app can turn each chapter into interactive lessons.\n\n"
        f"── RULES ──\n"
        f"• Follow the book's OWN structure (its units/chapters/sections) — one "
        f"chapter per book unit, typically 2–15 pages. Use the book's own titles, "
        f"shortened to plain English.\n"
        f"• Page ranges are 1-based and inclusive, must not overlap, and should "
        f"be in reading order. Small gaps are fine.\n"
        f"• SKIP non-teachable material: cover pages, table of contents, preface, "
        f"index, glossary, exercise ANSWER-KEY sections, publisher boilerplate.\n"
        f"• INCLUDE pronunciation/alphabet chapters only if they teach usable "
        f"content (our app teaches the script separately — mark such a chapter "
        f'with "skip_hint": true rather than omitting it).\n\n'
        f"── OUTLINE ──\n{skeleton}\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{"chapters": [\n'
        '  {"title": "<short English chapter title>", "start": <first page>, '
        '"end": <last page>, "skip_hint": false}\n'
        "]}"
    )


_CHAPTER_STATUSES = ("", "queued")     # "queued" = lessons generated already


def _clean_anchor(value) -> str:
    """A mid-page split marker is one verbatim line of the shared page. Collapse
    inner whitespace, strip, and cap — matching is line-based + whitespace
    tolerant, so we don't need to preserve the exact spacing."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:_ANCHOR_CAP]


def _norm_chapters(parsed: dict, num_pages: int) -> list[dict]:
    """Clamp/repair a chapter list (from the detector OR a user edit): ranges
    forced inside the book, ordered by start page, overlaps resolved in favour
    of the earlier chapter, empty/malformed entries dropped. A valid `status`
    and the user-controlled `lesson_enabled` flag survive (user edits round-trip
    through this too). Older saved chapters predate the flag and remain enabled.

    Mid-page splits: a chapter may carry `start_anchor`/`end_anchor` — verbatim
    lines marking where its content begins/ends WITHIN its first/last page. When
    the next chapter is anchored to the shared boundary page, we let the two units
    overlap on that single page (each keeps its own half) instead of forcing them
    apart; unanchored overlaps are still resolved forward as before."""
    out: list[dict] = []
    for ch in (parsed.get("chapters") or [])[: MAX_CHAPTERS * 2]:
        if not isinstance(ch, dict):
            continue
        title = (ch.get("title") or "").strip()[:_TITLE_CAP]
        try:
            start, end = int(ch.get("start")), int(ch.get("end"))
        except (TypeError, ValueError):
            continue
        start = max(1, min(start, num_pages))
        end = max(1, min(end, num_pages))
        if not title or end < start:
            continue
        status = ch.get("status") if ch.get("status") in _CHAPTER_STATUSES else ""
        out.append({"title": title, "start": start, "end": end,
                    "skip_hint": bool(ch.get("skip_hint")), "status": status,
                    "lesson_enabled": ch.get("lesson_enabled") is not False,
                    "start_anchor": _clean_anchor(ch.get("start_anchor")),
                    "end_anchor": _clean_anchor(ch.get("end_anchor"))})
    out.sort(key=lambda c: (c["start"], c["end"]))
    fixed: list[dict] = []
    for ch in out:
        if fixed:
            prev = fixed[-1]
            # Allow a deliberate mid-page split to share exactly the boundary
            # page (start == prev end) when either side marks the split point.
            share_boundary = (ch["start"] == prev["end"]
                              and (ch["start_anchor"] or prev["end_anchor"]))
            if ch["start"] <= prev["end"] and not share_boundary:
                ch["start"] = prev["end"] + 1
                if ch["start"] > ch["end"]:
                    continue
        fixed.append(ch)
    return fixed[:MAX_CHAPTERS]


def merge_chapters(chapters: list[dict], index: int, title: str = "") -> list[dict]:
    """Join chapters `index` and `index + 1` into one, spanning both page ranges.

    The boundary between them disappears entirely — whether it was a clean page
    break or a mid-page split — so the merged chapter keeps the FIRST one's
    start_anchor and the SECOND one's end_anchor (its own outer edges) and drops
    the anchors that described the removed cut. Raises IndexError when `index`
    isn't a real boundary."""
    if index < 0 or index + 1 >= len(chapters):
        raise IndexError("No boundary at that index")
    a, b = chapters[index], chapters[index + 1]
    merged = {
        "title": (title or "").strip()[:_TITLE_CAP]
                 or " & ".join([t for t in ((a.get("title") or "").strip(),
                                            (b.get("title") or "").strip()) if t])[:_TITLE_CAP]
                 or "Untitled unit",
        "start": min(int(a["start"]), int(b["start"])),
        "end": max(int(a["end"]), int(b["end"])),
        "skip_hint": bool(a.get("skip_hint")) and bool(b.get("skip_hint")),
        # If either half was intended for lessons, the combined chapter still
        # contains lesson material. The learner can turn it off after merging.
        "lesson_enabled": (a.get("lesson_enabled") is not False
                           or b.get("lesson_enabled") is not False),
        # Lessons were made from one of the halves → the merged range still has
        # lessons somewhere in it, so keep the flag rather than silently clearing.
        "status": a.get("status") or b.get("status") or "",
        "start_anchor": _clean_anchor(a.get("start_anchor")),
        "end_anchor": _clean_anchor(b.get("end_anchor")),
    }
    out = list(chapters)
    out[index:index + 2] = [merged]
    return out


# ── Mid-page unit boundaries (split a physical page between two units) ─────────

_MIN_PREFIX_MATCH = 8      # chars before a partial line may claim the anchor
_MAX_SPAN_LINES = 12       # lines an anchor may be spread across when rejoined


def _anchor_line_index(page_text: str, anchor: str) -> int | None:
    """Index of the line of `page_text` that matches the split `anchor`
    (whitespace-tolerant, casefolded), or None when it no longer occurs (→ the
    page is kept whole).

    EXACT matches win outright. Only if no line matches exactly do we fall back
    to a prefix match, and only for a substantial run of characters — an anchor
    is stored verbatim FROM the page, so an exact match is the normal case, and
    accepting the first loose prefix instead let a short earlier line ("Unit",
    "Lesson") hijack the boundary and trim the unit down to nothing.
    """
    target = re.sub(r"\s+", " ", anchor or "").strip().casefold()
    if not target:
        return None
    lines = [re.sub(r"\s+", " ", l).strip().casefold()
             for l in (page_text or "").split("\n")]
    for i, norm in enumerate(lines):
        if norm and norm == target:
            return i
    # The anchor may be spread over consecutive lines: a PDF that positions
    # words individually can extract one word per line, and the AI's anchor is
    # verified against the page as a whole (`_source_contains_quote`), so it can
    # legitimately name a "line" the page never stores as one. Rejoin a short run
    # and match that, or the split verifies fine and then silently does nothing.
    squeezed = target.replace(" ", "")
    for i in range(len(lines)):
        if not lines[i]:
            continue
        joined = ""
        for j in range(i, min(i + _MAX_SPAN_LINES, len(lines))):
            if not lines[j]:
                break
            joined = f"{joined} {lines[j]}".strip()
            if joined == target or joined.replace(" ", "") == squeezed:
                return i
            if len(joined) > len(target) + 2:
                break
    for i, norm in enumerate(lines):
        if not norm:
            continue
        shorter = min(len(norm), len(target))
        if shorter >= _MIN_PREFIX_MATCH and (norm.startswith(target)
                                             or target.startswith(norm)):
            return i
    return None


def split_page(page_text: str, *, head_anchor: str = "",
               tail_anchor: str = "") -> tuple[str, str, str]:
    """Split one page's text into (head_trim, kept, tail_trim) by line anchors.

    `head_anchor` marks where THIS unit's content begins (lines above it belong
    to the previous unit); `tail_anchor` marks where the NEXT unit begins (that
    line and everything after it belong to the next unit). Either may be empty."""
    lines = (page_text or "").split("\n")
    lo, hi = 0, len(lines)
    tail_idx = _anchor_line_index(page_text, tail_anchor)
    if tail_idx is not None:
        hi = tail_idx
    head_idx = _anchor_line_index(page_text, head_anchor)
    if head_idx is not None and head_idx <= hi:
        lo = head_idx
    return ("\n".join(lines[:lo]), "\n".join(lines[lo:hi]),
            "\n".join(lines[hi:]))


def unit_pages(pages: list[str], start: int, end: int,
               start_anchor: str = "", end_anchor: str = "") -> list[dict]:
    """Per-page view of a unit's pages [start, end] (1-based, inclusive) with any
    mid-page trim applied: only the first page can carry a head trim and only the
    last page a tail trim (a single-page unit can carry both). Returns
    [{page, head_trim, text, tail_trim}] — `text` is the portion kept for the
    unit; head/tail hold the greyed-out neighbouring-unit text for the preview."""
    out: list[dict] = []
    for p in range(start, end + 1):
        raw = pages[p - 1] if 1 <= p <= len(pages) else ""
        head, kept, tail = split_page(
            raw,
            head_anchor=start_anchor if p == start else "",
            tail_anchor=end_anchor if p == end else "")
        out.append({"page": p, "head_trim": head, "text": kept,
                    "tail_trim": tail})
    return out


def format_unit_source(pages: list[str], start: int, end: int,
                       start_anchor: str = "", end_anchor: str = "") -> str:
    """The reviewed source text for a unit, page-labelled and mid-page trimmed —
    the same shape the review UI shows and the generation routes fall back to."""
    return "\n\n".join(
        f"— PDF page {seg['page']} —\n{seg['text'].strip()}"
        for seg in unit_pages(pages, start, end, start_anchor, end_anchor)
    ).strip()


DETECT_ATTEMPTS = 2       # one automatic retry — see detect_chapters


async def detect_chapters(pages: list[str], target_lang: str, *, api_key: str,
                          anthropic_key: str | None = None, model: str,
                          attempts: int = DETECT_ATTEMPTS) -> tuple[list[dict], int]:
    """Propose the book's chapter structure as page ranges.

    Returns ``(chapters, llm_calls)`` — chapters are
    [{title, start, end, skip_hint, status}] and may be empty (the user can
    still define chapters by hand in the review UI).

    The call is retried once on a *soft* failure — the transport-level retry in
    ``translation._call`` only covers provider 5xx, so an empty candidate, a
    truncated reply, or JSON the parser can't recover left the user staring at
    "AI couldn't find clear chapter breaks" and hitting the button again by
    hand. Retrying here makes that automatic; ``llm_calls`` reports what was
    actually spent so metering stays honest.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    skeleton = build_skeleton(pages)
    if not skeleton.strip():
        return [], 0
    prompt = _build_chapter_prompt(target_lang, skeleton, len(pages))
    calls = 0
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        calls += 1
        try:
            raw = await llm.call(prompt, model=model, gemini_key=api_key,
                                 anthropic_key=anthropic_key)
            chapters = _norm_chapters(_parse_json(raw) or {}, len(pages))
        except Exception as e:          # noqa: BLE001 — retried, then re-raised
            last_error = e
            chapters = []
        if chapters:
            return chapters, calls
        if attempt + 1 >= max(1, attempts) and last_error is not None:
            raise last_error
    return [], calls


# ── Second pass: find mid-page boundaries the page-range detector can't ────────
# The skeleton detector only ever emits whole-page ranges, so a unit that starts
# partway down a shared page is always missed. This pass looks at the actual text
# on BOTH sides of each consecutive boundary and asks the model for the exact
# line where the next unit begins — turning a page break into a mid-page split
# (start_anchor/end_anchor + shared page) when the real boundary is inside a page.

MAX_REFINE_BOUNDARIES = 12       # bound the extra LLM calls per detect
REFINE_CONCURRENCY = 4           # boundaries examined in parallel
REFINE_BUDGET_S = 45.0           # wall-clock ceiling for the whole second pass
_REFINE_WINDOW_LINES = 45        # lines shown from each side of the boundary


def _page_lines(pages: list[str], page_no: int) -> list[str]:
    return (pages[page_no - 1] if 1 <= page_no <= len(pages) else "").split("\n")


def _first_content_line(lines: list[str]) -> int:
    for i, ln in enumerate(lines):
        if ln.strip():
            return i
    return -1


def _build_boundary_prompt(target_lang: str, a_title: str, b_title: str,
                           a_page: int, b_page: int, a_tail: str,
                           b_head: str) -> str:
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    return (
        f"You are refining where one unit ends and the next begins in a {name} "
        f"textbook a learner uploaded. Two consecutive units were detected:\n"
        f'• EARLIER unit: "{a_title}" (last page ≈ {a_page})\n'
        f'• LATER unit: "{b_title}" (first page ≈ {b_page})\n\n'
        f"Below is the text on BOTH sides of their boundary, labelled by page. "
        f'Find the EXACT line where the LATER unit "{b_title}" actually begins — '
        f"its heading or its first real line of content.\n\n"
        f"── RULES ──\n"
        f'• If "{b_title}" begins at the very top of page {b_page} (a clean page '
        f'break — nothing of it appears on page {a_page}, and page {a_page} is '
        f'entirely the earlier unit), there is NO mid-page split: return '
        f'{{"split": false}}.\n'
        f"• Otherwise the boundary falls WITHIN a page. Return the verbatim line "
        f"where the later unit begins and which page that line is on. Copy the "
        f"line EXACTLY as printed (the server verifies it).\n"
        f"• Ignore running headers/footers and page numbers when deciding.\n\n"
        f"── TEXT ──\n[page {a_page}]\n{a_tail}\n\n[page {b_page}]\n{b_head}\n\n"
        f"Return ONLY JSON, no other text:\n"
        '{"split": true|false, "line": "<verbatim line the later unit starts on>", '
        '"page": <page number that line is on>}'
    )


async def _plan_boundary_split(pages: list[str], a: dict, b: dict,
                               target_lang: str, *, api_key: str,
                               anthropic_key: str | None, model: str
                               ) -> dict | None:
    """Look at one A→B boundary and decide where the later unit really starts.

    Pure: returns the change to apply (``{"share": page, "line": …}`` when the
    later unit starts partway down the earlier unit's last page, or
    ``{"extend": page, "line": …}`` when the earlier unit runs into the top of
    the later unit's first page) or None for a clean page break. Deciding and
    applying are separate so boundaries can be examined concurrently and the
    results still applied in a deterministic order.
    """
    a_page, b_page = a["end"], b["start"]
    a_lines = _page_lines(pages, a_page)
    b_lines = _page_lines(pages, b_page)
    a_tail = "\n".join(a_lines[-_REFINE_WINDOW_LINES:]).strip()
    b_head = "\n".join(b_lines[:_REFINE_WINDOW_LINES]).strip()
    if not a_tail and not b_head:
        return None
    prompt = _build_boundary_prompt(target_lang, a["title"], b["title"],
                                    a_page, b_page, a_tail, b_head)
    raw = await llm.call(prompt, model=model, gemini_key=api_key,
                         anthropic_key=anthropic_key)
    parsed = _parse_json(raw) or {}
    if not parsed.get("split"):
        return None
    line = _clean_anchor(parsed.get("line"))
    try:
        page = int(parsed.get("page"))
    except (TypeError, ValueError):
        return None
    if not line:
        return None
    if page == a_page and _source_contains_quote(pages[a_page - 1], line):
        # The later unit starts partway down the earlier unit's last page:
        # share that page, trimming each side at the split line.
        return {"share": a_page, "line": line}
    if page == b_page and _source_contains_quote(pages[b_page - 1], line):
        idx = _anchor_line_index(pages[b_page - 1], line)
        # A clean top-of-page start is not a mid-page split.
        if idx is None or idx <= _first_content_line(b_lines):
            return None
        # The earlier unit runs into the top of the later unit's first page.
        return {"extend": b_page, "line": line}
    return None


async def refine_chapter_boundaries(
        pages: list[str], chapters: list[dict], target_lang: str, *,
        api_key: str, anthropic_key: str | None = None, model: str,
        budget_s: float = REFINE_BUDGET_S,
) -> tuple[list[dict], int]:
    """Second detection pass: promote page-break boundaries to mid-page splits
    where the text shows the next unit begins inside a shared page. One LLM call
    per eligible consecutive boundary (bounded). Returns (chapters, llm_calls).

    The calls run CONCURRENTLY (``REFINE_CONCURRENCY`` at a time) under a
    wall-clock ``budget_s``: run serially, a dozen boundaries stacked into
    minutes of latency on top of detection, which is what made the whole
    /analyze request time out and look like a flaky failure. Every boundary is
    independent and best-effort — one that errors or runs past the budget keeps
    its clean page break instead of failing the pass.
    """
    if target_lang not in LANG_INFO or len(chapters) < 2:
        return chapters, 0
    eligible = [
        i for i in range(len(chapters) - 1)
        # Only consecutive, un-split, gap-free boundaries can hide a mid-page one.
        if chapters[i + 1]["start"] == chapters[i]["end"] + 1
        and not chapters[i].get("end_anchor")
        and not chapters[i + 1].get("start_anchor")
    ][:MAX_REFINE_BOUNDARIES]
    if not eligible:
        return chapters, 0

    deadline = time.monotonic() + max(0.01, budget_s)
    sem = asyncio.Semaphore(REFINE_CONCURRENCY)
    issued = 0

    async def run(i: int) -> dict | None:
        nonlocal issued
        async with sem:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None      # out of budget — never sent, never charged
            issued += 1
            try:
                return await asyncio.wait_for(
                    _plan_boundary_split(
                        pages, chapters[i], chapters[i + 1], target_lang,
                        api_key=api_key, anthropic_key=anthropic_key,
                        model=model),
                    timeout=remaining)
            except Exception:
                return None      # keep the clean page break; move on

    plans = await asyncio.gather(*(run(i) for i in eligible))
    for i, plan in zip(eligible, plans):
        if not plan:
            continue
        a, b = chapters[i], chapters[i + 1]
        if "share" in plan:
            b["start"] = plan["share"]
        else:
            a["end"] = plan["extend"]
        a["end_anchor"] = plan["line"]
        b["start_anchor"] = plan["line"]
    return _norm_chapters({"chapters": chapters}, len(pages)), issued


# ── Lesson segmentation (per chapter) ─────────────────────────────────────────

def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split the chapter text into ~size-char chunks on line boundaries."""
    text = (text or "").strip()
    chunks: list[str] = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        cut = text.rfind("\n", max(0, size - 2000), size)
        if cut <= 0:
            cut = size
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return [c for c in chunks if c]


def _build_segment_prompt(target_lang: str, chunk: str, part: int, total: int,
                          chapter_title: str) -> str:
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    ctx = f' (the chapter "{chapter_title}")' if chapter_title else ""
    return (
        f"You are an expert {name} curriculum designer. Below is part {part}/{total} "
        f"of one chapter{ctx} of a {name} textbook / grammar book a learner "
        f"uploaded. Segment it into TEACHABLE LESSONS, so our app can turn each "
        f"one into an interactive lesson with drills.\n\n"
        f"{_lang_preamble(info)}"
        f"── RULES ──\n"
        f"• One LESSON = one satisfying, self-contained chunk: a grammar pattern "
        f"(kind \"grammar\") or a themed set of 4–8 words/phrases (kind \"vocab\"). "
        f"Follow the book's own progression and granularity — merge fragments that "
        f"belong together, split sections that cover two distinct skills.\n"
        f"• skill.key = stable snake_case; skill.label = the pattern name (grammar) "
        f"or native theme word (vocab); gloss in English.\n"
        f"• target_items: for GRAMMAR the forms/verbs/particles to cover; for VOCAB "
        f"the words themselves. Native {name} script, citation form.\n"
        f"• source = condensed notes FROM THE TEXT for this lesson (≤1500 chars): "
        f"the rule as the book states it, its example sentences, its vocab list — "
        f"enough for another teacher to author the lesson faithfully to the book. "
        f"If the text includes the book's own EXERCISES for this material, condense "
        f"the best of them (with answers when the text or your own certain knowledge "
        f"provides them) into the notes too, marked \"Book exercises:\".\n"
        f"• If the text only shows romanization for a word/example (no native "
        f"script), write the native {name} script yourself and keep the book's "
        f"romanization beside it.\n"
        f"• SKIP non-teachable material: front matter, exercise answer keys, "
        f"indexes, pronunciation-guide tables (our app teaches script/sounds "
        f"separately). If this part contains nothing teachable, return "
        f'{{"lessons": []}}.\n\n'
        f"── TEXT (part {part}/{total}) ──\n{chunk}\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{"lessons": [\n'
        '  {"title": "<short English lesson title>",\n'
        '   "skill": {"kind": "grammar"|"vocab", "key": "<snake_case>", "label": "<pattern name OR native theme word>", "gloss": "<one-line English>"},\n'
        '   "target_items": [{"label": "<native script>", "gloss": "<English>"}, ...],\n'
        '   "source": "<condensed notes from the text>"}\n'
        "]}"
    )


def _norm_lessons(parsed: dict) -> list[dict]:
    """Strictly normalize one chunk's segmentation. Malformed entries are dropped
    (filter-then-clip, like the planner): a lesson needs a skill label or at least
    one target item."""
    out: list[dict] = []
    for l in (parsed.get("lessons") or []):
        if not isinstance(l, dict):
            continue
        skill = l.get("skill") or {}
        if not isinstance(skill, dict):
            skill = {}
        kind = (skill.get("kind") or "vocab").strip().lower()
        if kind not in ("grammar", "vocab"):
            kind = "vocab"
        label = (skill.get("label") or "").strip()
        items = []
        for it in (l.get("target_items") or [])[:MAX_ITEMS_PER_LESSON]:
            if isinstance(it, dict) and (it.get("label") or "").strip():
                items.append({"label": it["label"].strip(),
                              "gloss": (it.get("gloss") or "").strip()})
        if not label and not items:
            continue
        if not label:
            label = items[0]["label"]
        out.append({
            "title": (l.get("title") or "").strip()[:_TITLE_CAP] or label[:_TITLE_CAP],
            "skill": {"kind": kind, "key": (skill.get("key") or "").strip(),
                      "label": label, "gloss": (skill.get("gloss") or "").strip()},
            "target_items": items,
            "source": (l.get("source") or "").strip()[:_SOURCE_CAP],
        })
    return out


def build_visual_contact_sheet(visuals: list[dict]) -> bytes | None:
    """Combine selected page-linked JPEGs into one labelled planning image."""
    if not visuals:
        return None
    try:
        import io
        from PIL import Image, ImageDraw, ImageOps
    except ImportError:
        return None
    tiles = []
    for visual in visuals[:6]:
        try:
            im = Image.open(io.BytesIO(visual["data"])).convert("RGB")
            im = ImageOps.contain(im, (560, 360))
            pages = visual.get("pages") or [visual.get("page")]
            label = "PDF page" + ("s" if len(pages) != 1 else "") + " " + ", ".join(
                str(p) for p in pages if p is not None)
            tiles.append((im, label))
        except Exception:
            continue
    if not tiles:
        return None
    cols = 2 if len(tiles) > 1 else 1
    rows = (len(tiles) + cols - 1) // cols
    cell_w, cell_h = 590, 410
    sheet = Image.new("RGB", (cell_w * cols, cell_h * rows), "white")
    draw = ImageDraw.Draw(sheet)
    for i, (im, label) in enumerate(tiles):
        x, y = (i % cols) * cell_w, (i // cols) * cell_h
        sheet.paste(im, (x + (cell_w - im.width) // 2, y + 34))
        draw.text((12 + x, 10 + y), label, fill="black")
        draw.rectangle((x, y, x + cell_w - 1, y + cell_h - 1), outline="#cccccc")
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=86, optimize=True)
    return buf.getvalue()


def _build_source_lesson_prompt(target_lang: str, source: str, book_title: str,
                                start: int, end: int, guidance: str = "",
                                has_visuals: bool = False) -> str:
    """Plan exactly one lesson from user-reviewed textbook text."""
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    direction = guidance.strip() or "Choose the most useful coherent topic in this selection."
    visual_note = (
        "A labelled contact sheet of images/diagrams extracted from these pages "
        "is attached. Use a visual only when it materially clarifies the lesson; "
        "ignore decorative or irrelevant images. Include relevant facts from a "
        "diagram in the source digest so the lesson author can use them.\n\n"
        if has_visuals else ""
    )
    return (
        f"You are planning ONE interactive {name} lesson from textbook text that "
        f"the learner has personally reviewed and approved. The source is from "
        f'"{book_title}" (PDF pages {start}-{end}).\n\n'
        f"{_lang_preamble(info)}"
        "── LEARNER'S DIRECTION ──\n"
        f"{direction}\n\n"
        f"{visual_note}"
        "── RULES ──\n"
        "• Produce exactly ONE satisfying, self-contained lesson. Follow the "
        "learner's direction when it is compatible with the selected text.\n"
        "• Base the lesson on the selected text only. Do not broaden it into a "
        "survey of unrelated material.\n"
        "• Pick one grammar pattern (kind \"grammar\") or one themed set of 4–8 "
        "words/phrases (kind \"vocab\").\n"
        "• skill.key is stable snake_case; skill.label is the pattern name for "
        "grammar or a native-script theme label for vocabulary; gloss is English.\n"
        f"• target_items use native {name} script and citation forms. If the book "
        "uses romanization only, reconstruct the native script.\n"
        "• source is a short factual digest of the selected rules, examples, "
        "vocabulary, and useful book exercises.\n\n"
        f"── APPROVED TEXTBOOK TEXT ──\n{source}\n\n"
        "Return ONLY valid JSON, no other text:\n"
        '{"lesson": {"title": "<short English title>", '
        '"skill": {"kind": "grammar|vocab", "key": "<snake_case>", '
        '"label": "<pattern or native theme>", "gloss": "<English>"}, '
        '"target_items": [{"label": "<native script>", "gloss": "<English>"}], '
        '"source": "<short digest>"}}'
    )


async def plan_source_lesson(source: str, target_lang: str, book_title: str,
                             start: int, end: int, guidance: str = "", *,
                             visuals: list[dict] | None = None,
                             api_key: str, anthropic_key: str | None = None,
                             model: str) -> dict:
    """Plan one lesson from an explicitly approved source selection.

    Unlike ``segment_chapter`` this never creates a batch or hidden queue. The
    caller temporarily queues this single normalized spec only to reuse the
    normal lesson authoring path.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    source = (source or "").strip()
    if not source:
        raise ValueError("The selected pages do not contain any text.")
    if len(source) > MAX_LESSON_SOURCE_CHARS:
        raise ValueError(
            f"The selected text is too long ({len(source):,} characters; "
            f"maximum {MAX_LESSON_SOURCE_CHARS:,}). Choose fewer pages or trim "
            "the text before creating the lesson."
        )
    contact_sheet = build_visual_contact_sheet(visuals or [])
    prompt = _build_source_lesson_prompt(
        target_lang, source, (book_title or "Textbook")[:_TITLE_CAP],
        start, end, (guidance or "")[:1000], bool(contact_sheet))
    if contact_sheet:
        raw = await llm.call_with_image(
            prompt, contact_sheet, model=model, gemini_key=api_key,
            anthropic_key=anthropic_key)
    else:
        raw = await llm.call(prompt, model=model, gemini_key=api_key,
                             anthropic_key=anthropic_key)
    parsed = _parse_json(raw) or {}
    lesson = parsed.get("lesson")
    if lesson is None and isinstance(parsed.get("lessons"), list):
        lesson = parsed["lessons"][0] if parsed["lessons"] else None
    normalized = _norm_lessons({"lessons": [lesson] if lesson else []})
    if not normalized:
        raise ValueError(
            "The selected text did not produce a teachable lesson. Choose a "
            "different page range or add a more specific instruction."
        )
    return normalized[0]


def _fold_accents(s: str) -> str:
    """Casefold + strip combining diacritics so romanization schemes that differ
    only in accent (the book's OCR-extracted ``bâai``/``â`` vs. standard Yale
    ``bāai``/``ā``, ``ô`` vs ``o``, etc.) still match. Verbatim quotes fail the
    substring check otherwise, dropping whole lessons over cosmetic accent drift."""
    import unicodedata
    decomposed = unicodedata.normalize("NFD", s)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", stripped).casefold()


def _source_contains_quote(source: str, quote: str) -> bool:
    """Whitespace-tolerant, accent-folded, but otherwise verbatim verification."""
    haystack = _fold_accents(re.sub(r"\s+", " ", source).strip())
    needle = _fold_accents(re.sub(r"\s+", " ", quote).strip())
    return len(needle) >= 3 and needle in haystack


def _norm_source_unit(parsed: dict, source: str) -> dict:
    """Validate a coverage-complete textbook unit plan — degrading, not rejecting.

    Every lesson is placed by its ``covers`` list against the source inventory.
    Verbatim ``source_excerpts`` are used as grounding WHEN they verify against the
    approved text, but a lesson whose excerpts fail verification (common when the
    book's OCR-extracted romanization or a leftover text-layer artifact differs
    cosmetically from the model's clean transcription) is kept with labels-only
    grounding rather than dropped — dropping it stranded its concepts as "missing"
    and killed the whole unit over cosmetic drift. The unit is rejected only when
    it collapses: no usable lessons, or the majority of concepts left unmapped.
    """
    inventory: list[dict] = []
    inventory_ids: set[str] = set()
    for raw in (parsed.get("concept_inventory") or [])[:48]:
        if not isinstance(raw, dict):
            continue
        concept_id = str(raw.get("id") or "").strip().lower()
        label = str(raw.get("label") or "").strip()[:120]
        if (not re.fullmatch(r"c\d{1,2}", concept_id) or not label or
                concept_id in inventory_ids):
            continue
        inventory_ids.add(concept_id)
        inventory.append({"id": concept_id, "label": label})
    if not inventory:
        raise ValueError("The unit planner did not identify any textbook concepts.")

    lessons: list[dict] = []
    coverage_counts = {concept_id: 0 for concept_id in inventory_ids}
    for raw in (parsed.get("lessons") or [])[:MAX_LESSONS_PER_CHAPTER]:
        if not isinstance(raw, dict):
            continue
        normalized = _norm_lessons({"lessons": [raw]})
        if not normalized:
            continue
        covers = []
        for concept_id in raw.get("covers") or []:
            concept_id = str(concept_id).strip().lower()
            if concept_id in inventory_ids and concept_id not in covers:
                covers.append(concept_id)
        if not covers:
            # Without a coverage list we can't place the lesson in the unit's
            # concept map, so it can't count toward completeness — skip it.
            continue
        excerpts = []
        for quote in (raw.get("source_excerpts") or [])[:8]:
            quote = str(quote or "").strip()[:1200]
            if quote and _source_contains_quote(source, quote):
                excerpts.append(quote)
        for concept_id in covers:
            coverage_counts[concept_id] += 1
        lesson = normalized[0]
        labels = [c["label"] for c in inventory if c["id"] in covers]
        notes = str(raw.get("teaching_notes") or "").strip()[:1000]
        if excerpts:
            grounding = (
                "Required textbook coverage: " + "; ".join(labels) + "\n"
                "Verbatim textbook excerpts:\n" +
                "\n".join(f"• {quote}" for quote in excerpts)
            )
        else:
            # Labels-only fallback — the model's excerpts didn't verify against
            # the approved text. Teach the listed coverage from the book anyway.
            grounding = (
                "Required textbook coverage: " + "; ".join(labels) + "\n"
                "(No verbatim excerpts verified for this lesson — teach the listed "
                "coverage faithfully from the book's own material for these pages.)"
            )
        if notes:
            grounding += "\nPlanner notes (secondary to the excerpts): " + notes
        lesson["source"] = grounding[:_SOURCE_CAP]
        lesson["covers"] = covers
        lessons.append(lesson)

    if not lessons:
        raise ValueError("The selected chapter did not produce any grounded lessons.")
    # A concept covered by no lesson is a real planner gap, but tolerate a few
    # rather than discarding a mostly-good unit. Reject only if coverage collapsed
    # (the majority unmapped) — reinforcing a concept across lessons is fine and
    # never rejected.
    missing = [c["label"] for c in inventory if coverage_counts[c["id"]] == 0]
    if len(missing) > max(2, len(inventory) // 2):
        raise ValueError(
            "The unit plan left most of the textbook coverage unmapped (missing: "
            + ", ".join(missing[:8])
            + (", …" if len(missing) > 8 else "")
            + "). Please try generating it again, or select a smaller page range."
        )
    return {"concept_inventory": inventory, "lessons": lessons}


def _build_source_unit_prompt(target_lang: str, source: str, book_title: str,
                              unit_title: str, start: int, end: int,
                              guidance: str = "", has_visuals: bool = False) -> str:
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    direction = guidance.strip() or "Follow the textbook's progression and emphasis."
    visual_note = (
        "A labelled contact sheet of user-selected diagrams/images is attached. "
        "Inventory and teach material visible there when it is instructional; "
        "ignore decoration. Describe its relevant facts in teaching_notes.\n\n"
        if has_visuals else ""
    )
    return (
        f"You are converting ONE complete chapter of a {name} textbook into ONE "
        f"interactive app unit containing MULTIPLE focused lessons. The source is "
        f'\"{book_title}\", unit \"{unit_title}\" (PDF pages {start}-{end}).\n\n'
        f"{_lang_preamble(info)}"
        "── LEARNER'S DIRECTION ──\n" + direction + "\n\n" + visual_note +
        "── NON-NEGOTIABLE SOURCE FIDELITY ──\n"
        "• This textbook chapter maps 1:1 to this app unit. Do not merge it with "
        "another chapter and do not add unrelated syllabus material.\n"
        "• First inventory EVERY teachable concept in the approved source: each "
        "grammar rule/distinction, usable expression set, vocabulary group, "
        "pronunciation point, and instructional exercise type. Use stable IDs c1, "
        "c2, ... in reading order. Do not inventory headers or duplicated text-layer "
        "artifacts.\n"
        "• Then produce 2–12 coherent lessons (one is allowed only if the source "
        "truly contains one indivisible concept). Every inventory ID must occur in "
        "exactly one lesson's covers list: none missing, none duplicated.\n"
        "• Preserve the book's order, terminology, distinctions, examples, and "
        "exercise intent. Make it interactive, but do not substitute a generic "
        "lesson on the same topic.\n"
        "• source_excerpts must be copied VERBATIM from APPROVED TEXTBOOK TEXT "
        "(including its spelling/romanization). Give each lesson the exact rules, "
        "examples, phrases, or exercise lines it needs; the server verifies them.\n"
        "• target_items use native script where possible. If the book only supplies "
        f"romanization, reconstruct native {name} script but preserve the original "
        "romanization in teaching_notes/source excerpts.\n"
        "• One lesson is one focused grammar concept or a cohesive set of 4–10 "
        "expressions/words. Split broad units into enough lessons to teach everything.\n\n"
        f"── APPROVED TEXTBOOK TEXT ──\n{source}\n\n"
        "Return ONLY valid JSON:\n"
        '{"concept_inventory":[{"id":"c1","label":"<specific source concept>"}],'
        '"lessons":[{"title":"<short English title>",'
        '"skill":{"kind":"grammar|vocab","key":"<snake_case>",'
        '"label":"<pattern or native theme>","gloss":"<English>"},'
        '"target_items":[{"label":"<native script>","gloss":"<English>"}],'
        '"covers":["c1"],"source_excerpts":["<exact verbatim excerpt>"],'
        '"teaching_notes":"<only useful interpretation/context>"}]}'
    )


PLAN_ATTEMPTS = 2         # one automatic retry — see plan_source_unit


async def plan_source_unit(source: str, target_lang: str, book_title: str,
                           unit_title: str, start: int, end: int,
                           guidance: str = "", *,
                           visuals: list[dict] | None = None,
                           api_key: str, anthropic_key: str | None = None,
                           model: str,
                           attempts: int = PLAN_ATTEMPTS) -> dict:
    """Plan a source-complete multi-lesson unit from an approved chapter.

    Returns the plan with an ``llm_calls`` count so the caller meters what was
    actually spent. Planning is retried once on a *soft* failure — an unparseable
    reply, an empty inventory, or coverage the validator rejects. Those are all
    model wobble on the same prompt, and the user's only recourse used to be
    tapping "Create unit" again and waiting through the whole flow a second time.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    source = (source or "").strip()
    if not source:
        raise ValueError("The selected pages do not contain any text.")
    if len(source) > MAX_LESSON_SOURCE_CHARS:
        raise ValueError(
            f"The selected text is too long ({len(source):,} characters; "
            f"maximum {MAX_LESSON_SOURCE_CHARS:,}). Choose fewer pages or trim it."
        )
    contact_sheet = build_visual_contact_sheet(visuals or [])
    prompt = _build_source_unit_prompt(
        target_lang, source, (book_title or "Textbook")[:_TITLE_CAP],
        (unit_title or "Textbook unit")[:_TITLE_CAP], start, end,
        (guidance or "")[:1000], bool(contact_sheet))
    calls = 0
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        calls += 1
        try:
            if contact_sheet:
                raw = await llm.call_with_image(
                    prompt, contact_sheet, model=model, gemini_key=api_key,
                    anthropic_key=anthropic_key)
            else:
                raw = await llm.call(prompt, model=model, gemini_key=api_key,
                                     anthropic_key=anthropic_key)
            plan = _norm_source_unit(_parse_json(raw) or {}, source)
            plan["llm_calls"] = calls
            return plan
        except Exception as e:          # noqa: BLE001 — retried, then re-raised
            last_error = e
    raise last_error   # type: ignore[misc]


_MERGE_TITLE_CAP = 60


def joined_title(a_title: str, b_title: str) -> str:
    """The deterministic name for two merged units — also the fallback when the
    AI rename can't run, so callers can tell the two apart by comparing."""
    joined = " & ".join([t for t in ((a_title or "").strip(),
                                     (b_title or "").strip()) if t][:2])
    return (joined or (a_title or b_title or "Unit").strip())[:_MERGE_TITLE_CAP]


async def name_merged_unit(a_title: str, b_title: str, source: str,
                           target_lang: str, book_title: str = "", *,
                           api_key: str, anthropic_key: str | None = None,
                           model: str) -> str:
    """One short title for two units being merged back together.

    Deleting a boundary in the reader joins two units whose names each describe
    only half of what the merged unit now covers ("Greetings" + "Introductions").
    One cheap call renames it from the merged pages themselves. Best-effort: any
    failure falls back to joining the two titles, so a merge never fails over
    its name."""
    fallback = joined_title(a_title, b_title)
    if target_lang not in LANG_INFO:
        return fallback
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    excerpt = (source or "").strip()[:4000]
    if not excerpt:
        return fallback
    prompt = (
        f"You are naming one unit of a {name} textbook course.\n"
        f"Two adjacent units are being merged into one because the learner "
        f"removed the boundary between them.\n"
        + (f"Book: {book_title[:_TITLE_CAP]}\n" if book_title else "")
        + f"First unit's title: {a_title or '(untitled)'}\n"
          f"Second unit's title: {b_title or '(untitled)'}\n\n"
          "TEXTBOOK PAGES NOW IN THE MERGED UNIT:\n"
          f"{excerpt}\n\n"
          "Write ONE title for the merged unit that covers everything above. "
          "Prefer the book's own wording for the section if it has one. "
          f"At most {_MERGE_TITLE_CAP} characters, no quotes, no numbering.\n"
          'Reply with JSON only: {"title": "..."}'
    )
    try:
        raw = await llm.call(prompt, model=model, gemini_key=api_key,
                             anthropic_key=anthropic_key)
        title = str((_parse_json(raw) or {}).get("title") or "").strip()
    except Exception:
        return fallback
    title = re.sub(r"\s+", " ", title).strip(" \"'“”·-–—")
    return title[:_MERGE_TITLE_CAP] or fallback


async def segment_chapter(pages: list[str], start: int, end: int,
                          target_lang: str, chapter_title: str, *, api_key: str,
                          anthropic_key: str | None = None, model: str) -> dict:
    """Segment ONE chapter (pages start..end, 1-based inclusive) into queued
    lesson specs. One LLM call per ~9k-char chunk. All the chapter's lessons
    form a single course unit titled after the chapter, so the roadmap mirrors
    the book's own table of contents. Returns
      {"items": [{unit_title, unit_size, spec, source}, ...], "llm_calls": n}
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    start = max(1, start)
    end = min(len(pages), end)
    text = "\n".join(pages[start - 1:end]).strip()[:MAX_CHAPTER_CHARS]
    chunks = chunk_text(text)
    lessons: list[dict] = []
    calls = 0
    for i, ch in enumerate(chunks):
        prompt = _build_segment_prompt(target_lang, ch, i + 1, len(chunks),
                                       chapter_title)
        raw = await llm.call(prompt, model=model, gemini_key=api_key,
                             anthropic_key=anthropic_key)
        calls += 1
        lessons.extend(_norm_lessons(_parse_json(raw) or {}))
        if len(lessons) >= MAX_LESSONS_PER_CHAPTER:
            lessons = lessons[:MAX_LESSONS_PER_CHAPTER]
            break

    unit_title = (chapter_title or "").strip()[:_TITLE_CAP] or \
        (lessons[0]["title"] if lessons else "")
    items = [{
        "unit_title": f"📕 {unit_title}",
        "unit_size": len(lessons),
        "spec": {"title": l["title"], "skill": l["skill"],
                 "target_items": l["target_items"]},
        "source": l["source"],
    } for l in lessons]
    return {"items": items, "llm_calls": calls}


# ── Chapter vocab deck (flat word list → SRS flashcards) ──────────────────────
# A lighter sibling of the lesson pipeline: extract EVERY vocabulary item a
# chapter teaches into a flat, reviewable word list the learner turns into a
# flashcard deck. One cheap LLM call per ~9k-char chunk — no grammar teaching,
# no drill authoring. Romanization is always recomputed by the offline oracle
# downstream (main), never trusted from the model, like every other card path.

_VALID_CEFR = {"A1", "A2", "B1", "B2", "C1", "C2"}
_GLOSS_CAP = 160
_EXAMPLE_CAP = 200


def _build_vocab_prompt(target_lang: str, chunk: str, part: int, total: int,
                        book_title: str, guidance: str = "") -> str:
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    ctx = f' from "{book_title}"' if book_title else ""
    direction = ("\n── LEARNER'S DIRECTION ──\n" + guidance.strip() + "\n"
                 if guidance.strip() else "")
    return (
        f"You are building a vocabulary flashcard deck from part {part}/{total} of "
        f"one chapter{ctx} of a {name} textbook a learner uploaded. Extract EVERY "
        f"vocabulary item this text teaches, so each becomes one flashcard.\n\n"
        f"{_lang_preamble(info)}"
        f"{direction}"
        f"── RULES ──\n"
        f"• Prefer the book's OWN vocabulary lists, glossaries, and highlighted "
        f"words. Also include content words from example sentences that are clearly "
        f"being taught. Include single words AND short set phrases the book presents "
        f"as units.\n"
        f"• SKIP grammar particles taught only as grammar, page numbers, exercise "
        f"instructions, proper nouns unless they are vocabulary, and anything not a "
        f"learnable word/phrase.\n"
        f"• target = the {name} word in native script, citation form. If the book "
        f"shows only romanization, reconstruct the native script.\n"
        f"• gloss = a short English meaning (a few words).\n"
        f"• example = ONE short example phrase/sentence from the book that uses the "
        f"word, in native script, if the text provides one — else omit it.\n"
        f"• cefr = your best CEFR estimate (A1/A2/B1/B2/C1/C2) for the word.\n"
        f"• Do NOT include romanization — our system computes it.\n"
        f"• Do not repeat the same word twice.\n\n"
        f"── TEXT (part {part}/{total}) ──\n{chunk}\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{"vocab": [\n'
        '  {"target": "<native script>", "gloss": "<English>", '
        '"example": "<native example or omit>", "cefr": "A1"}\n'
        "]}"
    )


def _norm_vocab(parsed: dict) -> list[dict]:
    """Strictly normalize one chunk's vocab list (filter-then-clip). An item needs
    a non-empty target and gloss; cefr is validated, example capped."""
    out: list[dict] = []
    seen: set[str] = set()
    for v in (parsed.get("vocab") or []):
        if not isinstance(v, dict):
            continue
        target = (v.get("target") or "").strip()
        gloss = (v.get("gloss") or "").strip()[:_GLOSS_CAP]
        if not target or not gloss or target in seen:
            continue
        seen.add(target)
        cefr = (v.get("cefr") or "").strip().upper()
        out.append({
            "target": target,
            "gloss": gloss,
            "example": (v.get("example") or "").strip()[:_EXAMPLE_CAP],
            "cefr": cefr if cefr in _VALID_CEFR else None,
        })
    return out


async def extract_chapter_vocab(source: str, target_lang: str, book_title: str,
                                guidance: str = "", *, api_key: str,
                                anthropic_key: str | None = None,
                                model: str) -> dict:
    """Extract a chapter's whole vocabulary as a flat list for a flashcard deck.

    One LLM call per ~9k-char chunk of the approved source; deduped across chunks
    by native target. Returns {"items": [{target, gloss, example, cefr}, ...],
    "llm_calls": n}. Never authors lessons — just the words.
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    source = (source or "").strip()
    if not source:
        raise ValueError("The selected pages do not contain any text.")
    if len(source) > MAX_LESSON_SOURCE_CHARS:
        raise ValueError(
            f"The selected text is too long ({len(source):,} characters; "
            f"maximum {MAX_LESSON_SOURCE_CHARS:,}). Choose fewer pages or trim it."
        )
    chunks = chunk_text(source)
    items: list[dict] = []
    seen: set[str] = set()
    calls = 0
    for i, ch in enumerate(chunks):
        prompt = _build_vocab_prompt(target_lang, ch, i + 1, len(chunks),
                                     (book_title or "").strip()[:_TITLE_CAP],
                                     guidance)
        raw = await llm.call(prompt, model=model, gemini_key=api_key,
                             anthropic_key=anthropic_key)
        calls += 1
        for v in _norm_vocab(_parse_json(raw) or {}):
            if v["target"] not in seen:
                seen.add(v["target"])
                items.append(v)
        if len(items) >= MAX_VOCAB_ITEMS:
            items = items[:MAX_VOCAB_ITEMS]
            break
    return {"items": items, "llm_calls": calls}
