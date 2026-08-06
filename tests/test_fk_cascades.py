"""The schema's ON DELETE CASCADE clauses must actually fire.

`foreign_keys` is a per-CONNECTION pragma that SQLite defaults to OFF, and db.py
opens a fresh connection per call. Setting it once in `init()` therefore armed
nothing: every cascade in the schema was decorative, and any delete path that
didn't hand-clean its children leaked rows forever.

These tests pin the pragma to the connection helper rather than to any one
delete function, so a new `db.connect()` caller can't quietly reintroduce it.
"""
import os
import pytest
import pytest_asyncio

os.environ.setdefault("DB_PATH", "/tmp/test_fk_cascades.db")

import auth
import db


@pytest_asyncio.fixture
async def fresh_db(tmp_path):
    db.DB_PATH = str(tmp_path / "cards.db")
    await db.init()
    return await db.bootstrap_admin("jsilcoff", auth.hash_password("test-password"))


async def _count(table: str, deck_id: int) -> int:
    async with db.connect() as conn:
        async with conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE deck_id=?", (deck_id,)
        ) as cur:
            return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_connect_enables_foreign_keys(fresh_db):
    """Every connection from the helper enforces FKs — not just init()'s."""
    async with db.connect() as conn:
        async with conn.execute("PRAGMA foreign_keys") as cur:
            assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_deleting_a_shared_deck_takes_its_children_with_it(fresh_db):
    """delete_shared_deck removes only the deck row and its label; the four
    child tables are cleaned up by the schema's cascades. Before the pragma was
    fixed these rows survived the deck forever."""
    user_id = fresh_db
    deck_id = await db.create_shared_deck(
        user_id, "Deck", "desc", "yue", "public",
        [{"source_text": "hello", "target_text": "你好", "romanization": "nei5 hou2"}],
    )
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO deck_imports (user_id, deck_id) VALUES (?, ?)", (user_id, deck_id)
        )
        await conn.execute(
            "INSERT INTO deck_ratings (deck_id, user_id, rating) VALUES (?, ?, 5)",
            (deck_id, user_id),
        )
        await conn.commit()

    for table in ("shared_deck_items", "deck_imports", "deck_ratings"):
        assert await _count(table, deck_id) > 0, f"{table} not seeded"

    assert await db.delete_shared_deck(user_id, deck_id) is True

    for table in ("shared_deck_items", "deck_imports", "deck_ratings"):
        assert await _count(table, deck_id) == 0, f"{table} leaked after deck delete"


@pytest.mark.asyncio
async def test_api_tokens_go_when_the_user_does(fresh_db):
    """api_tokens declares ON DELETE CASCADE on users(id) and nothing hand-cleans
    it, so this is entirely dependent on the pragma being on."""
    user_id = fresh_db
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO api_tokens (token_hash, user_id, label) VALUES (?, ?, ?)",
            ("deadbeef", user_id, "glasses"),
        )
        await conn.commit()

    await db.delete_user(user_id)

    async with db.connect() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM api_tokens WHERE user_id=?", (user_id,)
        ) as cur:
            assert (await cur.fetchone())[0] == 0
