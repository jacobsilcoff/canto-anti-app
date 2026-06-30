import os
import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_cards.db")

import auth
import db


@pytest_asyncio.fixture
async def two_users(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    creator = await db.bootstrap_admin("creator", auth.hash_password("pw"))
    importer = await db.create_user("importer", auth.hash_password("pw"))
    return creator, importer


async def _make_deck(creator):
    return await db.create_shared_deck(
        creator, "Food", "tasty words", "yue", "public",
        items=[
            {"source_text": "apple", "target_text": "蘋果", "romanization": "ping4 gwo2"},
            {"source_text": "water", "target_text": "水", "romanization": "seoi2"},
        ],
    )


@pytest.mark.asyncio
async def test_import_creates_cards_and_label(two_users):
    creator, importer = two_users
    deck_id = await _make_deck(creator)

    res = await db.import_deck(importer, deck_id)
    assert res["ok"] and res["created"] == 2

    cards = await db.get_all_cards(importer)
    assert {c["target_text"] for c in cards} == {"蘋果", "水"}


@pytest.mark.asyncio
async def test_unimport_deletes_imported_cards(two_users):
    creator, importer = two_users
    deck_id = await _make_deck(creator)
    await db.import_deck(importer, deck_id)

    out = await db.unimport_deck(importer, deck_id)
    assert out["ok"] and out["removed"] == 2

    cards = await db.get_all_cards(importer)
    assert cards == []
    # The deck no longer counts as imported.
    decks = await db.list_community_decks(importer)
    assert all(not d["imported"] for d in decks)


@pytest.mark.asyncio
async def test_unimport_keeps_preexisting_card(two_users):
    creator, importer = two_users
    deck_id = await _make_deck(creator)

    # Importer already had "水" before importing the deck.
    pre_id = await db.create_card(importer, "water", "水", "seoi2", target_lang="yue")
    await db.import_deck(importer, deck_id)

    out = await db.unimport_deck(importer, deck_id)
    # Only the import-created "蘋果" is removed; the pre-existing "水" stays.
    assert out["removed"] == 1
    cards = await db.get_all_cards(importer)
    assert [c["target_text"] for c in cards] == ["水"]
    assert any(c["id"] == pre_id for c in cards)


@pytest.mark.asyncio
async def test_unimport_not_imported_errors(two_users):
    creator, importer = two_users
    deck_id = await _make_deck(creator)
    out = await db.unimport_deck(importer, deck_id)
    assert not out["ok"]


@pytest.mark.asyncio
async def test_import_large_deck_bulk_path(two_users):
    """A 2000-card deck must import via the set-based bulk path (not time out)."""
    creator, importer = two_users
    items = [
        {"source_text": f"word {i}", "target_text": f"字{i}",
         "romanization": f"zi{i}"}
        for i in range(2000)
    ]
    deck_id = await db.create_shared_deck(
        creator, "Big", "many words", "yue", "public", items=items)

    res = await db.import_deck(importer, deck_id)
    assert res["ok"] and res["created"] == 2000 and res["total"] == 2000

    cards = await db.get_all_cards(importer)
    assert len(cards) == 2000
    # Every imported card got its three SRS faces.
    faces = await db.get_due_faces(importer)
    assert len(faces) == 2000 * len(db.FACES)

    # Un-import cleanly removes all 2000.
    out = await db.unimport_deck(importer, deck_id)
    assert out["removed"] == 2000
    assert await db.get_all_cards(importer) == []


@pytest.mark.asyncio
async def test_import_dedupes_repeated_targets(two_users):
    """Duplicate target+lang within a deck collapses to one card."""
    creator, importer = two_users
    items = [
        {"source_text": "hi", "target_text": "你好", "romanization": "nei5 hou2"},
        {"source_text": "hello", "target_text": "你好", "romanization": "nei5 hou2"},
    ]
    deck_id = await db.create_shared_deck(
        creator, "Dup", "", "yue", "public", items=items)
    res = await db.import_deck(importer, deck_id)
    assert res["created"] == 1
    assert len(await db.get_all_cards(importer)) == 1
