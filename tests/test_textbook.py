"""Tests for the textbook-PDF → queued-lessons segmentation (textbook.py).

Deterministic parts only: chunking, strict normalization of the segmenter's
output, and cross-chunk unit merging (the LLM call is faked)."""
import json

import pytest

import textbook


# ── Chunking ──────────────────────────────────────────────────────────────────

def test_chunk_text_short_is_one_chunk():
    assert textbook.chunk_text("hello world") == ["hello world"]
    assert textbook.chunk_text("") == []


def test_chunk_text_splits_on_line_boundaries():
    text = "\n".join(f"line {i} " + "x" * 40 for i in range(400))   # ~19k chars
    chunks = textbook.chunk_text(text, size=9000)
    assert len(chunks) >= 2
    assert all(len(c) <= 9000 for c in chunks)
    # No content lost (chunks are stripped, so compare line sets).
    got = [ln for c in chunks for ln in c.split("\n")]
    assert [ln.strip() for ln in got] == [ln.strip() for ln in text.split("\n")]


# ── Normalization ─────────────────────────────────────────────────────────────

def test_norm_units_drops_malformed():
    parsed = {"units": [
        {"title": "Ch 1", "lessons": [
            {"title": "Good", "skill": {"kind": "grammar", "key": "k1", "label": "L", "gloss": "g"},
             "target_items": [{"label": "un", "gloss": "one"}], "source": "s"},
            {"title": "No skill or items", "skill": {}, "target_items": []},   # dropped
            "not a dict",                                                      # dropped
            {"title": "", "skill": {"kind": "bogus_kind", "label": ""},
             "target_items": [{"label": "deux", "gloss": "two"}]},  # label falls back to item
        ]},
        {"title": "", "lessons": [{"skill": {"label": "x"}}]},   # no unit title → dropped
        {"title": "Empty", "lessons": []},                        # no lessons → dropped
    ]}
    units = textbook._norm_units(parsed)
    assert len(units) == 1
    lessons = units[0]["lessons"]
    assert len(lessons) == 2
    assert lessons[0]["skill"]["kind"] == "grammar"
    # Unknown kind clamps to vocab; missing label falls back to the first item.
    assert lessons[1]["skill"]["kind"] == "vocab"
    assert lessons[1]["skill"]["label"] == "deux"
    assert lessons[1]["title"] == "deux"


# ── Segmentation orchestration (fake LLM) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_segment_textbook_merges_units_across_chunks(monkeypatch):
    # Force 2 chunks (> CHUNK_CHARS of text).
    text = "\n".join("paragraph " + "x" * 90 for _ in range(120))   # ~12k chars
    prompts = []

    async def fake_call(prompt, **kw):
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps({"units": [{"title": "Chapter 1", "lessons": [
                {"title": "A", "skill": {"kind": "vocab", "key": "a", "label": "aa", "gloss": "ga"},
                 "target_items": [{"label": "aa", "gloss": "ga"}], "source": "sA"},
            ]}]})
        # Same unit title continues across the chunk boundary → merged.
        return json.dumps({"units": [
            {"title": "chapter 1", "lessons": [
                {"title": "B", "skill": {"kind": "grammar", "key": "b", "label": "bb", "gloss": "gb"},
                 "target_items": [], "source": "sB"},
            ]},
            {"title": "Chapter 2", "lessons": [
                {"title": "C", "skill": {"kind": "vocab", "key": "c", "label": "cc", "gloss": "gc"},
                 "target_items": [{"label": "cc", "gloss": "gc"}], "source": "sC"},
            ]},
        ]})
    monkeypatch.setattr(textbook.llm, "call", fake_call)

    seg = await textbook.segment_textbook(text, "fr", api_key="x", model="m")
    assert seg["llm_calls"] == 2
    assert seg["units"] == ["Chapter 1", "Chapter 2"]
    # Chapter 1 merged across chunks (case-insensitive title match) → size 2.
    assert [(i["unit_title"], i["unit_size"]) for i in seg["items"]] == [
        ("Chapter 1", 2), ("Chapter 1", 2), ("Chapter 2", 1)]
    assert seg["items"][1]["source"] == "sB"
    assert seg["items"][2]["spec"]["skill"]["key"] == "c"
    # The second prompt tells the model which units already exist.
    assert "Units already extracted" in prompts[1]
    assert "Chapter 1" in prompts[1]


@pytest.mark.asyncio
async def test_segment_textbook_caps_lessons(monkeypatch):
    many = [{"title": f"L{i}", "skill": {"kind": "vocab", "key": f"k{i}", "label": f"w{i}", "gloss": "g"},
             "target_items": [{"label": f"w{i}", "gloss": "g"}], "source": ""}
            for i in range(60)]

    async def fake_call(prompt, **kw):
        return json.dumps({"units": [{"title": "Big", "lessons": many}]})
    monkeypatch.setattr(textbook.llm, "call", fake_call)

    seg = await textbook.segment_textbook("short text", "fr", api_key="x", model="m")
    assert len(seg["items"]) == textbook.MAX_LESSONS


@pytest.mark.asyncio
async def test_segment_textbook_empty_result(monkeypatch):
    async def fake_call(prompt, **kw):
        return json.dumps({"units": []})
    monkeypatch.setattr(textbook.llm, "call", fake_call)
    seg = await textbook.segment_textbook("nothing teachable here", "fr", api_key="x", model="m")
    assert seg["items"] == [] and seg["llm_calls"] == 1
