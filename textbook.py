"""Textbook PDF → queued interactive lessons.

Pipeline: the route extracts the PDF's text (extract.extract_pdf, deterministic),
then `segment_textbook` runs one LLM call per ~9k-char chunk to carve the book
into TEACHABLE LESSON SPECS grouped into units (the book's own chapters). Each
spec carries condensed `source` notes (the chunk's rules / example sentences /
vocab lists for that lesson) so the normal lesson author can GROUND the teach
blocks + drills in the book's own material later.

The specs are stored in `lesson_queue` (db.add_lesson_queue) and consumed FIFO
by main._author_next_lesson: while the queue is non-empty the per-lesson planner
is SKIPPED (the book is the plan) and author_lesson receives the item's `source`.
Authoring stays one-lesson-per-request, so a 30-lesson book never runs 30 LLM
calls in one request — the existing "+ Add lesson" / auto-buffer flow drains the
queue lesson by lesson.

Segmentation output is strictly normalized here (same trust model as the
planner): malformed units/lessons are dropped, counts are capped, and keys are
synthesized downstream by main._concepts_from_spec when missing.
"""
import llm
from translation import LANG_INFO, _parse_json
from learning import _lang_preamble

# The reader's PDF import caps at 12k chars (per-sentence translate/TTS is the
# cost there); a textbook is consumed as lesson SPECS, so we can afford much
# more text — but still bound it (~60k ≈ 25–40 book pages of prose).
MAX_TEXTBOOK_CHARS = 60_000
CHUNK_CHARS = 9_000          # one segmentation LLM call per chunk
MAX_LESSONS = 40             # queue cap per import
MAX_ITEMS_PER_LESSON = 10
_SOURCE_CAP = 1500           # per-lesson condensed source notes
_TITLE_CAP = 80


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split the extracted text into ~size-char chunks on line boundaries."""
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
                          prior_units: list[str]) -> str:
    info = LANG_INFO[target_lang]
    name = info.get("full_name", info["name"])
    prior = ""
    if prior_units:
        prior = (
            "Units already extracted from earlier parts of this book (CONTINUE the "
            "sequence; if this part continues the last unit, reuse its title EXACTLY "
            f"so the lessons group together): {', '.join(prior_units[-8:])}\n\n"
        )
    return (
        f"You are an expert {name} curriculum designer. Below is part {part}/{total} "
        f"of a {name} textbook / grammar book a learner uploaded. Segment it into "
        f"TEACHABLE LESSONS grouped into units, so our app can turn each one into an "
        f"interactive lesson with drills.\n\n"
        f"{_lang_preamble(info)}"
        f"{prior}"
        f"── RULES ──\n"
        f"• One LESSON = one satisfying, self-contained chunk: a grammar pattern "
        f"(kind \"grammar\") or a themed set of 4–8 words/phrases (kind \"vocab\"). "
        f"Follow the book's own progression and granularity — merge fragments that "
        f"belong together, split sections that cover two distinct skills.\n"
        f"• One UNIT = the book's chapter/section (a coherent arc of 2–6 lessons). "
        f"Use short English unit titles.\n"
        f"• skill.key = stable snake_case; skill.label = the pattern name (grammar) "
        f"or native theme word (vocab); gloss in English.\n"
        f"• target_items: for GRAMMAR the forms/verbs/particles to cover; for VOCAB "
        f"the words themselves. Native {name} script, citation form.\n"
        f"• source = condensed notes FROM THE TEXT for this lesson (≤1200 chars): "
        f"the rule as the book states it, its example sentences, its vocab list — "
        f"enough for another teacher to author the lesson faithfully to the book.\n"
        f"• SKIP non-teachable material: front matter, prefaces, exercise answer "
        f"keys, indexes, pronunciation-guide tables (our app teaches script/sounds "
        f"separately). If this part contains nothing teachable, return {{\"units\": []}}.\n\n"
        f"── TEXT (part {part}/{total}) ──\n{chunk}\n\n"
        f"Return ONLY valid JSON, no other text:\n"
        '{"units": [\n'
        '  {"title": "<short English unit title>",\n'
        '   "lessons": [\n'
        '     {"title": "<short English lesson title>",\n'
        '      "skill": {"kind": "grammar"|"vocab", "key": "<snake_case>", "label": "<pattern name OR native theme word>", "gloss": "<one-line English>"},\n'
        '      "target_items": [{"label": "<native script>", "gloss": "<English>"}, ...],\n'
        '      "source": "<condensed notes from the text>"}\n'
        '   ]}\n'
        ']}'
    )


def _norm_units(parsed: dict) -> list[dict]:
    """Strictly normalize one chunk's segmentation. Malformed entries are dropped
    (filter-then-clip, like the planner): a lesson needs a skill label or at least
    one target item; a unit needs a title and ≥1 surviving lesson."""
    out: list[dict] = []
    for u in (parsed.get("units") or []):
        if not isinstance(u, dict):
            continue
        title = (u.get("title") or "").strip()[:_TITLE_CAP]
        lessons = []
        for l in (u.get("lessons") or []):
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
            lessons.append({
                "title": (l.get("title") or "").strip()[:_TITLE_CAP] or label[:_TITLE_CAP],
                "skill": {"kind": kind, "key": (skill.get("key") or "").strip(),
                          "label": label, "gloss": (skill.get("gloss") or "").strip()},
                "target_items": items,
                "source": (l.get("source") or "").strip()[:_SOURCE_CAP],
            })
        if title and lessons:
            out.append({"title": title, "lessons": lessons})
    return out


async def segment_textbook(text: str, target_lang: str, *, api_key: str,
                           anthropic_key: str | None = None, model: str) -> dict:
    """Segment extracted textbook text into queued lesson specs.

    One LLM call per chunk; a chunk whose first unit continues the previous
    chunk's last unit (same title) is merged so book chapters spanning chunk
    boundaries stay one unit. Returns
      {"items": [{unit_title, unit_size, spec, source}, ...],
       "units": [unit titles], "llm_calls": n}
    """
    if target_lang not in LANG_INFO:
        raise ValueError(f"Unsupported target language: {target_lang}")
    chunks = chunk_text((text or "")[:MAX_TEXTBOOK_CHARS])
    units: list[dict] = []
    calls = 0
    for i, ch in enumerate(chunks):
        prompt = _build_segment_prompt(target_lang, ch, i + 1, len(chunks),
                                       [u["title"] for u in units])
        raw = await llm.call(prompt, model=model, gemini_key=api_key,
                             anthropic_key=anthropic_key)
        calls += 1
        for u in _norm_units(_parse_json(raw) or {}):
            if units and units[-1]["title"].casefold() == u["title"].casefold():
                units[-1]["lessons"].extend(u["lessons"])
            else:
                units.append(u)

    items: list[dict] = []
    for u in units:
        # unit_size = the chapter's lesson budget when generation opens it.
        size = len(u["lessons"])
        for l in u["lessons"]:
            if len(items) >= MAX_LESSONS:
                break
            items.append({
                "unit_title": u["title"],
                "unit_size": size,
                "spec": {"title": l["title"], "skill": l["skill"],
                         "target_items": l["target_items"]},
                "source": l["source"],
            })
    return {"items": items, "units": [u["title"] for u in units], "llm_calls": calls}
