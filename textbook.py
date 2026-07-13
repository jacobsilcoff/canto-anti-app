"""Textbook PDF → reviewable source selection → interactive lesson.

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
   Detected sections remain editable reusable presets; they are not a generation
   batch. A contact sheet of selected visuals is included in source planning.
3. CREATE ONE LESSON — `plan_source_lesson` identifies one coherent skill from
   the approved text, then the normal lesson author receives the actual approved
   source + learner instruction and immediately creates the lesson.

The approved one-off spec is temporarily put at the front of `lesson_queue` so
`main._author_next_lesson` can reuse the established authoring, validation, and
course-bookkeeping path; it is consumed in the same request and never exposed as
a user workflow. `segment_chapter` and persistent batch queue support remain for
backward compatibility with imports made before this interaction was redesigned.

All LLM output is strictly normalized here (same trust model as the planner):
malformed chapters/units/lessons are dropped, ranges are clamped, counts are
capped, and keys are synthesized downstream by main._concepts_from_spec when
missing.
"""
import re

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
MAX_CHAPTERS = 60                 # structure-detection cap
_SOURCE_CAP = 2000                # per-lesson condensed source notes
_TITLE_CAP = 80
_SKELETON_CAP = 16_000            # chapter-detection prompt budget


# ── Deterministic page cleanup ────────────────────────────────────────────────

# Some PDFs render styled text as several overlapping layers, and extraction
# emits the same phrase 3–5× back to back ("jóu sàhnjóu sàhnjóu sàhnjóu sàhn").
# Collapse any unit repeated ≥4 times consecutively — legitimate exact 4×
# adjacent repetition is vanishingly rare in prose, and this text feeds an LLM,
# not the learner's screen. Digit-only units are kept ("10000" must not become
# "10"); whitespace runs are left to clean_text.
_ARTIFACT_RE = re.compile(r"(.{1,80}?)\1{3,}")


def clean_page_text(text: str) -> str:
    """Collapse the repeated-text-layer extraction artifact within one page."""
    def _repl(m: re.Match) -> str:
        unit = m.group(1)
        if unit.isdigit() or unit.isspace():
            return m.group(0)
        return unit

    out = []
    for line in (text or "").splitlines():
        prev = None
        # Fixed-point: nested repeats collapse layer by layer ("hóuuuu"→"hóu").
        for _ in range(6):
            if prev == line:
                break
            prev = line
            line = _ARTIFACT_RE.sub(_repl, line)
        out.append(line)
    return "\n".join(out)


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

    return ["\n".join(l for l in p.splitlines() if keep(l)) for p in pages]


def clean_pages(pages: list[str]) -> list[str]:
    """The full deterministic cleanup applied once at upload."""
    return strip_repeated_lines([clean_page_text(p) for p in pages])


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


def _norm_chapters(parsed: dict, num_pages: int) -> list[dict]:
    """Clamp/repair a chapter list (from the detector OR a user edit): ranges
    forced inside the book, ordered by start page, overlaps resolved in favour
    of the earlier chapter, empty/malformed entries dropped. A valid `status`
    survives (user edits round-trip through this too)."""
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
                    "skip_hint": bool(ch.get("skip_hint")), "status": status})
    out.sort(key=lambda c: (c["start"], c["end"]))
    fixed: list[dict] = []
    for ch in out:
        if fixed and ch["start"] <= fixed[-1]["end"]:
            ch["start"] = fixed[-1]["end"] + 1
            if ch["start"] > ch["end"]:
                continue
        fixed.append(ch)
    return fixed[:MAX_CHAPTERS]


async def detect_chapters(pages: list[str], target_lang: str, *, api_key: str,
                          anthropic_key: str | None = None, model: str) -> list[dict]:
    """One LLM call: propose the book's chapter structure as page ranges.
    Returns [{title, start, end, skip_hint, status}] (may be empty — the user
    can still define chapters by hand in the review UI)."""
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    skeleton = build_skeleton(pages)
    if not skeleton.strip():
        return []
    prompt = _build_chapter_prompt(target_lang, skeleton, len(pages))
    raw = await llm.call(prompt, model=model, gemini_key=api_key,
                         anthropic_key=anthropic_key)
    return _norm_chapters(_parse_json(raw) or {}, len(pages))


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
