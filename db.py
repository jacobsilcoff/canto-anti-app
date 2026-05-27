import os
import aiosqlite

DB_PATH = os.getenv("DB_PATH", "data/cards.db")


async def _column_exists(db, table: str, column: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any(r[1] == column for r in rows)


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
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                next_review TEXT DEFAULT (datetime('now')),
                interval_days INTEGER DEFAULT 1,
                ease_factor REAL DEFAULT 2.5,
                repetitions INTEGER DEFAULT 0
            )
        """)
        for col, defval in [
            ("notes", "NULL"),
            ("priority", "3"),
            ("tutor_flag", "0"),
            ("suspended", "0"),
        ]:
            if not await _column_exists(db, "cards", col):
                await db.execute(f"ALTER TABLE cards ADD COLUMN {col} {'TEXT' if col == 'notes' else 'INTEGER'} DEFAULT {defval}")

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
        if not await _column_exists(db, "card_faces", "first_seen_date"):
            await db.execute("ALTER TABLE card_faces ADD COLUMN first_seen_date TEXT")
            # Backfill: mark already-reviewed faces as seen (use a past date so they don't count today)
            await db.execute(
                "UPDATE card_faces SET first_seen_date = date('now', '-1 day') WHERE repetitions > 0"
            )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS card_labels (
                card_id INTEGER NOT NULL,
                label_id INTEGER NOT NULL,
                PRIMARY KEY (card_id, label_id),
                FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE,
                FOREIGN KEY (label_id) REFERENCES labels(id) ON DELETE CASCADE
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_card_labels_label ON card_labels(label_id)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Backfill face rows for any cards that don't have them yet
        for face in ('english', 'chinese', 'cantonese'):
            await db.execute(
                "INSERT OR IGNORE INTO card_faces (card_id, face) SELECT id, ? FROM cards",
                (face,),
            )
        await db.commit()


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        await db.commit()


# ── Cards ─────────────────────────────────────────────────────────────────────

async def create_card(
    english: str,
    chinese: str,
    jyutping: str,
    audio_data: bytes | None = None,
    notes: str | None = None,
    label_ids: list[int] | None = None,
    priority: int = 3,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO cards (english, chinese, jyutping, audio_data, notes, priority) VALUES (?, ?, ?, ?, ?, ?)",
            (english, chinese, jyutping, audio_data, notes, max(1, min(5, priority))),
        )
        card_id = cursor.lastrowid
        for face in ('english', 'chinese', 'cantonese'):
            await db.execute(
                "INSERT INTO card_faces (card_id, face) VALUES (?, ?)",
                (card_id, face),
            )
        if label_ids:
            await db.executemany(
                "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
                [(card_id, lid) for lid in label_ids],
            )
        await db.commit()
        return card_id


async def get_card(card_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, english, chinese, jyutping, notes, priority, tutor_flag, suspended FROM cards WHERE id = ?",
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


def _faces_query(where_extra: str = "", params: tuple = (), order_by: str = "cf.next_review ASC"):
    return (
        f"""
        SELECT cf.id AS face_id, cf.card_id, cf.face, cf.next_review,
               cf.interval_days, cf.ease_factor, cf.repetitions, cf.first_seen_date,
               c.english, c.chinese, c.jyutping, c.notes,
               c.priority, c.tutor_flag
        FROM card_faces cf
        JOIN cards c ON c.id = cf.card_id
        WHERE c.suspended = 0 {where_extra}
        ORDER BY {order_by}
        """,
        params,
    )


async def _faces_with_labels(rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    card_ids = {r["card_id"] for r in rows}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(card_ids))
        async with db.execute(
            f"""SELECT cl.card_id, l.id, l.name
                FROM card_labels cl JOIN labels l ON l.id = cl.label_id
                WHERE cl.card_id IN ({placeholders})""",
            tuple(card_ids),
        ) as cur:
            label_rows = await cur.fetchall()
    by_card: dict[int, list[dict]] = {}
    for lr in label_rows:
        by_card.setdefault(lr["card_id"], []).append({"id": lr["id"], "name": lr["name"]})
    for r in rows:
        r["labels"] = by_card.get(r["card_id"], [])
    return rows


async def get_study_session(label_id: int | None = None) -> dict:
    """Return due reviews + new cards up to the daily cap, with stats."""
    cap = int(await get_setting("new_cards_per_day") or 20)

    label_filter = ""
    label_params: tuple = ()
    if label_id is not None:
        label_filter = "AND cf.card_id IN (SELECT card_id FROM card_labels WHERE label_id=?)"
        label_params = (label_id,)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Due reviews: previously seen, due now, not suspended
        review_sql = f"""
            SELECT cf.id AS face_id, cf.card_id, cf.face, cf.next_review,
                   cf.interval_days, cf.ease_factor, cf.repetitions, cf.first_seen_date,
                   c.english, c.chinese, c.jyutping, c.notes,
                   c.priority, c.tutor_flag
            FROM card_faces cf JOIN cards c ON c.id = cf.card_id
            WHERE cf.first_seen_date IS NOT NULL
              AND cf.next_review <= datetime('now')
              AND c.suspended = 0
              {label_filter}
            ORDER BY cf.next_review ASC
        """
        async with db.execute(review_sql, label_params) as cur:
            reviews = [dict(r) for r in await cur.fetchall()]

        # Count new faces already introduced today
        async with db.execute(
            "SELECT COUNT(*) FROM card_faces WHERE first_seen_date = date('now')"
        ) as cur:
            row = await cur.fetchone()
            daily_new_used = row[0] if row else 0

        remaining = max(0, cap - daily_new_used)

        new_faces = []
        if remaining > 0:
            new_sql = f"""
                SELECT cf.id AS face_id, cf.card_id, cf.face, cf.next_review,
                       cf.interval_days, cf.ease_factor, cf.repetitions, cf.first_seen_date,
                       c.english, c.chinese, c.jyutping, c.notes,
                       c.priority, c.tutor_flag
                FROM card_faces cf JOIN cards c ON c.id = cf.card_id
                WHERE cf.first_seen_date IS NULL
                  AND c.suspended = 0
                  {label_filter}
                ORDER BY c.priority DESC, c.id ASC
                LIMIT ?
            """
            async with db.execute(new_sql, label_params + (remaining,)) as cur:
                new_faces = [dict(r) for r in await cur.fetchall()]

    all_faces = await _faces_with_labels(reviews + new_faces)
    return {
        "cards": all_faces,
        "review_count": len(reviews),
        "new_count": len(new_faces),
        "daily_new_used": daily_new_used,
        "daily_new_limit": cap,
    }


async def get_due_faces(label_id: int | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if label_id is None:
            sql, params = _faces_query("AND cf.next_review <= datetime('now')")
        else:
            sql, params = _faces_query(
                "AND cf.next_review <= datetime('now') "
                "AND cf.card_id IN (SELECT card_id FROM card_labels WHERE label_id=?)",
                (label_id,),
            )
        async with db.execute(sql, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return await _faces_with_labels(rows)


async def get_all_faces(label_id: int | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if label_id is None:
            sql, params = _faces_query()
        else:
            sql, params = _faces_query(
                "AND cf.card_id IN (SELECT card_id FROM card_labels WHERE label_id=?)",
                (label_id,),
            )
        async with db.execute(sql, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return await _faces_with_labels(rows)


async def get_all_cards() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, english, chinese, jyutping, notes, created_at, priority, tutor_flag, suspended FROM cards ORDER BY created_at DESC"
        ) as cur:
            cards = [dict(r) for r in await cur.fetchall()]
        if not cards:
            return cards
        ids = [c["id"] for c in cards]
        placeholders = ",".join("?" * len(ids))
        async with db.execute(
            f"""SELECT cl.card_id, l.id, l.name
                FROM card_labels cl JOIN labels l ON l.id = cl.label_id
                WHERE cl.card_id IN ({placeholders})""",
            tuple(ids),
        ) as cur:
            label_rows = await cur.fetchall()
    by_card: dict[int, list[dict]] = {}
    for lr in label_rows:
        by_card.setdefault(lr["card_id"], []).append({"id": lr["id"], "name": lr["name"]})
    for c in cards:
        c["labels"] = by_card.get(c["id"], [])
    return cards


async def get_due_count(label_id: int | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        if label_id is None:
            async with db.execute(
                """SELECT COUNT(*) FROM card_faces cf JOIN cards c ON c.id = cf.card_id
                   WHERE cf.next_review <= datetime('now') AND c.suspended = 0"""
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                """SELECT COUNT(*) FROM card_faces cf JOIN cards c ON c.id = cf.card_id
                   WHERE cf.next_review <= datetime('now')
                   AND c.suspended = 0
                   AND cf.card_id IN (SELECT card_id FROM card_labels WHERE label_id=?)""",
                (label_id,),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0


async def get_audio(card_id: int) -> bytes | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT audio_data FROM cards WHERE id = ?", (card_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_audio(card_id: int, data: bytes):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE cards SET audio_data=? WHERE id=?", (data, card_id))
        await db.commit()


async def update_face_review(card_id: int, face: str, state: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE card_faces
               SET interval_days=?, ease_factor=?, repetitions=?, next_review=?,
                   first_seen_date = CASE WHEN first_seen_date IS NULL THEN date('now') ELSE first_seen_date END
               WHERE card_id=? AND face=?""",
            (state["interval_days"], state["ease_factor"], state["repetitions"], state["next_review"],
             card_id, face),
        )
        await db.commit()


async def update_card(
    card_id: int,
    english: str,
    chinese: str,
    jyutping: str,
    audio_data: bytes | None = None,
    notes: str | None = None,
    label_ids: list[int] | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        if audio_data is not None:
            await db.execute(
                "UPDATE cards SET english=?, chinese=?, jyutping=?, notes=?, audio_data=? WHERE id=?",
                (english, chinese, jyutping, notes, audio_data, card_id),
            )
        else:
            await db.execute(
                "UPDATE cards SET english=?, chinese=?, jyutping=?, notes=? WHERE id=?",
                (english, chinese, jyutping, notes, card_id),
            )
        if label_ids is not None:
            await db.execute("DELETE FROM card_labels WHERE card_id=?", (card_id,))
            if label_ids:
                await db.executemany(
                    "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
                    [(card_id, lid) for lid in label_ids],
                )
        await db.commit()


async def delete_card(card_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM card_faces WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM card_labels WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        await db.commit()


async def set_card_priority(card_id: int, priority: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cards SET priority=? WHERE id=?",
            (max(1, min(5, priority)), card_id),
        )
        await db.commit()


async def set_card_tutor_flag(card_id: int, flagged: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cards SET tutor_flag=? WHERE id=?",
            (1 if flagged else 0, card_id),
        )
        await db.commit()


async def set_card_suspended(card_id: int, suspended: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cards SET suspended=? WHERE id=?",
            (1 if suspended else 0, card_id),
        )
        await db.commit()


async def reset_card_to_new(card_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE card_faces
               SET repetitions=0, interval_days=1, ease_factor=2.5,
                   next_review=datetime('now'), first_seen_date=NULL
               WHERE card_id=?""",
            (card_id,),
        )
        await db.execute("UPDATE cards SET priority=1 WHERE id=?", (card_id,))
        await db.commit()


# ── Labels ────────────────────────────────────────────────────────────────────

async def list_labels() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT l.id, l.name, COUNT(cl.card_id) AS card_count
               FROM labels l
               LEFT JOIN card_labels cl ON cl.label_id = l.id
               GROUP BY l.id, l.name
               ORDER BY l.name COLLATE NOCASE"""
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def create_label(name: str) -> dict:
    name = name.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("INSERT OR IGNORE INTO labels (name) VALUES (?)", (name,))
        await db.commit()
        async with db.execute(
            "SELECT id, name FROM labels WHERE name = ? COLLATE NOCASE", (name,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def rename_label(label_id: int, name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("UPDATE labels SET name=? WHERE id=?", (name.strip(), label_id))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def delete_label(label_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM card_labels WHERE label_id=?", (label_id,))
        await db.execute("DELETE FROM labels WHERE id=?", (label_id,))
        await db.commit()
