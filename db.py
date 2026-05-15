import os
import aiosqlite

DB_PATH = os.getenv("DB_PATH", "data/cards.db")


async def init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                english TEXT NOT NULL,
                chinese TEXT NOT NULL,
                jyutping TEXT NOT NULL,
                audio_data BLOB,
                created_at TEXT DEFAULT (datetime('now')),
                next_review TEXT DEFAULT (datetime('now')),
                interval_days INTEGER DEFAULT 1,
                ease_factor REAL DEFAULT 2.5,
                repetitions INTEGER DEFAULT 0
            )
        """)
        await db.commit()


async def create_card(english: str, chinese: str, jyutping: str, audio_data: bytes | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO cards (english, chinese, jyutping, audio_data) VALUES (?, ?, ?, ?)",
            (english, chinese, jyutping, audio_data),
        )
        await db.commit()
        return cursor.lastrowid


async def get_card(card_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, english, chinese, jyutping, interval_days, ease_factor, repetitions, next_review FROM cards WHERE id = ?",
            (card_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_due_cards() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, english, chinese, jyutping, interval_days, ease_factor, repetitions, next_review
               FROM cards WHERE next_review <= datetime('now') ORDER BY next_review ASC"""
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_cards() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, english, chinese, jyutping, interval_days, ease_factor, repetitions, next_review, created_at FROM cards ORDER BY created_at DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_due_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM cards WHERE next_review <= datetime('now')") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_audio(card_id: int) -> bytes | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT audio_data FROM cards WHERE id = ?", (card_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def update_card_review(card_id: int, state: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cards SET interval_days=?, ease_factor=?, repetitions=?, next_review=? WHERE id=?",
            (state["interval_days"], state["ease_factor"], state["repetitions"], state["next_review"], card_id),
        )
        await db.commit()


async def delete_card(card_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        await db.commit()
