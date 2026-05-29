import os
import aiosqlite

DB_PATH = os.getenv("DB_PATH", "data/cards.db")

# Card face values. 'source' = native-language text, 'target' = target-language text,
# 'pronunciation' = romanization (logographic) or audio-only (Latin script).
FACES = ("source", "target", "pronunciation")

SUPPORTED_LANGS = ("yue", "cmn", "fr", "es")
LOGOGRAPHIC_LANGS = {"yue", "cmn"}


async def _column_exists(db, table: str, column: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any(r[1] == column for r in rows)


async def _table_exists(db, table: str) -> bool:
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ) as cur:
        return await cur.fetchone() is not None


async def _migrate_legacy_schema(db):
    """One-time migration from old single-user schema to multi-user, multi-language."""
    # Rename legacy column names if they still exist.
    if await _column_exists(db, "cards", "english") and not await _column_exists(db, "cards", "source_text"):
        await db.execute("ALTER TABLE cards RENAME COLUMN english TO source_text")
    if await _column_exists(db, "cards", "chinese") and not await _column_exists(db, "cards", "target_text"):
        await db.execute("ALTER TABLE cards RENAME COLUMN chinese TO target_text")
    if await _column_exists(db, "cards", "jyutping") and not await _column_exists(db, "cards", "romanization"):
        await db.execute("ALTER TABLE cards RENAME COLUMN jyutping TO romanization")

    # Rename face values in card_faces.
    if await _table_exists(db, "card_faces"):
        await db.execute("UPDATE card_faces SET face='source' WHERE face='english'")
        await db.execute("UPDATE card_faces SET face='target' WHERE face='chinese'")
        await db.execute("UPDATE card_faces SET face='pronunciation' WHERE face='cantonese'")

    # Move legacy single-user settings table into user_settings under user_id=1 (the bootstrap admin).
    if await _table_exists(db, "settings") and not await _table_exists(db, "user_settings"):
        await db.execute("""
            CREATE TABLE user_settings (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
        """)
        # Defer copying rows until after we know there's a user; settings is preserved meanwhile.

    # Migrate labels.name UNIQUE constraint to (user_id, name) by recreating the table if needed.
    # We only recreate when the old constraint exists AND we have not yet added user_id.
    if await _table_exists(db, "labels") and not await _column_exists(db, "labels", "user_id"):
        await db.execute("ALTER TABLE labels ADD COLUMN user_id INTEGER")


async def init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        # Users.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                native_lang TEXT NOT NULL DEFAULT 'en',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Cards (legacy single-user table is named "cards"; we keep the name and migrate columns).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_text TEXT NOT NULL,
                target_text TEXT NOT NULL,
                romanization TEXT NOT NULL DEFAULT '',
                audio_data BLOB,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        await _migrate_legacy_schema(db)

        # Ensure all needed columns exist (for both fresh and migrated schemas).
        for col, sql_type, default in [
            ("priority", "INTEGER", "3"),
            ("tutor_flag", "INTEGER", "0"),
            ("suspended", "INTEGER", "0"),
            ("notes", "TEXT", "NULL"),
            ("target_lang", "TEXT", "'yue'"),
            ("user_id", "INTEGER", "NULL"),
            ("classifier", "TEXT", "''"),
            ("embedding", "TEXT", "NULL"),
            ("canonical_card_id", "INTEGER", "NULL"),
        ]:
            if not await _column_exists(db, "cards", col):
                await db.execute(f"ALTER TABLE cards ADD COLUMN {col} {sql_type} DEFAULT {default}")

        # Card faces.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS card_faces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                face TEXT NOT NULL,
                next_review TEXT DEFAULT (datetime('now')),
                interval_days INTEGER DEFAULT 1,
                ease_factor REAL DEFAULT 2.5,
                repetitions INTEGER DEFAULT 0,
                first_seen_date TEXT,
                UNIQUE(card_id, face)
            )
        """)
        if not await _column_exists(db, "card_faces", "first_seen_date"):
            await db.execute("ALTER TABLE card_faces ADD COLUMN first_seen_date TEXT")

        # Labels: per-user, unique within user.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                is_story_label INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Drop legacy UNIQUE(name) if present by recreating the table when needed.
        if not await _column_exists(db, "labels", "user_id"):
            await db.execute("ALTER TABLE labels ADD COLUMN user_id INTEGER")
        if not await _column_exists(db, "labels", "is_story_label"):
            await db.execute("ALTER TABLE labels ADD COLUMN is_story_label INTEGER NOT NULL DEFAULT 0")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_labels_user_name ON labels(user_id, name COLLATE NOCASE)"
        )

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

        # Per-user settings.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
        """)

        # Reader texts: AI-generated texts saved for re-reading.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reader_texts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                content TEXT NOT NULL,
                target_lang TEXT NOT NULL DEFAULT 'yue',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Daily study activity for streak tracking.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS study_activity (
                user_id INTEGER NOT NULL,
                study_date TEXT NOT NULL,
                PRIMARY KEY (user_id, study_date)
            )
        """)

        # Pre-generated sentence translations and audio for reader texts.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reader_sentences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text_id INTEGER NOT NULL,
                sentence_idx INTEGER NOT NULL,
                sentence_text TEXT NOT NULL,
                translation TEXT,
                audio_data BLOB,
                UNIQUE(text_id, sentence_idx)
            )
        """)

        # Backfill face rows for any cards that don't have them yet.
        for face in FACES:
            await db.execute(
                "INSERT OR IGNORE INTO card_faces (card_id, face) SELECT id, ? FROM cards",
                (face,),
            )
        await db.commit()


async def bootstrap_admin(username: str, password_hash: str) -> int:
    """Ensure an admin user exists. If no users, create with given creds and migrate existing data.
    Returns the admin's user_id.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            user_count = row[0]

        if user_count == 0:
            cursor = await db.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
                (username, password_hash),
            )
            admin_id = cursor.lastrowid

            # Migrate any pre-existing cards/labels to this user.
            await db.execute(
                "UPDATE cards SET user_id=? WHERE user_id IS NULL",
                (admin_id,),
            )
            await db.execute(
                "UPDATE labels SET user_id=? WHERE user_id IS NULL",
                (admin_id,),
            )
            # Move legacy single-user settings (if any) into user_settings under this admin.
            if await _table_exists(db, "settings"):
                async with db.execute("SELECT key, value FROM settings") as cur:
                    legacy = await cur.fetchall()
                for r in legacy:
                    await db.execute(
                        "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                        (admin_id, r["key"], r["value"]),
                    )
                await db.execute("DROP TABLE settings")
            await db.commit()
            return admin_id

        async with db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ) as cur:
            row = await cur.fetchone()
            return row["id"] if row else 0


# ── Users ─────────────────────────────────────────────────────────────────────

async def get_user_by_username(username: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, password_hash, is_admin, native_lang FROM users WHERE username=? COLLATE NOCASE",
            (username,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, is_admin, native_lang, created_at FROM users WHERE id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, is_admin, native_lang, created_at FROM users ORDER BY id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def create_user(username: str, password_hash: str, is_admin: bool = False) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (username, password_hash, 1 if is_admin else 0),
        )
        await db.commit()
        return cursor.lastrowid


async def delete_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        # Manually clean up since some legacy rows may have user_id but no FK.
        async with db.execute("SELECT id FROM cards WHERE user_id=?", (user_id,)) as cur:
            card_ids = [r[0] for r in await cur.fetchall()]
        if card_ids:
            placeholders = ",".join("?" * len(card_ids))
            await db.execute(f"DELETE FROM card_faces WHERE card_id IN ({placeholders})", card_ids)
            await db.execute(f"DELETE FROM card_labels WHERE card_id IN ({placeholders})", card_ids)
            await db.execute(f"DELETE FROM cards WHERE id IN ({placeholders})", card_ids)
        await db.execute("DELETE FROM labels WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM users WHERE id=?", (user_id,))
        await db.commit()


async def update_user_password(user_id: int, password_hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        await db.commit()


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_setting(user_id: int, key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(user_id: int, key: str, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
            (user_id, key, str(value)),
        )
        await db.commit()


# ── Cards ─────────────────────────────────────────────────────────────────────

async def get_or_create_label(user_id: int, name: str, is_story_label: bool = False) -> int:
    """Return the id of an existing label (case-insensitive) or create it."""
    name = name.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
            (user_id, name),
        ) as cur:
            row = await cur.fetchone()
        if row:
            return row[0]
        cursor = await db.execute(
            "INSERT INTO labels (user_id, name, is_story_label) VALUES (?, ?, ?)",
            (user_id, name, 1 if is_story_label else 0),
        )
        await db.commit()
        return cursor.lastrowid


async def create_card(
    user_id: int,
    source_text: str,
    target_text: str,
    romanization: str = "",
    target_lang: str = "yue",
    audio_data: bytes | None = None,
    notes: str | None = None,
    label_ids: list[int] | None = None,
    priority: int = 3,
    classifier: str = "",
    canonical_card_id: int | None = None,
    suggested_label_names: list[str] | None = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO cards (user_id, source_text, target_text, romanization, target_lang,
                                  audio_data, notes, priority, classifier, canonical_card_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, source_text, target_text, romanization, target_lang,
             audio_data, notes, max(1, min(5, priority)), classifier or "", canonical_card_id),
        )
        card_id = cursor.lastrowid
        for face in FACES:
            await db.execute(
                "INSERT INTO card_faces (card_id, face) VALUES (?, ?)",
                (card_id, face),
            )
        # Collect all label ids to assign.
        all_label_ids: list[int] = list(label_ids or [])

        # Auto-create and assign suggested labels.
        for name in (suggested_label_names or []):
            name = name.strip()
            if not name:
                continue
            await db.execute(
                "INSERT OR IGNORE INTO labels (user_id, name, is_story_label) VALUES (?, ?, 0)",
                (user_id, name),
            )
            async with db.execute(
                "SELECT id FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
                (user_id, name),
            ) as cur:
                row = await cur.fetchone()
            if row and row[0] not in all_label_ids:
                all_label_ids.append(row[0])

        if all_label_ids:
            await db.executemany(
                """INSERT OR IGNORE INTO card_labels (card_id, label_id)
                   SELECT ?, id FROM labels WHERE id=? AND user_id=?""",
                [(card_id, lid, user_id) for lid in all_label_ids],
            )
        await db.commit()
        return card_id


_CARD_COLS = "id, source_text, target_text, romanization, target_lang, notes, priority, tutor_flag, suspended, classifier, canonical_card_id"


async def get_card(user_id: int, card_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_CARD_COLS} FROM cards WHERE id = ? AND user_id = ?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_face_state(user_id: int, card_id: int, face: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT cf.interval_days, cf.ease_factor, cf.repetitions
               FROM card_faces cf JOIN cards c ON c.id = cf.card_id
               WHERE cf.card_id=? AND cf.face=? AND c.user_id=?""",
            (card_id, face, user_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


def _faces_query(extra_where: str = "", extra_params: tuple = (),
                 order_by: str = "cf.next_review ASC"):
    return (
        f"""
        SELECT cf.id AS face_id, cf.card_id, cf.face, cf.next_review,
               cf.interval_days, cf.ease_factor, cf.repetitions, cf.first_seen_date,
               c.source_text, c.target_text, c.romanization, c.target_lang, c.notes,
               c.priority, c.tutor_flag, c.classifier, c.canonical_card_id
        FROM card_faces cf
        JOIN cards c ON c.id = cf.card_id
        WHERE c.user_id = ? AND c.suspended = 0 {extra_where}
        ORDER BY {order_by}
        """,
        extra_params,
    )


async def _faces_with_labels(user_id: int, rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    card_ids = {r["card_id"] for r in rows}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(card_ids))
        async with db.execute(
            f"""SELECT cl.card_id, l.id, l.name
                FROM card_labels cl JOIN labels l ON l.id = cl.label_id
                WHERE cl.card_id IN ({placeholders}) AND l.user_id = ?""",
            tuple(card_ids) + (user_id,),
        ) as cur:
            label_rows = await cur.fetchall()
    by_card: dict[int, list[dict]] = {}
    for lr in label_rows:
        by_card.setdefault(lr["card_id"], []).append({"id": lr["id"], "name": lr["name"]})
    for r in rows:
        r["labels"] = by_card.get(r["card_id"], [])
    return rows


async def get_study_session(user_id: int, label_id: int | None = None) -> dict:
    """Return due reviews + new cards up to the daily cap, with stats."""
    cap = int(await get_setting(user_id, "new_cards_per_day") or 20)

    label_filter = ""
    label_params: tuple = ()
    if label_id is not None:
        label_filter = (
            "AND cf.card_id IN (SELECT cl.card_id FROM card_labels cl "
            "JOIN labels l ON l.id = cl.label_id "
            "WHERE cl.label_id=? AND l.user_id=?)"
        )
        label_params = (label_id, user_id)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        review_sql = f"""
            SELECT cf.id AS face_id, cf.card_id, cf.face, cf.next_review,
                   cf.interval_days, cf.ease_factor, cf.repetitions, cf.first_seen_date,
                   c.source_text, c.target_text, c.romanization, c.target_lang, c.notes,
                   c.priority, c.tutor_flag
            FROM card_faces cf JOIN cards c ON c.id = cf.card_id
            WHERE c.user_id = ?
              AND cf.first_seen_date IS NOT NULL
              AND cf.next_review <= datetime('now')
              AND c.suspended = 0
              {label_filter}
            ORDER BY cf.next_review ASC
        """
        async with db.execute(review_sql, (user_id,) + label_params) as cur:
            reviews = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            """SELECT COUNT(*) FROM card_faces cf JOIN cards c ON c.id = cf.card_id
               WHERE c.user_id = ? AND cf.first_seen_date = date('now')""",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            daily_new_used = row[0] if row else 0

        remaining = max(0, cap - daily_new_used)

        new_faces = []
        if remaining > 0:
            new_sql = f"""
                SELECT cf.id AS face_id, cf.card_id, cf.face, cf.next_review,
                       cf.interval_days, cf.ease_factor, cf.repetitions, cf.first_seen_date,
                       c.source_text, c.target_text, c.romanization, c.target_lang, c.notes,
                       c.priority, c.tutor_flag
                FROM card_faces cf JOIN cards c ON c.id = cf.card_id
                WHERE c.user_id = ?
                  AND cf.first_seen_date IS NULL
                  AND c.suspended = 0
                  {label_filter}
                ORDER BY c.priority DESC, c.id ASC
                LIMIT ?
            """
            async with db.execute(new_sql, (user_id,) + label_params + (remaining,)) as cur:
                new_faces = [dict(r) for r in await cur.fetchall()]

    all_faces = await _faces_with_labels(user_id, reviews + new_faces)
    return {
        "cards": all_faces,
        "review_count": len(reviews),
        "new_count": len(new_faces),
        "daily_new_used": daily_new_used,
        "daily_new_limit": cap,
    }


async def get_due_faces(user_id: int, label_id: int | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if label_id is None:
            sql, params = _faces_query("AND cf.next_review <= datetime('now')", (user_id,))
        else:
            sql, params = _faces_query(
                "AND cf.next_review <= datetime('now') "
                "AND cf.card_id IN (SELECT cl.card_id FROM card_labels cl "
                "JOIN labels l ON l.id = cl.label_id "
                "WHERE cl.label_id=? AND l.user_id=?)",
                (user_id, label_id, user_id),
            )
        async with db.execute(sql, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return await _faces_with_labels(user_id, rows)


async def get_all_faces(user_id: int, label_id: int | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if label_id is None:
            sql, params = _faces_query("", (user_id,))
        else:
            sql, params = _faces_query(
                "AND cf.card_id IN (SELECT cl.card_id FROM card_labels cl "
                "JOIN labels l ON l.id = cl.label_id "
                "WHERE cl.label_id=? AND l.user_id=?)",
                (user_id, label_id, user_id),
            )
        async with db.execute(sql, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return await _faces_with_labels(user_id, rows)


async def get_all_cards(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT {_CARD_COLS}, created_at FROM cards
                WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,),
        ) as cur:
            cards = [dict(r) for r in await cur.fetchall()]
        if not cards:
            return cards
        ids = [c["id"] for c in cards]
        placeholders = ",".join("?" * len(ids))
        async with db.execute(
            f"""SELECT cl.card_id, l.id, l.name
                FROM card_labels cl JOIN labels l ON l.id = cl.label_id
                WHERE cl.card_id IN ({placeholders}) AND l.user_id = ?""",
            tuple(ids) + (user_id,),
        ) as cur:
            label_rows = await cur.fetchall()
    by_card: dict[int, list[dict]] = {}
    for lr in label_rows:
        by_card.setdefault(lr["card_id"], []).append({"id": lr["id"], "name": lr["name"]})
    for c in cards:
        c["labels"] = by_card.get(c["id"], [])
    return cards


async def get_due_count(user_id: int, label_id: int | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        if label_id is None:
            async with db.execute(
                """SELECT COUNT(*) FROM card_faces cf JOIN cards c ON c.id = cf.card_id
                   WHERE c.user_id = ? AND cf.next_review <= datetime('now') AND c.suspended = 0""",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                """SELECT COUNT(*) FROM card_faces cf JOIN cards c ON c.id = cf.card_id
                   WHERE c.user_id = ?
                   AND cf.next_review <= datetime('now')
                   AND c.suspended = 0
                   AND cf.card_id IN (SELECT cl.card_id FROM card_labels cl
                                      JOIN labels l ON l.id = cl.label_id
                                      WHERE cl.label_id=? AND l.user_id=?)""",
                (user_id, label_id, user_id),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0


async def get_audio(user_id: int, card_id: int) -> bytes | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT audio_data FROM cards WHERE id = ? AND user_id = ?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_audio(user_id: int, card_id: int, data: bytes):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cards SET audio_data=? WHERE id=? AND user_id=?",
            (data, card_id, user_id),
        )
        await db.commit()


async def update_face_review(user_id: int, card_id: int, face: str, state: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        # Ensure the card belongs to this user.
        async with db.execute(
            "SELECT 1 FROM cards WHERE id=? AND user_id=?", (card_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return
        await db.execute(
            """UPDATE card_faces
               SET interval_days=?, ease_factor=?, repetitions=?, next_review=?,
                   first_seen_date = CASE WHEN first_seen_date IS NULL THEN date('now') ELSE first_seen_date END
               WHERE card_id=? AND face=?""",
            (state["interval_days"], state["ease_factor"], state["repetitions"], state["next_review"],
             card_id, face),
        )
        await db.execute(
            "INSERT OR IGNORE INTO study_activity (user_id, study_date) VALUES (?, date('now'))",
            (user_id,),
        )
        await db.commit()


async def update_card(
    user_id: int,
    card_id: int,
    source_text: str,
    target_text: str,
    romanization: str,
    audio_data: bytes | None = None,
    notes: str | None = None,
    label_ids: list[int] | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        # Confirm ownership before mutating.
        async with db.execute(
            "SELECT 1 FROM cards WHERE id=? AND user_id=?", (card_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return
        if audio_data is not None:
            await db.execute(
                """UPDATE cards SET source_text=?, target_text=?, romanization=?, notes=?, audio_data=?
                   WHERE id=?""",
                (source_text, target_text, romanization, notes, audio_data, card_id),
            )
        else:
            await db.execute(
                """UPDATE cards SET source_text=?, target_text=?, romanization=?, notes=?
                   WHERE id=?""",
                (source_text, target_text, romanization, notes, card_id),
            )
        if label_ids is not None:
            await db.execute("DELETE FROM card_labels WHERE card_id=?", (card_id,))
            if label_ids:
                await db.executemany(
                    """INSERT OR IGNORE INTO card_labels (card_id, label_id)
                       SELECT ?, id FROM labels WHERE id=? AND user_id=?""",
                    [(card_id, lid, user_id) for lid in label_ids],
                )
        await db.commit()


async def delete_card(user_id: int, card_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM cards WHERE id=? AND user_id=?", (card_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return
        await db.execute("DELETE FROM card_faces WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM card_labels WHERE card_id = ?", (card_id,))
        await db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        await db.commit()


async def set_card_priority(user_id: int, card_id: int, priority: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cards SET priority=? WHERE id=? AND user_id=?",
            (max(1, min(5, priority)), card_id, user_id),
        )
        await db.commit()


async def set_card_tutor_flag(user_id: int, card_id: int, flagged: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cards SET tutor_flag=? WHERE id=? AND user_id=?",
            (1 if flagged else 0, card_id, user_id),
        )
        await db.commit()


async def set_card_suspended(user_id: int, card_id: int, suspended: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cards SET suspended=? WHERE id=? AND user_id=?",
            (1 if suspended else 0, card_id, user_id),
        )
        await db.commit()


async def reset_card_to_new(user_id: int, card_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM cards WHERE id=? AND user_id=?", (card_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return
        await db.execute(
            """UPDATE card_faces
               SET repetitions=0, interval_days=1, ease_factor=2.5,
                   next_review=datetime('now'), first_seen_date=NULL
               WHERE card_id=?""",
            (card_id,),
        )
        await db.execute("UPDATE cards SET priority=1 WHERE id=?", (card_id,))
        await db.commit()


async def update_card_embedding(card_id: int, embedding_json: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE cards SET embedding=? WHERE id=?", (embedding_json, card_id))
        await db.commit()


async def get_all_embeddings(user_id: int) -> list[dict]:
    """Return all cards that have embeddings, for similarity search."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, source_text, target_text, embedding
               FROM cards WHERE user_id=? AND embedding IS NOT NULL""",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_canonical_card(user_id: int, card_id: int, canonical_id: int | None) -> bool:
    """Set (or clear) the canonical card pointer. Returns False if card not found."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM cards WHERE id=? AND user_id=?", (card_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return False
        await db.execute(
            "UPDATE cards SET canonical_card_id=? WHERE id=? AND user_id=?",
            (canonical_id, card_id, user_id),
        )
        await db.commit()
        return True


async def get_card_forms(user_id: int, canonical_card_id: int) -> list[dict]:
    """Return all cards that point to this canonical card."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT {_CARD_COLS} FROM cards
                WHERE user_id=? AND canonical_card_id=?
                ORDER BY id""",
            (user_id, canonical_card_id),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Labels ────────────────────────────────────────────────────────────────────

async def list_labels(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT l.id, l.name, l.is_story_label, COUNT(cl.card_id) AS card_count
               FROM labels l
               LEFT JOIN card_labels cl ON cl.label_id = l.id
               WHERE l.user_id = ?
               GROUP BY l.id, l.name, l.is_story_label
               ORDER BY l.name COLLATE NOCASE""",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def create_label(user_id: int, name: str, is_story_label: bool = False) -> dict:
    name = name.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR IGNORE INTO labels (user_id, name, is_story_label) VALUES (?, ?, ?)",
            (user_id, name, 1 if is_story_label else 0),
        )
        await db.commit()
        async with db.execute(
            "SELECT id, name, is_story_label FROM labels WHERE user_id=? AND name = ? COLLATE NOCASE",
            (user_id, name),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def rename_label(user_id: int, label_id: int, name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "UPDATE labels SET name=? WHERE id=? AND user_id=?",
                (name.strip(), label_id, user_id),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def merge_labels(user_id: int, source_ids: list[int], target_id: int) -> int:
    """Reassign all card_labels rows from source labels to target, delete sources.
    Returns number of source labels deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Verify target belongs to this user.
        async with db.execute(
            "SELECT 1 FROM labels WHERE id=? AND user_id=?", (target_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return 0
        deleted = 0
        for src_id in source_ids:
            if src_id == target_id:
                continue
            async with db.execute(
                "SELECT 1 FROM labels WHERE id=? AND user_id=?", (src_id, user_id)
            ) as cur:
                if not await cur.fetchone():
                    continue
            # Move card associations; INSERT OR IGNORE handles cards already in target.
            await db.execute(
                "INSERT OR IGNORE INTO card_labels (card_id, label_id) "
                "SELECT card_id, ? FROM card_labels WHERE label_id=?",
                (target_id, src_id),
            )
            await db.execute("DELETE FROM card_labels WHERE label_id=?", (src_id,))
            await db.execute("DELETE FROM labels WHERE id=?", (src_id,))
            deleted += 1
        await db.commit()
        return deleted


async def delete_label(user_id: int, label_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # Only delete the label if it belongs to this user; FK cascade handles card_labels.
        async with db.execute(
            "SELECT 1 FROM labels WHERE id=? AND user_id=?", (label_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return
        await db.execute("DELETE FROM card_labels WHERE label_id=?", (label_id,))
        await db.execute("DELETE FROM labels WHERE id=?", (label_id,))
        await db.commit()


async def get_or_create_story_label(user_id: int, text_id: int) -> dict:
    """Return (or create) the story label for a reader text. Returns {id, name, is_story_label}."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT title FROM reader_texts WHERE id=? AND user_id=?", (text_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return {}
        label_name = f"📖 {row['title']}"
        async with db.execute(
            "SELECT id, name, is_story_label FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
            (user_id, label_name),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            return dict(existing)
        cursor = await db.execute(
            "INSERT INTO labels (user_id, name, is_story_label) VALUES (?, ?, 1)",
            (user_id, label_name),
        )
        await db.commit()
        return {"id": cursor.lastrowid, "name": label_name, "is_story_label": 1}


# ── Reader texts ──────────────────────────────────────────────────────────────

async def create_reader_text(
    user_id: int, title: str, prompt: str, content: str, target_lang: str
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO reader_texts (user_id, title, prompt, content, target_lang)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, title, prompt, content, target_lang),
        )
        await db.commit()
        return cursor.lastrowid


async def list_reader_texts(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, title, prompt, target_lang, created_at
               FROM reader_texts WHERE user_id=? ORDER BY created_at DESC""",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_reader_text(user_id: int, text_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, title, prompt, content, target_lang, created_at
               FROM reader_texts WHERE id=? AND user_id=?""",
            (text_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_reader_text(user_id: int, text_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM reader_texts WHERE id=? AND user_id=?", (text_id, user_id)
        )
        await db.commit()


async def get_reader_sentences(user_id: int, text_id: int) -> list[dict]:
    """Return cached sentence data for a reader text owned by user_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Verify ownership first.
        async with db.execute(
            "SELECT 1 FROM reader_texts WHERE id=? AND user_id=?", (text_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return []
        async with db.execute(
            """SELECT sentence_idx, sentence_text, translation,
                      CASE WHEN audio_data IS NOT NULL THEN 1 ELSE 0 END AS has_audio
               FROM reader_sentences WHERE text_id=? ORDER BY sentence_idx""",
            (text_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def upsert_reader_sentence(
    text_id: int, idx: int, sentence_text: str,
    translation: str | None = None, audio_data: bytes | None = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO reader_sentences (text_id, sentence_idx, sentence_text, translation, audio_data)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(text_id, sentence_idx) DO UPDATE SET
                 translation = COALESCE(excluded.translation, translation),
                 audio_data  = COALESCE(excluded.audio_data,  audio_data)""",
            (text_id, idx, sentence_text, translation, audio_data),
        )
        await db.commit()


async def get_sentence_audio(user_id: int, text_id: int, idx: int) -> bytes | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT rs.audio_data FROM reader_sentences rs
               JOIN reader_texts rt ON rt.id = rs.text_id
               WHERE rs.text_id=? AND rs.sentence_idx=? AND rt.user_id=?""",
            (text_id, idx, user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


def _cjk_only(text: str) -> str:
    """Strip punctuation/spaces, keeping only CJK characters."""
    import re
    return re.sub(r"[^一-鿿㐀-䶿豈-﫿぀-ヿ]", "", text)


async def get_word_statuses(user_id: int, words: list[str], target_lang: str) -> dict[str, str]:
    """Return a mapping of word → 'known' | 'weak' | 'new' for the given word list.

    Matching is fuzzy: a token matches a deck card if the normalized token equals the
    normalized card target_text, OR if the normalized card target_text starts with the
    normalized token (e.g. token '多謝' matches card '多謝你').
    """
    if not words:
        return {}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT c.target_text,
                      MAX(cf.repetitions) AS max_reps,
                      MIN(cf.ease_factor) AS min_ease
               FROM cards c
               JOIN card_faces cf ON cf.card_id = c.id
               WHERE c.user_id = ? AND c.target_lang = ?
               GROUP BY c.target_text""",
            (user_id, target_lang),
        ) as cur:
            rows = await cur.fetchall()

    # Build normalized card lookup: stripped_text → status
    card_lookup: dict[str, str] = {}
    for r in rows:
        key = _cjk_only(r["target_text"])
        if not key:
            continue
        status = "known" if r["max_reps"] >= 2 and r["min_ease"] >= 2.0 else "weak"
        if key not in card_lookup or status == "known":
            card_lookup[key] = status

    result: dict[str, str] = {}
    for word in words:
        norm = _cjk_only(word)
        if not norm:
            continue
        # Exact normalized match → use actual status.
        if norm in card_lookup:
            result[word] = card_lookup[norm]
            continue
        # Token appears anywhere within a card (e.g. token '去' in card '我去旅行').
        # Use the best status found, but cap substring-only matches at 'weak' since the
        # user may have learned the whole phrase without isolating this word.
        best: str | None = None
        for card_norm, status in card_lookup.items():
            if norm in card_norm:
                # Prefer 'known' over 'weak' if multiple cards contain this token.
                if best is None or status == "known":
                    best = status
        if best is not None:
            result[word] = "weak" if best == "known" else best
    return result


async def get_streak(user_id: int) -> int:
    """Return the user's current study streak in days.

    A streak is the number of consecutive days (ending today or yesterday) with
    at least one review. Counting back from today preserves the streak before
    the user studies on a given day.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT study_date FROM study_activity WHERE user_id=? ORDER BY study_date DESC",
            (user_id,),
        ) as cur:
            rows = [r[0] async for r in cur]

    if not rows:
        return 0

    from datetime import date, timedelta
    today = date.today()
    # Allow streak if most-recent activity is today or yesterday.
    most_recent = date.fromisoformat(rows[0])
    if most_recent < today - timedelta(days=1):
        return 0

    streak = 0
    expected = today if most_recent == today else today - timedelta(days=1)
    for row in rows:
        d = date.fromisoformat(row)
        if d == expected:
            streak += 1
            expected -= timedelta(days=1)
        elif d < expected:
            break
    return streak
