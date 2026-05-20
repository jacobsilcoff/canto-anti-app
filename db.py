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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS card_faces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                face TEXT NOT NULL,
                next_review TEXT DEFAULT (datetime('now')),
                interval_days INTEGER DEFAULT 1,
                ease_factor REAL DEFAULT 2.5,
                repetitions INTEGER DEFAULT 0,
                UNIQUE(card_id, face)
            )
        """)
        # Backfill face rows for any cards that don't have them yet
        for face in ('english', 'chinese', 'cantonese'):
            await db.execute(
                "INSERT OR IGNORE INTO card_faces (card_id, face) SELECT id, ? FROM cards",
                (face,),
            )
        await db.commit()


async def create_card(english: str, chinese: str, jyutping: str, audio_data: bytes | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO cards (english, chinese, jyutping, audio_data) VALUES (?, ?, ?, ?)",
            (english, chinese, jyutping, audio_data),
        )
        card_id = cursor.lastrowid
        for face in ('english', 'chinese', 'cantonese'):
            await db.execute(
                "INSERT INTO card_faces (card_id, face) VALUES (?, ?)",
                (card_id, face),
            )
        await db.commit()
        return card_id


async def get_card(card_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, english, chinese, jyutping FROM cards WHERE id = ?",
            (card_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_face_state(card_id: int, face: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT interval_days, ease_factor, repetitions FROM card_faces WHERE card_id=? AND face=?",
            (card_id, face),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_due_faces() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT cf.id AS face_id, cf.card_id, cf.face, cf.next_review,
                   cf.interval_days, cf.ease_factor, cf.repetitions,
                   c.english, c.chinese, c.jyutping
            FROM card_faces cf
            JOIN cards c ON c.id = cf.card_id
            WHERE cf.next_review <= datetime('now')
            ORDER BY cf.next_review ASC
        """) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_faces() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT cf.id AS face_id, cf.card_id, cf.face, cf.next_review,
                   cf.interval_days, cf.ease_factor, cf.repetitions,
                   c.english, c.chinese, c.jyutping
            FROM card_faces cf
            JOIN cards c ON c.id = cf.card_id
            ORDER BY cf.next_review ASC
        """) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_cards() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, english, chinese, jyutping, created_at FROM cards ORDER BY created_at DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_due_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM card_faces WHERE next_review <= datetime('now')"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_audio(card_id: int) -> bytes | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT audio_data FROM cards WHERE id = ?", (card_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def update_face_review(card_id: int, face: str, state: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE card_faces SET interval_days=?, ease_factor=?, repetitions=?, next_review=?
               WHERE card_id=? AND face=?""",
            (state["interval_days"], state["ease_factor"], state["repetitions"], state["next_review"],
             card_id, face),
        )
        await db.commit()


async def update_card(card_id: int, english: str, chinese: str, jyutping: str, audio_data: bytes | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if audio_data is not None:
            await db.execute(
                "UPDATE cards SET english=?, chinese=?, jyutping=?, audio_data=? WHERE id=?",
                (english, chinese, jyutping, audio_data, card_id),
            )
        else:
            await db.execute(
                "UPDATE cards SET english=?, chinese=?, jyutping=? WHERE id=?",
                (english, chinese, jyutping, card_id),
            )
        await db.commit()


async def delete_card(card_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM card_faces WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        await db.commit()
