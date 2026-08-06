"""Chapter → vocab flashcard deck (textbook.extract_chapter_vocab + routes)."""
import os
import time

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DB_PATH", "/tmp/test_vocab_deck_cards.db")
os.environ.setdefault("API_KEY_ENC_KEY", Fernet.generate_key().decode())

import auth
import db
import main
import textbook


# ── Pure extraction / normalization ──────────────────────────────────────────

def test_norm_vocab_filters_and_dedups():
    parsed = {"vocab": [
        {"target": "食飯", "gloss": "to eat a meal", "cefr": "A1"},
        {"target": "食飯", "gloss": "duplicate", "cefr": "A1"},   # dropped (dup)
        {"target": "", "gloss": "no target"},                     # dropped
        {"target": "貴", "gloss": ""},                            # dropped (no gloss)
        {"target": "埋單", "gloss": "the bill", "cefr": "bogus"},  # cefr nulled
        "not a dict",                                              # dropped
    ]}
    out = textbook._norm_vocab(parsed)
    assert [v["target"] for v in out] == ["食飯", "埋單"]
    assert out[0]["cefr"] == "A1"
    assert out[1]["cefr"] is None


@pytest.mark.asyncio
async def test_extract_chapter_vocab_dedups_across_chunks(monkeypatch):
    calls = {"n": 0}

    async def fake_call(prompt, **kwargs):
        calls["n"] += 1
        # Same word appears in both chunks — must be deduped to one item.
        return (
            '{"vocab": [{"target": "水", "gloss": "water", "cefr": "A1"},'
            ' {"target": "茶", "gloss": "tea", "cefr": "A1"}]}'
        )

    monkeypatch.setattr(textbook.llm, "call", fake_call)
    # Force two chunks by exceeding CHUNK_CHARS.
    source = ("line\n" * 4000)
    result = await textbook.extract_chapter_vocab(
        source, "yue", "Book", api_key="k", model="m")
    assert result["llm_calls"] >= 2
    assert sorted(v["target"] for v in result["items"]) == ["水", "茶"]


@pytest.mark.asyncio
async def test_extract_chapter_vocab_rejects_empty_source():
    with pytest.raises(ValueError):
        await textbook.extract_chapter_vocab("  ", "yue", "Book", api_key="k", model="m")


# ── Route: commit a reviewed vocab list into a studyable deck ─────────────────

@pytest_asyncio.fixture(autouse=True)
async def disable_rate_limits():
    original = main.limiter.enabled
    main.limiter.enabled = False
    yield
    main.limiter.enabled = original
    main.limiter._storage.reset()


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    user_id = await db.create_user("learner", auth.hash_password("password1"))
    session = "vocab-deck-test-session"
    await db.create_session(session, user_id, time.time() + 3600)
    # No Gemini key in tests. The route must still work: it resolves the key
    # best-effort and just skips the background embedding (see the no-key test).
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="https://test") as ac:
        ac.cookies.set("session", session)
        ac._user_id = user_id
        yield ac


@pytest.mark.asyncio
async def test_vocab_deck_creates_cards_under_label(client):
    payload = {
        "lang": "yue",
        "deck_name": "Ch.3 · Ordering food",
        "items": [
            {"target_text": "食飯", "source_text": "to eat a meal", "cefr_level": "A1"},
            {"target_text": "埋單", "source_text": "the bill", "cefr_level": "A2",
             "notes": "唔該埋單"},
        ],
    }
    res = await client.post("/api/vocab-deck", json=payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] == 2
    assert body["skipped"] == 0
    assert body["deck_id"] is None
    assert isinstance(body["label_id"], int)

    # The label is named 📕 <deck> and the cards are studyable via it.
    cards = await db.get_all_cards_basic(client._user_id, "yue")
    targets = {c["target_text"] for c in cards}
    assert {"食飯", "埋單"} <= targets
    # Romanization was filled by the offline oracle (jyutping) — not blank.
    faan = next(c for c in cards if c["target_text"] == "食飯")
    assert faan["romanization"].strip()


@pytest.mark.asyncio
async def test_vocab_deck_skips_words_already_in_deck(client):
    payload = {
        "lang": "yue",
        "deck_name": "Dupes",
        "items": [{"target_text": "貓", "source_text": "cat"}],
    }
    first = (await client.post("/api/vocab-deck", json=payload)).json()
    assert first["created"] == 1
    # Same word again → skipped, no duplicate card.
    second = (await client.post("/api/vocab-deck", json=payload)).json()
    assert second["created"] == 0
    assert second["skipped"] == 1


@pytest.mark.asyncio
async def test_vocab_deck_optionally_snapshots_shared_deck(client):
    payload = {
        "lang": "yue",
        "deck_name": "Shareable",
        "save_shared": True,
        "items": [{"target_text": "書", "source_text": "book"}],
    }
    body = (await client.post("/api/vocab-deck", json=payload)).json()
    assert body["created"] == 1
    assert isinstance(body["deck_id"], int)


@pytest.mark.asyncio
async def test_vocab_deck_works_without_a_gemini_key(client, monkeypatch):
    """Building a vocab deck needs NO AI — a label plus cards, audio generated
    lazily on first play. The key is used only for the background embedding, so
    a missing/unusable one must not fail the request.

    It used to: _resolve_gemini was called eagerly and raises 503 when no shared
    key is configured, so a fresh self-hosted instance (and CI) got
    "Shared API key is not configured." instead of a deck. The try/except that
    was supposed to cover this sat around background_tasks.add_task — one level
    below where the raise actually happened.
    """
    from fastapi import HTTPException

    async def no_key(*a, **k):
        raise HTTPException(503, "Shared API key is not configured.")
    monkeypatch.setattr(main, "_resolve_gemini", no_key)

    res = await client.post("/api/vocab-deck", json={
        "lang": "yue", "deck_name": "No key",
        "items": [{"target_text": "水", "source_text": "water"}]})
    assert res.status_code == 200, res.text
    assert res.json()["created"] == 1
    cards = await db.get_all_cards_basic(client._user_id, "yue")
    assert "水" in {c["target_text"] for c in cards}


@pytest.mark.asyncio
async def test_vocab_deck_rejects_empty_selection(client):
    res = await client.post("/api/vocab-deck", json={
        "lang": "yue", "deck_name": "Empty", "items": []})
    assert res.status_code == 400
