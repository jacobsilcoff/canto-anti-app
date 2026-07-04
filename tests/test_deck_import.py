import os
import sqlite3
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
async def test_import_on_legacy_global_unique_labels(tmp_path):
    """Legacy single-user DBs had a GLOBAL UNIQUE(name) on labels. The creator
    owns the "📦 Deck" label, so an importer's INSERT used to be silently ignored
    and the per-user lookup returned None → 'NoneType is not subscriptable'.
    init() must rebuild the table to per-user uniqueness, and import must succeed."""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_text TEXT NOT NULL, target_text TEXT NOT NULL,
            romanization TEXT NOT NULL DEFAULT '', audio_data BLOB, notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE card_labels (
            card_id INTEGER NOT NULL, label_id INTEGER NOT NULL,
            PRIMARY KEY (card_id, label_id),
            FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE,
            FOREIGN KEY (label_id) REFERENCES labels(id) ON DELETE CASCADE
        );
        INSERT INTO cards (id, source_text, target_text, romanization) VALUES (1,'old','舊','gau6');
        INSERT INTO labels (id, name) VALUES (7,'verbs');
        INSERT INTO card_labels (card_id, label_id) VALUES (1,7);
        """
    )
    conn.commit()
    conn.close()

    db.DB_PATH = path
    await db.init()  # must rebuild labels, dropping the global UNIQUE(name)

    # Legacy label + its card link survive the rebuild, now owned by admin (1).
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT user_id FROM labels WHERE id=7").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM card_labels").fetchone()[0] == 1
    conn.close()

    creator = await db.bootstrap_admin("creator", auth.hash_password("pw"))
    importer = await db.create_user("importer", auth.hash_password("pw"))
    cid = await db.create_card(creator, "hello", "你好", "nei5 hou2", target_lang="yue")
    deck_id = await db.create_shared_deck(
        creator, "Greetings", "", "yue", "public",
        items=[{"source_text": "hello", "target_text": "你好", "romanization": "nei5 hou2"}],
    )
    await db.label_cards_for_deck(creator, "Greetings", [cid])

    res = await db.import_deck(importer, deck_id)
    assert res["ok"] and res["created"] == 1
    # Both users now own their own per-user "📦 Greetings" label.
    cards = await db.get_all_cards(importer)
    assert [c["target_text"] for c in cards] == ["你好"]


@pytest.mark.asyncio
async def test_import_item_without_romanization(two_users):
    """A deck item with no romanization (None) must import — cards.romanization
    is NOT NULL DEFAULT '', so the bulk insert must coalesce None to ''."""
    creator, importer = two_users
    deck_id = await db.create_shared_deck(
        creator, "NoRoman", "", "yue", "public",
        items=[{"source_text": "hi", "target_text": "你好"}],  # no romanization key
    )
    res = await db.import_deck(importer, deck_id)
    assert res["ok"] and res["created"] == 1
    cards = await db.get_all_cards(importer)
    assert [c["target_text"] for c in cards] == ["你好"]
    assert cards[0]["romanization"] == ""


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
