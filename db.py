import hashlib
import json
import os
import time

import aiosqlite

import tokenizer

DB_PATH = os.getenv("DB_PATH", "data/cards.db")
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

# Card face values. 'source' = native-language text, 'target' = target-language text,
# 'pronunciation' = romanization (logographic) or audio-only (Latin script).
FACES = ("source", "target", "pronunciation")

# When a word is brand-new we introduce only this face. The other faces of the
# same card stay locked until the primary face graduates out of learning, so a
# single word no longer shows up three times in the same first session.
PRIMARY_FACE = "target"

SUPPORTED_LANGS = ("yue", "cmn", "fr", "es", "de", "it", "pt", "tl", "ms", "id", "ko", "hi", "te", "ja", "bn", "ur", "ar", "sw", "ru", "vi", "fa", "tr", "nl", "pl", "sv", "nb", "ro", "uk", "el", "th", "he")
LOGOGRAPHIC_LANGS = {"yue", "cmn", "ja"}


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
            ("cefr_level", "TEXT", "NULL"),
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
                learning_step INTEGER DEFAULT NULL,
                UNIQUE(card_id, face)
            )
        """)
        if not await _column_exists(db, "card_faces", "first_seen_date"):
            await db.execute("ALTER TABLE card_faces ADD COLUMN first_seen_date TEXT")
        if not await _column_exists(db, "card_faces", "learning_step"):
            await db.execute("ALTER TABLE card_faces ADD COLUMN learning_step INTEGER DEFAULT NULL")

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
        if await _table_exists(db, "reader_sentences") and not await _column_exists(db, "reader_sentences", "romanization"):
            await db.execute("ALTER TABLE reader_sentences ADD COLUMN romanization TEXT")
        # can_use_shared_key was superseded by the plan/billing system and is never read.
        if await _column_exists(db, "users", "can_use_shared_key"):
            await db.execute("ALTER TABLE users DROP COLUMN can_use_shared_key")
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

        # Sessions: persisted so logins survive deploys/restarts and span workers.
        # We store sha256(token), never the raw token, so a DB leak can't be replayed as a cookie.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expiry REAL NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)"
        )

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
                romanization TEXT,
                UNIQUE(text_id, sentence_idx)
            )
        """)

        # ── Learning path (AI course) — IDEAS item 43 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                target_lang TEXT    NOT NULL,
                level       TEXT    NOT NULL DEFAULT 'A1',
                status      TEXT    NOT NULL DEFAULT 'active',
                active_plan TEXT,            -- JSON outline of the in-progress unit (concepts + cursor); NULL between units
                created_at  TEXT             DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_units (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL,
                idx       INTEGER NOT NULL DEFAULT 0,
                title     TEXT    NOT NULL DEFAULT '',
                summary   TEXT    NOT NULL DEFAULT '',
                theme     TEXT    NOT NULL DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_lessons (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id      INTEGER NOT NULL,
                unit_id        INTEGER,
                lesson_num     INTEGER NOT NULL DEFAULT 1,
                title          TEXT    NOT NULL DEFAULT '',
                objective      TEXT    NOT NULL DEFAULT '',
                content        TEXT,
                concepts_json  TEXT    NOT NULL DEFAULT '[]',
                summary        TEXT    NOT NULL DEFAULT '',
                llm_debug_json TEXT,
                score          INTEGER,
                completed_at   TEXT,
                crown_level    INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_concepts (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id            INTEGER NOT NULL,
                kind                 TEXT    NOT NULL,
                key                  TEXT    NOT NULL,
                label                TEXT    NOT NULL DEFAULT '',
                gloss                TEXT    NOT NULL DEFAULT '',
                introduced_lesson_id INTEGER,
                UNIQUE(course_id, key)
            )
        """)
        # Verified canonical grammar content — SHARED across users, keyed by
        # (lang, concept_key). Expensive to generate (generator + critic pass),
        # cheap to replay; see grammar_lessons.py.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS concept_content (
                lang        TEXT NOT NULL,
                concept_key TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (lang, concept_key)
            )
        """)
        # Per-user, per-language concept mastery ledger. Incremented when a lesson
        # is completed; fed back to the unit planner to steer around weak spots.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS concept_mastery (
                user_id     INTEGER NOT NULL,
                lang        TEXT    NOT NULL,
                concept_key TEXT    NOT NULL,
                correct     INTEGER NOT NULL DEFAULT 0,
                total       INTEGER NOT NULL DEFAULT 0,
                last_seen   TEXT,
                PRIMARY KEY (user_id, lang, concept_key)
            )
        """)
        # Tutor chat — per-user conversations with the AI tutor. Tutor turns store
        # the structured JSON payload (reply + corrections + new_items + points).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tutor_conversations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                lang            TEXT    NOT NULL,
                title           TEXT    NOT NULL DEFAULT '',
                active_drill_id INTEGER,
                created_at      TEXT    DEFAULT (datetime('now')),
                updated_at      TEXT    DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tutor_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role            TEXT    NOT NULL,
                content         TEXT    NOT NULL,
                drill_id        INTEGER,
                drill_skill     TEXT,
                created_at      TEXT    DEFAULT (datetime('now'))
            )
        """)
        # Light gamification: append-only points ledger (tutor awards points when
        # the learner correctly uses known vocab/grammar in conversation).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS points_ledger (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                lang       TEXT    NOT NULL,
                points     INTEGER NOT NULL,
                reason     TEXT    NOT NULL DEFAULT '',
                created_at TEXT    DEFAULT (datetime('now'))
            )
        """)
        # Word embedding cache — shared across users (a word embeds the same for
        # everyone), keyed by (lang, model, word). vector = packed float32 BLOB.
        # Powers the tutor's construction drills (snap example fillers to known vocab).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS embedding_cache (
                lang   TEXT NOT NULL,
                model  TEXT NOT NULL,
                word   TEXT NOT NULL,
                vector BLOB NOT NULL,
                PRIMARY KEY (lang, model, word)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL DEFAULT 'bug',
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                screenshot_media_id TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                priority TEXT NOT NULL DEFAULT 'medium',
                triage_summary TEXT,
                triage_group TEXT,
                suggested_prompt TEXT,
                admin_notes TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Backfill face rows for any cards that don't have them yet.
        for face in FACES:
            await db.execute(
                "INSERT OR IGNORE INTO card_faces (card_id, face) SELECT id, ? FROM cards",
                (face,),
            )
        await db.commit()

        # Apply any versioned schema migrations layered on top of the baseline above.
        # Must run BEFORE the indexes below so that schema-redesign migrations (e.g.
        # 012_lesson_redesign.sql) can drop/recreate tables before we index their columns.
        await _run_migrations(db)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_units_course    ON course_units(course_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_lessons_course  ON course_lessons(course_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_concepts_course ON course_concepts(course_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tutor_msgs_conv ON tutor_messages(conversation_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_points_user     ON points_ledger(user_id, lang)")


async def _run_migrations(db) -> None:
    """Apply pending migrations from migrations/*.sql, in filename order, once each.

    The CREATE TABLE statements in init() define the *current* baseline schema and
    are idempotent. Post-baseline schema changes go in a new numbered file under
    migrations/ (e.g. 001_add_user_api_keys.sql) rather than as ad-hoc ALTERs here,
    so dev and prod converge predictably. Applied filenames are recorded in
    schema_migrations and never re-run.
    """
    await db.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)
    async with db.execute("SELECT version FROM schema_migrations") as cur:
        applied = {r[0] for r in await cur.fetchall()}

    if not os.path.isdir(MIGRATIONS_DIR):
        return

    for fname in sorted(os.listdir(MIGRATIONS_DIR)):
        if not fname.endswith(".sql") or fname in applied:
            continue
        with open(os.path.join(MIGRATIONS_DIR, fname), encoding="utf-8") as f:
            script = f.read()
        try:
            await db.executescript(script)
        except Exception as e:
            msg = str(e).lower()
            # Idempotency: ignore errors that mean the migration is a no-op on this DB.
            # "duplicate column name" — baseline schema already has the column.
            # "no such column/table"  — old migration references a column/table that was
            #                           removed by a later migration (e.g. schema redesign).
            ignorable = ("duplicate column name", "no such column", "no such table")
            if not any(p in msg for p in ignorable):
                raise
        await db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (fname,))
        await db.commit()


async def bootstrap_admin(username: str, password_hash: str, email: str | None = None) -> int:
    """Ensure an admin user exists. If no users, create with given creds and migrate existing data.
    On every startup, ensures the admin's email is set and verified if provided.
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
                """INSERT INTO users
                   (username, password_hash, is_admin, email, email_verified)
                   VALUES (?, ?, 1, ?, 1)""",
                (username, password_hash, email),
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
            admin_id = row["id"] if row else 0

        # Keep admin email in sync with env var and always mark it verified.
        if admin_id and email:
            await db.execute(
                "UPDATE users SET email=?, email_verified=1 WHERE id=?",
                (email, admin_id),
            )
            await db.commit()

        return admin_id


# ── Users ─────────────────────────────────────────────────────────────────────

_USER_COLS = (
    "id, username, email, display_name, password_hash, is_admin, "
    "native_lang, email_verified, created_at, "
    "plan, stripe_customer_id, subscription_status, subscription_period_end, "
    "stripe_subscription_id, cancel_at_period_end"
)


async def get_user_by_username(username: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_USER_COLS} FROM users WHERE username=? COLLATE NOCASE",
            (username,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user_by_email(email: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_USER_COLS} FROM users WHERE lower(email)=lower(?)",
            (email,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user_by_token(token: str, token_type: str) -> dict | None:
    """Look up a user by verification_token or reset_token.

    When token_type is 'reset', reset_token_expiry is also included in the
    returned dict so the caller can validate expiry without a second query.
    """
    col = "verification_token" if token_type == "verification" else "reset_token"
    extra = ", reset_token_expiry" if token_type == "reset" else ""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_USER_COLS}{extra} FROM users WHERE {col}=?",
            (token,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_USER_COLS} FROM users WHERE id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_user_plan(user_id: int, plan: str) -> None:
    """Admin comp: set a user's plan directly (e.g. grant Pro to a friend) without
    a Stripe subscription. Use set_plan_by_customer for Stripe-driven changes."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET plan=? WHERE id=?",
            (plan, user_id),
        )
        await db.commit()


async def list_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_USER_COLS} FROM users ORDER BY id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def create_user(
    username: str,
    password_hash: str,
    is_admin: bool = False,
    email: str | None = None,
    display_name: str | None = None,
    email_verified: bool = True,
    verification_token: str | None = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO users
               (username, password_hash, is_admin, email, display_name, email_verified, verification_token)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, password_hash, 1 if is_admin else 0,
             email, display_name, 1 if email_verified else 0, verification_token),
        )
        await db.commit()
        return cursor.lastrowid


async def set_email_verified(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET email_verified=1, verification_token=NULL WHERE id=?",
            (user_id,),
        )
        await db.commit()


async def set_verification_token(user_id: int, token: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET verification_token=? WHERE id=?",
            (token, user_id),
        )
        await db.commit()


async def set_reset_token(user_id: int, token: str | None, expiry: str | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET reset_token=?, reset_token_expiry=? WHERE id=?",
            (token, expiry, user_id),
        )
        await db.commit()


async def update_user_profile(
    user_id: int,
    *,
    username: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    email_verified: bool | None = None,
    verification_token: str | None = ...,  # type: ignore[assignment]
) -> None:
    """Partial-update profile fields. Pass only the kwargs you want to change."""
    fields, vals = [], []
    if username is not None:
        fields.append("username=?"); vals.append(username)
    if display_name is not None:
        fields.append("display_name=?"); vals.append(display_name)
    if email is not None:
        fields.append("email=?"); vals.append(email)
    if email_verified is not None:
        fields.append("email_verified=?"); vals.append(1 if email_verified else 0)
    if verification_token is not ...:  # explicitly passed (including None to clear)
        fields.append("verification_token=?"); vals.append(verification_token)
    if not fields:
        return
    vals.append(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", vals)
        await db.commit()


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


# ── Sessions ──────────────────────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(token: str, user_id: int, expiry: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sessions (token_hash, user_id, expiry) VALUES (?, ?, ?)",
            (_hash_token(token), user_id, expiry),
        )
        await db.commit()


async def get_session_user(token: str) -> int | None:
    """Return the user_id for a valid session token, or None if missing/expired.
    Expired rows are deleted lazily on lookup."""
    th = _hash_token(token)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, expiry FROM sessions WHERE token_hash=?", (th,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        user_id, expiry = row
        if expiry < time.time():
            await db.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
            await db.commit()
            return None
        return user_id


async def delete_session(token: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
        await db.commit()


async def delete_user_sessions(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        await db.commit()


async def purge_expired_sessions() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE expiry < ?", (time.time(),))
        await db.commit()


# ── Settings ──────────────────────────────────────────────────────────────────

async def count_cards(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM cards WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


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


# ── Billing & usage metering ────────────────────────────────────────────────

async def get_admin_dashboard_stats() -> dict:
    """Aggregate stats for the admin dashboard — runs in one connection."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # ── Users by tier ──
        async with db.execute(
            """SELECT
                 COUNT(*) AS total,
                 COALESCE(SUM(is_admin),0) AS admins,
                 COALESCE(SUM(CASE WHEN plan='pro' AND stripe_customer_id IS NOT NULL THEN 1 ELSE 0 END),0) AS pro_paid,
                 COALESCE(SUM(CASE WHEN plan='pro' AND stripe_customer_id IS NULL AND NOT is_admin THEN 1 ELSE 0 END),0) AS pro_comped,
                 COALESCE(SUM(CASE WHEN plan='free' AND NOT is_admin THEN 1 ELSE 0 END),0) AS free
               FROM users"""
        ) as cur:
            tier_row = dict(await cur.fetchone())

        # ── Per-user activity summary ──
        async with db.execute(
            """SELECT u.id, u.username, u.plan, u.is_admin, u.created_at,
                      u.stripe_customer_id,
                      (SELECT COUNT(*) FROM cards c WHERE c.user_id=u.id) AS card_count,
                      (SELECT MAX(d) FROM (
                         SELECT MAX(study_date) AS d FROM study_activity sa WHERE sa.user_id=u.id
                         UNION ALL
                         SELECT MAX(created_at) FROM cards c2 WHERE c2.user_id=u.id
                         UNION ALL
                         SELECT MAX(tm.created_at) FROM tutor_messages tm
                           JOIN tutor_conversations tc ON tc.id=tm.conversation_id
                           WHERE tc.user_id=u.id
                         UNION ALL
                         SELECT MAX(created_at) FROM points_ledger pl WHERE pl.user_id=u.id
                         UNION ALL
                         -- AI usage is only month-precision (period = 'YYYY-MM'), but it
                         -- guarantees an active user with cards/translations whose card
                         -- rows predate created_at tracking still shows a date, not "never".
                         SELECT MAX(uc2.period || '-01') FROM usage_counters uc2 WHERE uc2.user_id=u.id
                       )) AS last_active,
                      (SELECT ai_calls FROM usage_counters uc
                       WHERE uc.user_id=u.id AND uc.period=strftime('%Y-%m','now')) AS ai_calls_month
               FROM users u ORDER BY u.id"""
        ) as cur:
            users = [dict(r) for r in await cur.fetchall()]

        # ── DAU / WAU / MAU ──
        async with db.execute(
            """SELECT
                 (SELECT COUNT(DISTINCT user_id) FROM study_activity WHERE study_date=date('now')) AS dau,
                 (SELECT COUNT(DISTINCT user_id) FROM study_activity WHERE study_date>=date('now','-7 days')) AS wau,
                 (SELECT COUNT(DISTINCT user_id) FROM study_activity WHERE study_date>=date('now','-30 days')) AS mau"""
        ) as cur:
            activity = dict(await cur.fetchone())

        # ── Signups this week / month ──
        async with db.execute(
            """SELECT
                 (SELECT COUNT(*) FROM users WHERE created_at>=datetime('now','-7 days')) AS signups_week,
                 (SELECT COUNT(*) FROM users WHERE created_at>=datetime('now','-30 days')) AS signups_month"""
        ) as cur:
            signups = dict(await cur.fetchone())

        # ── AI usage totals (current month) ──
        async with db.execute(
            """SELECT COALESCE(SUM(ai_calls),0) AS total_calls
               FROM usage_counters WHERE period=strftime('%Y-%m','now')"""
        ) as cur:
            row = await cur.fetchone()
            ai_calls_total = row[0]

        # ── AI usage last month ──
        async with db.execute(
            """SELECT COALESCE(SUM(ai_calls),0) AS total_calls
               FROM usage_counters WHERE period=strftime('%Y-%m','now','-1 month')"""
        ) as cur:
            row = await cur.fetchone()
            ai_calls_last_month = row[0]

        # ── Language breakdown (cards across all users) ──
        async with db.execute(
            "SELECT target_lang, COUNT(*) AS cnt FROM cards GROUP BY target_lang ORDER BY cnt DESC"
        ) as cur:
            lang_dist = [dict(r) for r in await cur.fetchall()]

        # ── Language breakdown by active learners ──
        async with db.execute(
            """SELECT c.target_lang, COUNT(DISTINCT c.user_id) AS learners
               FROM cards c GROUP BY c.target_lang ORDER BY learners DESC"""
        ) as cur:
            lang_learners = [dict(r) for r in await cur.fetchall()]

        # ── Feedback summary ──
        async with db.execute(
            """SELECT status, COUNT(*) AS cnt FROM feedback GROUP BY status"""
        ) as cur:
            feedback_by_status = {r["status"]: r["cnt"] for r in await cur.fetchall()}
        async with db.execute(
            """SELECT type, COUNT(*) AS cnt FROM feedback GROUP BY type"""
        ) as cur:
            feedback_by_type = {r["type"]: r["cnt"] for r in await cur.fetchall()}

        # ── Total cards, reviews ──
        async with db.execute("SELECT COUNT(*) FROM cards") as cur:
            total_cards = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM study_activity") as cur:
            total_study_days = (await cur.fetchone())[0]

        # ── Points today (all users) ──
        async with db.execute(
            "SELECT COALESCE(SUM(points),0) FROM points_ledger WHERE date(created_at)=date('now')"
        ) as cur:
            xp_today = (await cur.fetchone())[0]

    return {
        "tiers": tier_row,
        "users": users,
        "activity": activity,
        "signups": signups,
        "ai_usage": {
            "this_month": ai_calls_total,
            "last_month": ai_calls_last_month,
        },
        "languages": {"cards": lang_dist, "learners": lang_learners},
        "feedback": {"by_status": feedback_by_status, "by_type": feedback_by_type},
        "totals": {
            "cards": total_cards,
            "study_days": total_study_days,
            "xp_today": xp_today,
        },
    }


async def get_usage(user_id: int) -> int:
    """AI calls the user has made in the current calendar month (UTC)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ai_calls FROM usage_counters "
            "WHERE user_id=? AND period=strftime('%Y-%m','now')",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def increment_usage(user_id: int) -> int:
    """Add one AI call to the current month's counter; return the new total."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO usage_counters (user_id, period, ai_calls)
               VALUES (?, strftime('%Y-%m','now'), 1)
               ON CONFLICT(user_id, period) DO UPDATE SET ai_calls = ai_calls + 1""",
            (user_id,),
        )
        async with db.execute(
            "SELECT ai_calls FROM usage_counters "
            "WHERE user_id=? AND period=strftime('%Y-%m','now')",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        await db.commit()
        return row[0] if row else 1


async def get_user_by_stripe_customer(customer_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_USER_COLS} FROM users WHERE stripe_customer_id=?",
            (customer_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_stripe_customer(user_id: int, customer_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET stripe_customer_id=? WHERE id=?",
            (customer_id, user_id),
        )
        await db.commit()


async def set_plan_by_customer(
    customer_id: str,
    plan: str,
    status: str | None,
    period_end: str | None,
    sub_id: str | None = None,
    cancel_at_period_end: bool = False,
) -> None:
    """Update subscription state for whichever user owns this Stripe customer."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET plan=?, subscription_status=?, subscription_period_end=?, "
            "stripe_subscription_id=?, cancel_at_period_end=? "
            "WHERE stripe_customer_id=?",
            (plan, status, period_end, sub_id, int(cancel_at_period_end), customer_id),
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
    cefr_level: str | None = None,
) -> int:
    _VALID_CEFR = {"A1", "A2", "B1", "B2", "C1", "C2"}
    safe_cefr = cefr_level if cefr_level in _VALID_CEFR else None
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO cards (user_id, source_text, target_text, romanization, target_lang,
                                  audio_data, notes, priority, classifier, canonical_card_id, cefr_level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, source_text, target_text, romanization, target_lang,
             audio_data, notes, max(1, min(5, priority)), classifier or "", canonical_card_id, safe_cefr),
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


async def add_labels_by_name(user_id: int, card_id: int, names: list[str]) -> list[int]:
    """Auto-create labels by name (if needed) and attach them to an existing card.
    Used by the background auto-labeler. Returns the attached label ids."""
    if not names:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        # Only operate on a card the user actually owns.
        async with db.execute(
            "SELECT 1 FROM cards WHERE id=? AND user_id=?", (card_id, user_id),
        ) as cur:
            if not await cur.fetchone():
                return []
        attached: list[int] = []
        for name in names:
            name = (name or "").strip()
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
            if row:
                await db.execute(
                    "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
                    (card_id, row[0]),
                )
                attached.append(row[0])
        await db.commit()
        return attached


_CARD_COLS = "id, source_text, target_text, romanization, target_lang, notes, priority, tutor_flag, suspended, classifier, canonical_card_id, cefr_level"


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
            """SELECT cf.interval_days, cf.ease_factor, cf.repetitions,
                      cf.first_seen_date, cf.learning_step
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
               cf.learning_step,
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


async def _gate_phrases(db, user_id: int, faces: list[dict]) -> list[dict]:
    """Exclude phrase cards until all constituent single-word cards have graduated.

    A phrase is any card whose target_text tokenizes into >1 word.  For each
    phrase, we check whether every constituent word exists as a separate card
    (same user + language) with its primary face graduated (learning_step IS NULL
    and first_seen_date IS NOT NULL).  Phrases whose constituents exist as cards
    but haven't all graduated yet are EXCLUDED (not just deprioritized) so
    learners always study individual words before the phrases that contain them.

    If NONE of the constituent words exist as separate cards, the phrase is NOT
    gated — it's a standalone vocabulary item (e.g. CJK compound words that the
    tokenizer segments but the user hasn't atomized).
    """
    if not faces:
        return faces

    langs = {f["target_lang"] for f in faces}
    graduated: dict[str, set[str]] = {}
    existing: dict[str, set[str]] = {}
    for lang in langs:
        async with db.execute(
            """SELECT DISTINCT c.target_text FROM cards c
               JOIN card_faces cf ON cf.card_id = c.id
               WHERE c.user_id = ? AND c.target_lang = ?
                 AND cf.face = ?
                 AND cf.first_seen_date IS NOT NULL
                 AND cf.learning_step IS NULL""",
            (user_id, lang, PRIMARY_FACE),
        ) as cur:
            graduated[lang] = {r[0] for r in await cur.fetchall()}
        async with db.execute(
            """SELECT DISTINCT c.target_text FROM cards c
               WHERE c.user_id = ? AND c.target_lang = ?""",
            (user_id, lang),
        ) as cur:
            existing[lang] = {r[0] for r in await cur.fetchall()}

    ready = []
    gated_cards: set[int] = set()
    seen_cards: set[int] = set()
    for face in faces:
        cid = face["card_id"]
        if cid in gated_cards:
            continue
        if cid in seen_cards:
            ready.append(face)
            continue
        seen_cards.add(cid)
        words = tokenizer.phrase_words(face["target_text"], face["target_lang"])
        if len(words) <= 1:
            ready.append(face)
            continue
        grad_set = graduated.get(face["target_lang"], set())
        exist_set = existing.get(face["target_lang"], set())
        constituent_words = [w for w in words if w != face["target_text"]]
        words_in_deck = [w for w in constituent_words if w in exist_set]
        if not words_in_deck:
            ready.append(face)
        elif all(w in grad_set for w in words_in_deck):
            ready.append(face)
        else:
            gated_cards.add(cid)

    return ready


async def get_study_session(
    user_id: int,
    label_ids: list[int] | None = None,
    target_lang: str | None = None,
) -> dict:
    """Return due reviews + new cards up to the daily cap, with stats."""
    cap = int(await get_setting(user_id, "new_cards_per_day") or 20)

    extra_filter = ""
    extra_params: tuple = ()
    if label_ids:
        for lid in label_ids:
            extra_filter += (
                " AND cf.card_id IN (SELECT cl.card_id FROM card_labels cl "
                "JOIN labels l ON l.id = cl.label_id "
                "WHERE cl.label_id=? AND l.user_id=?)"
            )
            extra_params += (lid, user_id)
    if target_lang is not None:
        extra_filter += " AND c.target_lang = ?"
        extra_params += (target_lang,)
    label_filter = extra_filter
    label_params = extra_params

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        review_sql = f"""
            SELECT cf.id AS face_id, cf.card_id, cf.face, cf.next_review,
                   cf.interval_days, cf.ease_factor, cf.repetitions, cf.first_seen_date,
                   cf.learning_step,
                   c.source_text, c.target_text, c.romanization, c.target_lang, c.notes,
                   c.priority, c.tutor_flag, c.cefr_level
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
            # Stagger faces: a brand-new word offers only its primary face. The
            # other faces unlock once the primary face has graduated out of
            # learning (learning_step IS NULL with a first_seen_date set), so the
            # three faces of one word spread across days instead of clustering in
            # a single session.
            #
            # Phrase gating: phrase cards (multi-word target_text) are deferred
            # until all constituent single-word cards have their primary face
            # graduated.  Over-fetch to compensate for filtered phrases.
            fetch_limit = remaining * 3
            new_sql = f"""
                SELECT cf.id AS face_id, cf.card_id, cf.face, cf.next_review,
                       cf.interval_days, cf.ease_factor, cf.repetitions, cf.first_seen_date,
                       cf.learning_step,
                       c.source_text, c.target_text, c.romanization, c.target_lang, c.notes,
                       c.priority, c.tutor_flag, c.cefr_level
                FROM card_faces cf JOIN cards c ON c.id = cf.card_id
                WHERE c.user_id = ?
                  AND cf.first_seen_date IS NULL
                  AND c.suspended = 0
                  AND (
                        cf.face = ?
                        OR EXISTS (
                            SELECT 1 FROM card_faces p
                            WHERE p.card_id = cf.card_id AND p.face = ?
                              AND p.first_seen_date IS NOT NULL
                              AND p.learning_step IS NULL
                        )
                  )
                  {label_filter}
                ORDER BY c.priority DESC, c.id ASC
                LIMIT ?
            """
            async with db.execute(
                new_sql,
                (user_id, PRIMARY_FACE, PRIMARY_FACE) + label_params + (fetch_limit,),
            ) as cur:
                new_faces = [dict(r) for r in await cur.fetchall()]

            new_faces = await _gate_phrases(db, user_id, new_faces)
            new_faces = new_faces[:remaining]

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


async def get_all_faces(
    user_id: int,
    label_ids: list[int] | None = None,
    target_lang: str | None = None,
) -> list[dict]:
    extra = ""
    extra_params: tuple = ()
    if label_ids:
        for lid in label_ids:
            extra += (
                " AND cf.card_id IN (SELECT cl.card_id FROM card_labels cl "
                "JOIN labels l ON l.id = cl.label_id "
                "WHERE cl.label_id=? AND l.user_id=?)"
            )
            extra_params += (lid, user_id)
    if target_lang is not None:
        extra += " AND c.target_lang = ?"
        extra_params += (target_lang,)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql, params = _faces_query(extra, (user_id,) + extra_params)
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
        async with db.execute(
            f"""SELECT card_id, ease_factor, repetitions, interval_days,
                       learning_step, first_seen_date
                FROM card_faces
                WHERE card_id IN ({placeholders}) AND face = ?""",
            tuple(ids) + (PRIMARY_FACE,),
        ) as cur:
            face_rows = await cur.fetchall()
    by_card: dict[int, list[dict]] = {}
    for lr in label_rows:
        by_card.setdefault(lr["card_id"], []).append({"id": lr["id"], "name": lr["name"]})
    face_by_card: dict[int, dict] = {}
    for fr in face_rows:
        face_by_card[fr["card_id"]] = dict(fr)
    for c in cards:
        c["labels"] = by_card.get(c["id"], [])
        f = face_by_card.get(c["id"])
        if f:
            c["ease_factor"] = f["ease_factor"]
            c["repetitions"] = f["repetitions"]
            c["interval_days"] = f["interval_days"]
            c["learning_step"] = f["learning_step"]
            c["first_seen_date"] = f["first_seen_date"]
        else:
            c["ease_factor"] = 2.5
            c["repetitions"] = 0
            c["interval_days"] = 1
            c["learning_step"] = None
            c["first_seen_date"] = None
    return cards


async def get_due_count(
    user_id: int,
    label_ids: list[int] | None = None,
    target_lang: str | None = None,
) -> int:
    extra = ""
    extra_params: tuple = ()
    if label_ids:
        for lid in label_ids:
            extra += (
                " AND cf.card_id IN (SELECT cl.card_id FROM card_labels cl "
                "JOIN labels l ON l.id = cl.label_id "
                "WHERE cl.label_id=? AND l.user_id=?)"
            )
            extra_params += (lid, user_id)
    if target_lang is not None:
        extra += " AND c.target_lang = ?"
        extra_params += (target_lang,)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Reviews (already seen)
        async with db.execute(
            f"""SELECT COUNT(*) FROM card_faces cf JOIN cards c ON c.id = cf.card_id
               WHERE c.user_id = ? AND cf.next_review <= datetime('now') AND c.suspended = 0
                 AND cf.first_seen_date IS NOT NULL{extra}""",
            (user_id,) + extra_params,
        ) as cur:
            row = await cur.fetchone()
        review_count = row[0] if row else 0

        # New faces — apply phrase gating so the badge matches the session
        cap = int(await get_setting(user_id, "new_cards_per_day") or 20)
        async with db.execute(
            """SELECT COUNT(*) FROM card_faces cf JOIN cards c ON c.id = cf.card_id
               WHERE c.user_id = ? AND cf.first_seen_date = date('now')""",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            daily_new_used = row[0] if row else 0
        remaining = max(0, cap - daily_new_used)

        new_count = 0
        if remaining > 0:
            async with db.execute(
                f"""SELECT cf.id AS face_id, cf.card_id, cf.face,
                           c.target_text, c.target_lang
                    FROM card_faces cf JOIN cards c ON c.id = cf.card_id
                    WHERE c.user_id = ?
                      AND cf.first_seen_date IS NULL
                      AND c.suspended = 0
                      AND (
                            cf.face = ?
                            OR EXISTS (
                                SELECT 1 FROM card_faces p
                                WHERE p.card_id = cf.card_id AND p.face = ?
                                  AND p.first_seen_date IS NOT NULL
                                  AND p.learning_step IS NULL
                            )
                      ){extra}
                    ORDER BY c.priority DESC, c.id ASC
                    LIMIT ?""",
                (user_id, PRIMARY_FACE, PRIMARY_FACE) + extra_params + (remaining * 3,),
            ) as cur:
                new_faces = [dict(r) for r in await cur.fetchall()]
            gated = await _gate_phrases(db, user_id, new_faces)
            new_count = min(len(gated), remaining)

        return review_count + new_count


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
                   learning_step=?,
                   first_seen_date = CASE WHEN first_seen_date IS NULL THEN date('now') ELSE first_seen_date END
               WHERE card_id=? AND face=?""",
            (state["interval_days"], state["ease_factor"], state["repetitions"], state["next_review"],
             state.get("learning_step"), card_id, face),
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
                   next_review=datetime('now'), first_seen_date=NULL,
                   learning_step=NULL
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


async def get_cards_missing_embedding(user_id: int, limit: int) -> list[dict]:
    """Cards with no stored embedding yet — for lazy backfill in suggest-cards."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, source_text, target_text
               FROM cards WHERE user_id=? AND embedding IS NULL
               ORDER BY id LIMIT ?""",
            (user_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_cards_basic(user_id: int, target_lang: str) -> list[dict]:
    """Lightweight card list without audio BLOBs — for atomize feature.
    Ordered shortest target_text first so phrase detection is consistent."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, source_text, target_text, romanization, target_lang,
                      notes, priority, classifier, canonical_card_id
               FROM cards
               WHERE user_id=? AND target_lang=? AND suspended=0
               ORDER BY length(target_text) ASC""",
            (user_id, target_lang),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_cards_missing_classifier(user_id: int, target_lang: str) -> list[dict]:
    """Cards with an empty classifier field for the given language."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, target_text, source_text
               FROM cards
               WHERE user_id=? AND target_lang=? AND suspended=0
                 AND (classifier IS NULL OR classifier='')
               ORDER BY id""",
            (user_id, target_lang),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def update_card_classifier(card_id: int, classifier: str, user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cards SET classifier=? WHERE id=? AND user_id=?",
            (classifier or "", card_id, user_id),
        )
        await db.commit()


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

async def _ensure_reader_cols(db) -> None:
    for col, default in [
        ("image_media_id", None),
        ("visibility", "'private'"),
        ("difficulty", "'B1'"),
    ]:
        if not await _column_exists(db, "reader_texts", col):
            dflt = f" DEFAULT {default}" if default else ""
            await db.execute(f"ALTER TABLE reader_texts ADD COLUMN {col} TEXT{dflt}")


async def create_reader_text(
    user_id: int, title: str, prompt: str, content: str, target_lang: str,
    image_media_id: str | None = None, difficulty: str = "B1",
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_reader_cols(db)
        cursor = await db.execute(
            """INSERT INTO reader_texts
               (user_id, title, prompt, content, target_lang, image_media_id, difficulty)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, title, prompt, content, target_lang, image_media_id, difficulty),
        )
        await db.commit()
        return cursor.lastrowid


async def list_reader_texts(user_id: int, target_lang: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_reader_cols(db)
        if target_lang is not None:
            async with db.execute(
                """SELECT id, title, prompt, target_lang, created_at, visibility
                   FROM reader_texts WHERE user_id=? AND target_lang=? ORDER BY created_at DESC""",
                (user_id, target_lang),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            """SELECT id, title, prompt, target_lang, created_at, visibility
               FROM reader_texts WHERE user_id=? ORDER BY created_at DESC""",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_reader_text(user_id: int, text_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_reader_cols(db)
        async with db.execute(
            """SELECT id, title, prompt, content, target_lang, created_at,
                      image_media_id, visibility, difficulty, user_id as owner_id
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


# ── Learning path (AI course) ─────────────────────────────────────────────────

async def create_course(user_id: int, target_lang: str, level: str) -> int:
    """Create an empty course. Lessons are generated one at a time on demand."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO courses (user_id, target_lang, level) VALUES (?, ?, ?)",
            (user_id, target_lang, level),
        )
        await db.commit()
        return cur.lastrowid


async def seed_foundation_units(course_id: int, units: list[dict]) -> None:
    """Persist pre-built Foundations (reading) units + lessons at the FRONT of a
    course. Each unit becomes a CLOSED course_units row (theme='foundations') with
    its lessons assigned and content already set. These don't register vocab
    concepts and are skippable in get_course.

    When the course already has AI units (backfill), existing units are shifted up
    to keep foundations at idx 0..N-1. Lesson numbers continue from MAX(lesson_num)
    so they don't collide with existing lessons.

    `units` — output of foundations.build_units(): [{title, objective, lessons:[
              {title, objective, content}]}].
    """
    if not units:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        # If AI units already exist, shift them to make room for foundations at front.
        async with db.execute(
            "SELECT COUNT(*) FROM course_units WHERE course_id=?", (course_id,)
        ) as cur:
            existing = (await cur.fetchone())[0]
        if existing:
            await db.execute(
                "UPDATE course_units SET idx = idx + ? WHERE course_id=?",
                (len(units), course_id),
            )
        unit_idx = 0

        # lesson_num continues from existing max so there are no collisions.
        async with db.execute(
            "SELECT COALESCE(MAX(lesson_num), 0) FROM course_lessons WHERE course_id=?",
            (course_id,),
        ) as cur:
            lesson_num = (await cur.fetchone())[0] + 1

        for u in units:
            cur = await db.execute(
                "INSERT INTO course_units (course_id, idx, title, summary, theme) VALUES (?, ?, ?, ?, 'foundations')",
                (course_id, unit_idx, (u.get("title") or "").strip(), (u.get("objective") or "").strip()),
            )
            unit_id = cur.lastrowid
            unit_idx += 1
            for lsn in (u.get("lessons") or []):
                await db.execute(
                    """INSERT INTO course_lessons
                       (course_id, unit_id, lesson_num, title, objective, content, concepts_json, summary)
                       VALUES (?, ?, ?, ?, ?, ?, '[]', '')""",
                    (
                        course_id, unit_id, lesson_num,
                        (lsn.get("title") or "").strip(), (lsn.get("objective") or "").strip(),
                        json.dumps(lsn.get("content")) if lsn.get("content") is not None else None,
                    ),
                )
                lesson_num += 1
        await db.commit()


async def get_courses(user_id: int, target_lang: str | None = None) -> list[dict]:
    """List the user's courses (optionally filtered by language), newest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT id, target_lang, level, status, created_at FROM courses WHERE user_id=?"
        params: tuple = (user_id,)
        if target_lang is not None:
            sql += " AND target_lang=?"
            params += (target_lang,)
        sql += " ORDER BY created_at DESC"
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_course(user_id: int, course_id: int) -> dict | None:
    """Return the full nested course (completed units + in-progress lessons)
    with per-lesson status (done / available / locked)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, target_lang, level, status, created_at FROM courses WHERE id=? AND user_id=?",
            (course_id, user_id),
        ) as cur:
            course = await cur.fetchone()
        if not course:
            return None
        course = dict(course)

        # Completed units (those with assigned lessons)
        async with db.execute(
            "SELECT id, idx, title, summary, theme FROM course_units WHERE course_id=? ORDER BY idx",
            (course_id,),
        ) as cur:
            units = [dict(r) for r in await cur.fetchall()]

        # Locking: Foundations (reading) lessons are SKIPPABLE — always available
        # if not done, so the learner can do them in any order or jump straight to
        # vocab. The AI vocab lessons keep strict sequential locking among THEMSELVES
        # (first incomplete one available), independent of foundations progress.
        ai_available_set = False

        def _status(lesson: dict, is_foundation: bool) -> str:
            nonlocal ai_available_set
            if lesson["completed_at"] is not None:
                return "done"
            if is_foundation:
                return "available"          # skippable — never gated
            if not ai_available_set:
                ai_available_set = True
                return "available"
            return "locked"

        for unit in units:
            is_foundation = unit.get("theme") == "foundations"
            async with db.execute(
                """SELECT id, lesson_num, title, objective, score, completed_at,
                          (SELECT COUNT(*) FROM course_concepts
                           WHERE introduced_lesson_id = course_lessons.id) AS concept_count
                   FROM course_lessons WHERE unit_id=? ORDER BY lesson_num""",
                (unit["id"],),
            ) as cur:
                lessons = [dict(r) for r in await cur.fetchall()]
            for l in lessons:
                l["status"] = _status(l, is_foundation)
            unit["lessons"] = lessons

        # Pending lessons (unit_id IS NULL) = current in-progress AI unit (never foundations)
        async with db.execute(
            """SELECT id, lesson_num, title, objective, score, completed_at, crown_level,
                      (SELECT COUNT(*) FROM course_concepts
                       WHERE introduced_lesson_id = course_lessons.id) AS concept_count
               FROM course_lessons WHERE course_id=? AND unit_id IS NULL ORDER BY lesson_num""",
            (course_id,),
        ) as cur:
            pending = [dict(r) for r in await cur.fetchall()]

        for l in pending:
            l["status"] = _status(l, False)

        if pending:
            units.append({
                "id": None, "idx": len(units),
                "title": None, "summary": None, "theme": "",
                "lessons": pending, "in_progress": True,
            })

        course["units"] = units
        course["lesson_count"] = sum(len(u["lessons"]) for u in units)
        course["done_count"] = sum(
            sum(1 for l in u["lessons"] if l.get("status") == "done")
            for u in units
        )
        return course


async def get_active_course(user_id: int, target_lang: str) -> dict | None:
    """The user's most recent active course for a language, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id FROM courses WHERE user_id=? AND target_lang=? AND status='active'
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, target_lang),
        ) as cur:
            row = await cur.fetchone()
    return await get_course(user_id, row["id"]) if row else None


async def delete_course(user_id: int, course_id: int) -> None:
    """Delete a course and all its units/lessons/concepts (ownership-checked)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM courses WHERE id=? AND user_id=?", (course_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return
        await db.execute("DELETE FROM course_concepts WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM course_lessons WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM course_units WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM courses WHERE id=? AND user_id=?", (course_id, user_id))
        await db.commit()


async def delete_ai_lessons(course_id: int) -> None:
    """Delete all non-foundation units (and their lessons/concepts) from a course.
    Foundation units (theme='foundations') are preserved. Resets active_plan."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM course_units WHERE course_id=? AND theme != 'foundations'",
            (course_id,)
        ) as cur:
            unit_ids = [r[0] for r in await cur.fetchall()]
        if unit_ids:
            ph = ",".join("?" * len(unit_ids))
            await db.execute(f"DELETE FROM course_lessons WHERE unit_id IN ({ph})", unit_ids)
            await db.execute(f"DELETE FROM course_units WHERE id IN ({ph})", unit_ids)
        # Also clear pending lessons (unit_id IS NULL = in-progress unit not yet closed)
        await db.execute(
            "DELETE FROM course_lessons WHERE course_id=? AND unit_id IS NULL", (course_id,)
        )
        await db.execute("DELETE FROM course_concepts WHERE course_id=?", (course_id,))
        await db.execute("UPDATE courses SET active_plan=NULL WHERE id=?", (course_id,))
        await db.commit()


async def get_active_plan(course_id: int) -> dict | None:
    """The in-progress unit's outline (concepts + cursor), or None between units."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT active_plan FROM courses WHERE id=?", (course_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


async def set_active_plan(course_id: int, plan: dict | None) -> None:
    """Store (or clear, when plan is None) the in-progress unit outline."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE courses SET active_plan=? WHERE id=?",
            (json.dumps(plan) if plan is not None else None, course_id),
        )
        await db.commit()


async def get_next_lesson_context(course_id: int) -> dict:
    """Return everything needed to generate the next lesson:
    lesson_num, concept_registry, unit_summaries, recent_summaries, prior_concepts."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            "SELECT COUNT(*) FROM course_lessons WHERE course_id=?", (course_id,)
        ) as cur:
            lesson_num = (await cur.fetchone())[0] + 1

        async with db.execute(
            "SELECT kind, key, label, gloss FROM course_concepts WHERE course_id=? ORDER BY id",
            (course_id,),
        ) as cur:
            concept_registry = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT title, summary FROM course_units WHERE course_id=? ORDER BY idx",
            (course_id,),
        ) as cur:
            unit_summaries = [dict(r) for r in await cur.fetchall()]

        # Last 3 lessons with summaries, in chronological order
        async with db.execute(
            """SELECT lesson_num, title, summary FROM course_lessons
               WHERE course_id=? AND summary != ''
               ORDER BY lesson_num DESC LIMIT 3""",
            (course_id,),
        ) as cur:
            recent_summaries = list(reversed([dict(r) for r in await cur.fetchall()]))

        return {
            "lesson_num":       lesson_num,
            "concept_registry": concept_registry,
            "unit_summaries":   unit_summaries,
            "recent_summaries": recent_summaries,
            "prior_concepts":   concept_registry,  # same data, used for distractor pool
        }


async def create_lesson(
    course_id: int,
    lesson_num: int,
    title: str,
    objective: str,
    concepts: list[dict],
    content: dict | None,
    summary: str,
    llm_debug: dict | None = None,
) -> int:
    """Persist a generated lesson (unit_id NULL until a unit is closed).
    Inserts concepts into the registry. Returns lesson_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO course_lessons
               (course_id, lesson_num, title, objective, content, concepts_json, summary, llm_debug_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                course_id, lesson_num,
                (title or "").strip(), (objective or "").strip(),
                json.dumps(content) if content is not None else None,
                json.dumps(concepts),
                (summary or "").strip(),
                json.dumps(llm_debug) if llm_debug else None,
            ),
        )
        lesson_id = cur.lastrowid

        for c in concepts:
            key = (c.get("key") or "").strip()
            if not key:
                continue
            await db.execute(
                """INSERT OR IGNORE INTO course_concepts
                   (course_id, kind, key, label, gloss, introduced_lesson_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    course_id, (c.get("kind") or "vocab").strip(), key,
                    (c.get("label") or "").strip(), (c.get("gloss") or "").strip(),
                    lesson_id,
                ),
            )

        await db.commit()
        return lesson_id


async def close_unit(course_id: int, title: str, summary: str) -> int:
    """Create a unit row and assign all unitless lessons in this course to it."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COALESCE(MAX(idx), -1) FROM course_units WHERE course_id=?", (course_id,)
        ) as cur:
            next_idx = (await cur.fetchone())[0] + 1
        cur = await db.execute(
            "INSERT INTO course_units (course_id, idx, title, summary) VALUES (?, ?, ?, ?)",
            (course_id, next_idx, (title or "").strip(), (summary or "").strip()),
        )
        unit_id = cur.lastrowid
        await db.execute(
            "UPDATE course_lessons SET unit_id=? WHERE course_id=? AND unit_id IS NULL",
            (unit_id, course_id),
        )
        await db.commit()
        return unit_id


async def get_lesson(user_id: int, lesson_id: int) -> dict | None:
    """Return a lesson (ownership-checked). Content is stored at creation time."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT l.id, l.lesson_num, l.title, l.objective,
                      l.content, l.concepts_json, l.llm_debug_json,
                      l.score, l.completed_at,
                      c.target_lang, c.level, c.id AS course_id,
                      COALESCE(u.theme, '') AS theme
               FROM course_lessons l
               JOIN courses c ON c.id = l.course_id
               LEFT JOIN course_units u ON u.id = l.unit_id
               WHERE l.id=? AND c.user_id=?""",
            (lesson_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        lesson = dict(row)
        lesson["content"]   = json.loads(lesson["content"])        if lesson["content"]        else None
        lesson["llm_debug"] = json.loads(lesson["llm_debug_json"]) if lesson["llm_debug_json"] else None
        lesson.pop("llm_debug_json", None)
        lesson["concepts"]  = json.loads(lesson["concepts_json"] or "[]")
        lesson.pop("concepts_json", None)
        lesson["completed"] = lesson["completed_at"] is not None
        return lesson


CROWN_MAX = 3   # skill-tree crown cap; each completion bumps the crown by 1


async def complete_lesson(user_id: int, lesson_id: int, score: int) -> tuple[bool, bool, int, bool]:
    """Record (or improve) lesson completion + score. Ownership-checked.
    Returns (found, first_completion, crown_level, leveled_up). first_completion is
    True only the FIRST time the lesson is finished, so XP is awarded once (replays
    don't re-award). Every completion bumps the crown level by 1 up to CROWN_MAX;
    leveled_up is True when this completion actually raised the crown (i.e. it
    wasn't already maxed)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT l.completed_at, l.crown_level FROM course_lessons l
               JOIN courses c ON c.id = l.course_id
               WHERE l.id=? AND c.user_id=?""",
            (lesson_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return (False, False, 0, False)
        first = row[0] is None
        old_crown = row[1] or 0
        crown = min(old_crown + 1, CROWN_MAX)
        await db.execute(
            """UPDATE course_lessons
               SET score        = MAX(COALESCE(score, 0), ?),
                   completed_at = COALESCE(completed_at, datetime('now')),
                   crown_level  = ?
               WHERE id=?""",
            (int(score), crown, lesson_id),
        )
        await db.commit()
        return (True, first, crown, crown > old_crown)


async def record_concept_results(user_id: int, lang: str, results: list[dict]) -> None:
    """Upsert per-concept mastery by incrementing correct + total counters."""
    if not results:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        for r in results:
            key = (r.get("concept_key") or "").strip()
            correct = max(0, int(r.get("correct") or 0))
            total = max(0, int(r.get("total") or 0))
            if not key or total == 0:
                continue
            await db.execute(
                """INSERT INTO concept_mastery
                       (user_id, lang, concept_key, correct, total, last_seen)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(user_id, lang, concept_key) DO UPDATE SET
                       correct   = correct  + excluded.correct,
                       total     = total    + excluded.total,
                       last_seen = excluded.last_seen""",
                (user_id, lang, key, correct, total),
            )
        await db.commit()


async def get_mastery_summary(user_id: int, lang: str) -> list[dict]:
    """Return per-concept mastery rows for a user+language, most-practised first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT concept_key, correct, total, last_seen
               FROM concept_mastery WHERE user_id=? AND lang=?
               ORDER BY total DESC""",
            (user_id, lang),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_concept_content(lang: str, concept_key: str) -> dict | None:
    """Verified canonical grammar artifact for (lang, concept_key), or None.
    Shared across users — not ownership-scoped."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT content FROM concept_content WHERE lang=? AND concept_key=?",
            (lang, concept_key),
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row[0]) if row else None


async def set_concept_content(lang: str, concept_key: str, content: dict) -> None:
    """Cache a verified grammar artifact (shared across users; upsert)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO concept_content (lang, concept_key, content, created_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(lang, concept_key) DO UPDATE SET
                 content=excluded.content, created_at=excluded.created_at""",
            (lang, concept_key, json.dumps(content)),
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
            """SELECT sentence_idx, sentence_text, translation, romanization,
                      CASE WHEN audio_data IS NOT NULL THEN 1 ELSE 0 END AS has_audio
               FROM reader_sentences WHERE text_id=? ORDER BY sentence_idx""",
            (text_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def upsert_reader_sentence(
    text_id: int, idx: int, sentence_text: str,
    translation: str | None = None, audio_data: bytes | None = None,
    romanization: str | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO reader_sentences (text_id, sentence_idx, sentence_text, translation, audio_data, romanization)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(text_id, sentence_idx) DO UPDATE SET
                 translation  = COALESCE(excluded.translation,  translation),
                 audio_data   = COALESCE(excluded.audio_data,   audio_data),
                 romanization = COALESCE(excluded.romanization, romanization)""",
            (text_id, idx, sentence_text, translation, audio_data, romanization),
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


import re as _re

_CJK_RE = _re.compile(r"[^一-鿿㐀-䶿豈-﫿぀-ヿ]")
_NON_ALPHA_RE = _re.compile(r"[\W]", _re.UNICODE)


def _normalize_word(text: str) -> str:
    """Normalize a word for deck-status lookup.

    CJK text: strip everything except CJK/kana characters.
    Latin/other: lowercase and strip non-word characters.
    """
    cjk = _CJK_RE.sub("", text)
    if cjk:
        return cjk
    return _NON_ALPHA_RE.sub("", text).lower()


async def get_word_statuses(user_id: int, words: list[str], target_lang: str, *, exact_only: bool = False) -> dict[str, str]:
    """Return a mapping of word → 'known' | 'weak' for words present in the user's deck.

    Words not in the deck are absent from the result (callers treat absence as 'new').
    Matching is fuzzy: a token matches a card if the normalised token equals the
    normalised card target_text, OR (CJK only) if the card text contains the token
    as a substring (e.g. token '去' inside card '我去旅行').
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

    is_cjk_lang = target_lang in ("yue", "cmn")

    card_lookup: dict[str, str] = {}
    for r in rows:
        key = _normalize_word(r["target_text"])
        if not key:
            continue
        status = "known" if r["max_reps"] >= 2 and r["min_ease"] >= 2.0 else "weak"
        if key not in card_lookup or status == "known":
            card_lookup[key] = status

    result: dict[str, str] = {}
    for word in words:
        norm = _normalize_word(word)
        if not norm:
            continue
        if norm in card_lookup:
            result[word] = card_lookup[norm]
            continue
        # CJK substring heuristic: token '去' inside card '我去旅行'.
        # Cap at 'weak' — user may have learned the phrase, not the isolated character.
        if is_cjk_lang and not exact_only:
            best: str | None = None
            for card_norm, status in card_lookup.items():
                if norm in card_norm:
                    if best is None or status == "known":
                        best = status
            if best is not None:
                result[word] = "weak" if best == "known" else best
    return result


async def match_cards_by_target(user_id: int, targets: list[str], target_lang: str, label_id: int,
                                sources: list[str] | None = None) -> dict[str, dict]:
    """For each target word that exists as a card, return
    {target_text: {id, source_text, in_label}}. `in_label` says whether that card
    is already tagged with `label_id`. When duplicates share a target_text, prefer
    the copy already in the label. Used to route populate suggestions: in-deck words
    not yet in the label become 'tag this card' suggestions.

    Also matches by source_text (English) if `sources` is provided — this catches
    cards where the LLM's target form differs slightly from what's in the deck."""
    if not targets:
        return {}
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in targets)
        where = f"c.target_text IN ({placeholders})"
        params: list = [label_id, user_id, target_lang, *targets]
        if sources:
            src_placeholders = ",".join("?" for _ in sources)
            where = f"({where} OR LOWER(c.source_text) IN ({src_placeholders}))"
            params.extend(s.lower() for s in sources)
        async with conn.execute(
            f"""SELECT c.id, c.target_text, c.source_text,
                       EXISTS(SELECT 1 FROM card_labels cl
                              WHERE cl.card_id=c.id AND cl.label_id=?) AS in_label
                FROM cards c
                WHERE c.user_id=? AND c.target_lang=? AND {where}""",
            params,
        ) as cur:
            rows = await cur.fetchall()
    result: dict[str, dict] = {}
    for r in rows:
        prev = result.get(r["target_text"])
        if prev is None or (r["in_label"] and not prev["in_label"]):
            result[r["target_text"]] = {
                "id": r["id"], "source_text": r["source_text"], "in_label": bool(r["in_label"]),
            }
    return result


async def get_label_words(user_id: int, label_id: int, target_lang: str, limit: int = 60) -> list[dict]:
    """The vocab already tagged with a label — [{target_text, source_text}], newest first.

    Fed to the populate LLM so it learns the label's granularity/style and avoids
    re-suggesting words already in the label."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT c.target_text, c.source_text
               FROM cards c
               JOIN card_labels cl ON cl.card_id = c.id
               WHERE cl.label_id=? AND c.user_id=? AND c.target_lang=?
               ORDER BY c.id DESC LIMIT ?""",
            (label_id, user_id, target_lang, limit),
        ) as cur:
            return [{"target_text": r["target_text"], "source_text": r["source_text"]}
                    for r in await cur.fetchall()]


async def get_known_words(user_id: int, target_lang: str, limit: int = 150) -> list[dict]:
    """The user's well-known deck words, strongest first — fed into lesson/tutor
    prompts so generation builds on what the learner already knows.

    'Known' = the primary `target` face has graduated (no learning step, seen at
    least once) AND has real traction (repetitions ≥ 2 or interval ≥ 3 days).
    Returns lean rows: [{target_text, gloss}] (gloss = the card's English side).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT c.target_text, c.source_text AS gloss
               FROM cards c
               JOIN card_faces cf ON cf.card_id = c.id AND cf.face = 'target'
               WHERE c.user_id = ? AND c.target_lang = ? AND c.suspended = 0
                 AND cf.learning_step IS NULL AND cf.first_seen_date IS NOT NULL
                 AND (cf.repetitions >= 2 OR cf.interval_days >= 3)
               ORDER BY cf.interval_days DESC, cf.repetitions DESC, c.id ASC
               LIMIT ?""",
            (user_id, target_lang, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def count_known_words(user_id: int, target_lang: str) -> int:
    """How many words meet the same 'known' bar as get_known_words (no limit).
    Used to pick the drill-vocab strategy: small decks pass the whole list to the
    model; large decks fall back to embedding-snapping a relevant subset."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COUNT(*)
               FROM cards c
               JOIN card_faces cf ON cf.card_id = c.id AND cf.face = 'target'
               WHERE c.user_id = ? AND c.target_lang = ? AND c.suspended = 0
                 AND cf.learning_step IS NULL AND cf.first_seen_date IS NOT NULL
                 AND (cf.repetitions >= 2 OR cf.interval_days >= 3)""",
            (user_id, target_lang),
        ) as cur:
            return (await cur.fetchone())[0]


async def get_weak_cards(user_id: int, target_lang: str, limit: int = 12) -> list[dict]:
    """Deck words the user keeps struggling with (low ease or relapsed into
    learning after having been seen), weakest first — surfaced to the lesson
    author / tutor so they get extra in-context practice.
    Returns lean rows: [{target_text, gloss}]."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT c.target_text, c.source_text AS gloss
               FROM cards c
               JOIN card_faces cf ON cf.card_id = c.id AND cf.face = 'target'
               WHERE c.user_id = ? AND c.target_lang = ? AND c.suspended = 0
                 AND cf.first_seen_date IS NOT NULL
                 AND (cf.ease_factor <= 2.0
                      OR (cf.learning_step IS NOT NULL AND cf.repetitions > 0))
               ORDER BY cf.ease_factor ASC, c.id ASC
               LIMIT ?""",
            (user_id, target_lang, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_recent_cards(user_id: int, target_lang: str, limit: int = 15) -> list[dict]:
    """The most recently ADDED deck words for a language, newest first — regardless
    of SRS graduation. This is the cross-app adaptivity signal for the lesson
    planner: words the learner just picked up via the tutor chat or flashcards
    (which `get_known_words` won't surface until they've graduated) so the next
    lesson can build on them. Returns lean rows: [{target_text, gloss}]."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT target_text, source_text AS gloss
               FROM cards
               WHERE user_id = ? AND target_lang = ? AND suspended = 0
               ORDER BY id DESC
               LIMIT ?""",
            (user_id, target_lang, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_cefr_distribution(user_id: int) -> dict:
    """Return counts of cards at each CEFR level plus an unlabelled count."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT cefr_level, COUNT(*) AS cnt
               FROM cards
               WHERE user_id = ? AND suspended = 0
               GROUP BY cefr_level""",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    levels = {"A1": 0, "A2": 0, "B1": 0, "B2": 0, "C1": 0, "C2": 0, "unknown": 0}
    for r in rows:
        key = r["cefr_level"] if r["cefr_level"] in levels else "unknown"
        levels[key] += r["cnt"]
    return levels


_KNOWN_WHERE = (
    "cf.learning_step IS NULL AND cf.first_seen_date IS NOT NULL "
    "AND (cf.repetitions >= 2 OR cf.interval_days >= 3)"
)


async def get_known_cefr_distribution(user_id: int, target_lang: str) -> dict:
    """CEFR-level counts among the learner's KNOWN words for one language (same bar
    as get_known_words). Feeds the large-deck drill prompt a rough vocab profile."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT c.cefr_level AS lvl, COUNT(*) AS cnt
                FROM cards c
                JOIN card_faces cf ON cf.card_id = c.id AND cf.face = 'target'
                WHERE c.user_id = ? AND c.target_lang = ? AND c.suspended = 0
                  AND {_KNOWN_WHERE}
                GROUP BY c.cefr_level""",
            (user_id, target_lang),
        ) as cur:
            rows = await cur.fetchall()
    levels = {"A1": 0, "A2": 0, "B1": 0, "B2": 0, "C1": 0, "C2": 0, "unknown": 0}
    for r in rows:
        key = r["lvl"] if r["lvl"] in levels else "unknown"
        levels[key] += r["cnt"]
    return levels


async def get_known_words_missing_cefr(user_id: int, target_lang: str, limit: int = 60) -> list[str]:
    """Known words with no valid CEFR tag (added via lesson/tutor/starter paths that
    skip translation's CEFR step) — for bounded lazy backfill."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""SELECT c.target_text
                FROM cards c
                JOIN card_faces cf ON cf.card_id = c.id AND cf.face = 'target'
                WHERE c.user_id = ? AND c.target_lang = ? AND c.suspended = 0
                  AND {_KNOWN_WHERE}
                  AND (c.cefr_level IS NULL OR c.cefr_level NOT IN ('A1','A2','B1','B2','C1','C2'))
                LIMIT ?""",
            (user_id, target_lang, limit),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def set_cards_cefr(user_id: int, target_lang: str, mapping: dict[str, str]) -> None:
    """Backfill cefr_level on the user's cards by target_text (only where still unset)."""
    valid = {"A1", "A2", "B1", "B2", "C1", "C2"}
    rows = [(lvl, user_id, target_lang, w) for w, lvl in mapping.items() if lvl in valid]
    if not rows:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "UPDATE cards SET cefr_level = ? "
            "WHERE user_id = ? AND target_lang = ? AND target_text = ? "
            "AND (cefr_level IS NULL OR cefr_level NOT IN ('A1','A2','B1','B2','C1','C2'))",
            rows,
        )
        await db.commit()


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


async def record_study_activity(user_id: int) -> None:
    """Mark today as an active learning day for the streak. Called for ANY
    meaningful activity — SRS reviews, completing a lesson, or a tutor turn —
    so the 🔥 streak reflects all study, not only flashcard reviews."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO study_activity (user_id, study_date) VALUES (?, date('now'))",
            (user_id,),
        )
        await db.commit()


# ── Tutor chat ─────────────────────────────────────────────────────────────────

async def create_tutor_conversation(user_id: int, lang: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tutor_conversations (user_id, lang) VALUES (?, ?)",
            (user_id, lang),
        )
        await db.commit()
        return cur.lastrowid


async def list_tutor_conversations(user_id: int, lang: str, limit: int = 30) -> list[dict]:
    """Most recent first. Lean rows for the conversation drawer."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, title, created_at, updated_at
               FROM tutor_conversations
               WHERE user_id=? AND lang=?
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (user_id, lang, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_tutor_conversation(user_id: int, conv_id: int) -> dict | None:
    """Ownership-checked conversation row, or None. `active_drill_id` is non-NULL
    while a drill sub-session is in progress (else NULL)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, lang, title, active_drill_id FROM tutor_conversations WHERE id=? AND user_id=?",
            (conv_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_tutor_conversation(user_id: int, conv_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM tutor_messages WHERE conversation_id IN "
            "(SELECT id FROM tutor_conversations WHERE id=? AND user_id=?)",
            (conv_id, user_id),
        )
        await db.execute(
            "DELETE FROM tutor_conversations WHERE id=? AND user_id=?",
            (conv_id, user_id),
        )
        await db.commit()


async def get_tutor_messages(user_id: int, conv_id: int, limit: int = 200) -> list[dict]:
    """Messages in chronological order (ownership-checked via the join).
    `drill_id` is non-NULL for messages that belong to a drill sub-session
    (grouped + collapsible client-side); `drill_skill` labels that group."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT m.id, m.role, m.content, m.drill_id, m.drill_skill, m.created_at
               FROM tutor_messages m
               JOIN tutor_conversations c ON c.id = m.conversation_id
               WHERE m.conversation_id=? AND c.user_id=?
               ORDER BY m.id ASC LIMIT ?""",
            (conv_id, user_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_tutor_message(user_id: int, conv_id: int, role: str, content: str,
                            drill_id: int | None = None, drill_skill: str | None = None) -> int | None:
    """Append a message (ownership-checked). The first user message becomes the
    conversation title. Pass `drill_id`/`drill_skill` to tag the message as part
    of a drill sub-session. Returns the message id, or None if not the owner."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT title FROM tutor_conversations WHERE id=? AND user_id=?",
            (conv_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        cur = await db.execute(
            "INSERT INTO tutor_messages (conversation_id, role, content, drill_id, drill_skill) "
            "VALUES (?, ?, ?, ?, ?)",
            (conv_id, role, content, drill_id, drill_skill),
        )
        msg_id = cur.lastrowid
        # Don't let a drill's opening tutor message become the conversation title.
        if role == "user" and not (row[0] or "").strip() and drill_id is None:
            await db.execute(
                "UPDATE tutor_conversations SET title=? WHERE id=?",
                (content[:60], conv_id),
            )
        await db.execute(
            "UPDATE tutor_conversations SET updated_at=datetime('now') WHERE id=?",
            (conv_id,),
        )
        await db.commit()
        return msg_id


async def set_tutor_message_drill(user_id: int, msg_id: int, drill_id: int, drill_skill: str) -> None:
    """Tag an already-inserted message as a drill turn (used for the opener, whose
    own id becomes the drill group id)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE tutor_messages SET drill_id=?, drill_skill=?
               WHERE id=? AND conversation_id IN
                 (SELECT id FROM tutor_conversations WHERE user_id=?)""",
            (drill_id, drill_skill, msg_id, user_id),
        )
        await db.commit()


async def set_active_drill(user_id: int, conv_id: int, drill_id: int | None) -> None:
    """Set (start) or clear (end) the conversation's active drill sub-session."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tutor_conversations SET active_drill_id=? WHERE id=? AND user_id=?",
            (drill_id, conv_id, user_id),
        )
        await db.commit()


# ── Points ledger (light gamification) ──────────────────────────────────────────

async def add_points(user_id: int, lang: str, points: int, reason: str = "") -> None:
    if points <= 0:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO points_ledger (user_id, lang, points, reason) VALUES (?, ?, ?, ?)",
            (user_id, lang, int(points), (reason or "").strip()[:200]),
        )
        await db.commit()


async def get_points_total(user_id: int, lang: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(points), 0) FROM points_ledger WHERE user_id=? AND lang=?",
            (user_id, lang),
        ) as cur:
            return (await cur.fetchone())[0]


async def get_points_today(user_id: int, lang: str) -> int:
    """XP earned today (all languages) — drives the daily-goal ring. Uses local
    time so the goal resets at the learner's midnight, matching the streak."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(points), 0) FROM points_ledger "
            "WHERE user_id=? AND date(created_at, 'localtime') = date('now', 'localtime')",
            (user_id,),
        ) as cur:
            return (await cur.fetchone())[0]


# ── Embedding cache ───────────────────────────────────────────────────────────
# Shared word→vector cache (a word embeds the same for every user). Vectors are
# stored as packed float32 BLOBs; pack/unpack live in embeddings.py.

async def get_cached_embeddings(lang: str, model: str, words: list[str]) -> dict[str, bytes]:
    """Return {word: vector_blob} for the cached subset of `words`."""
    words = [w for w in {w for w in words} if w]
    if not words:
        return {}
    out: dict[str, bytes] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        # Chunk the IN-list to stay well under SQLite's variable limit.
        for i in range(0, len(words), 400):
            chunk = words[i:i + 400]
            placeholders = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT word, vector FROM embedding_cache "
                f"WHERE lang=? AND model=? AND word IN ({placeholders})",
                (lang, model, *chunk),
            ) as cur:
                async for row in cur:
                    out[row[0]] = row[1]
    return out


async def put_cached_embeddings(lang: str, model: str, vectors: dict[str, bytes]) -> None:
    """Insert {word: vector_blob}; ignore words already cached."""
    if not vectors:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO embedding_cache (lang, model, word, vector) VALUES (?,?,?,?)",
            [(lang, model, w, v) for w, v in vectors.items()],
        )
        await db.commit()


# ── Friends ────────────────────────────────────────────────────────────────────


async def send_friend_request(requester_id: int, addressee_id: int) -> dict:
    """Send a friend request. Returns {ok, error}."""
    if requester_id == addressee_id:
        return {"ok": False, "error": "cannot_self"}
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if relationship already exists in either direction
        async with db.execute(
            """SELECT id, status, requester_id FROM friendships
               WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)""",
            (requester_id, addressee_id, addressee_id, requester_id),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            if existing[1] == "accepted":
                return {"ok": False, "error": "already_friends"}
            return {"ok": False, "error": "request_pending"}
        await db.execute(
            "INSERT INTO friendships (requester_id, addressee_id, status) VALUES (?,?,'pending')",
            (requester_id, addressee_id),
        )
        await db.commit()
    return {"ok": True}


async def respond_friend_request(friendship_id: int, addressee_id: int, accept: bool) -> int | None:
    """Accept or reject a pending request addressed to addressee_id.

    Returns the requester's user_id on success (so the caller can notify them),
    or None if no matching pending request was found.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT requester_id FROM friendships WHERE id=? AND addressee_id=? AND status='pending'",
            (friendship_id, addressee_id),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
        requester_id = row[0]
        if accept:
            await db.execute(
                "UPDATE friendships SET status='accepted' WHERE id=?", (friendship_id,)
            )
        else:
            await db.execute("DELETE FROM friendships WHERE id=?", (friendship_id,))
        await db.commit()
    return requester_id


async def remove_friend(user_id: int, other_user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """DELETE FROM friendships
               WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)""",
            (user_id, other_user_id, other_user_id, user_id),
        )
        await db.commit()


async def get_friends(user_id: int) -> dict:
    """Return friends, pending sent requests, and pending received requests."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT f.id, f.requester_id, f.addressee_id, f.status, f.created_at,
                      r.username AS requester_name, a.username AS addressee_name
               FROM friendships f
               JOIN users r ON r.id = f.requester_id
               JOIN users a ON a.id = f.addressee_id
               WHERE f.requester_id=? OR f.addressee_id=?""",
            (user_id, user_id),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    friends, sent, received = [], [], []
    for r in rows:
        other_id = r["addressee_id"] if r["requester_id"] == user_id else r["requester_id"]
        other_name = r["addressee_name"] if r["requester_id"] == user_id else r["requester_name"]
        entry = {"id": r["id"], "user_id": other_id, "username": other_name, "created_at": r["created_at"]}
        if r["status"] == "accepted":
            friends.append(entry)
        elif r["requester_id"] == user_id:
            sent.append(entry)
        else:
            received.append(entry)
    return {"friends": friends, "sent": sent, "received": received}


# ── Conversations + Messages ───────────────────────────────────────────────────

async def get_or_create_conversation(user1_id: int, user2_id: int) -> dict:
    """Get or create an in-app 1:1 conversation. IDs are normalised (min < max)."""
    a, b = min(user1_id, user2_id), max(user1_id, user2_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM conversations WHERE user1_id=? AND user2_id=? AND platform IS NULL",
            (a, b),
        ) as cur:
            row = await cur.fetchone()
        if row:
            return {"id": row["id"], "created": False}
        cur2 = await db.execute(
            "INSERT INTO conversations (user1_id, user2_id) VALUES (?,?)", (a, b)
        )
        await db.commit()
        return {"id": cur2.lastrowid, "created": True}


async def get_or_create_platform_conversation(
    owner_user_id: int, platform: str, platform_thread_id: str
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id FROM conversations
               WHERE owner_user_id=? AND platform=? AND platform_thread_id=?""",
            (owner_user_id, platform, platform_thread_id),
        ) as cur:
            row = await cur.fetchone()
        if row:
            return row["id"]
        cur2 = await db.execute(
            """INSERT INTO conversations (owner_user_id, platform, platform_thread_id)
               VALUES (?,?,?)""",
            (owner_user_id, platform, platform_thread_id),
        )
        await db.commit()
        return cur2.lastrowid


async def list_conversations(user_id: int) -> list[dict]:
    """All conversations for a user, sorted by last activity."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT c.id, c.user1_id, c.user2_id, c.platform, c.platform_thread_id,
                      c.owner_user_id, c.last_message_at,
                      u1.username AS user1_name, u2.username AS user2_name,
                      (SELECT COUNT(*) FROM messages m
                       WHERE m.conversation_id=c.id AND m.read_at IS NULL
                         AND (m.sender_user_id IS NULL OR m.sender_user_id != ?)) AS unread,
                      (SELECT m2.original_text FROM messages m2
                       WHERE m2.conversation_id=c.id ORDER BY m2.created_at DESC LIMIT 1) AS last_text,
                      (SELECT m2.translations FROM messages m2
                       WHERE m2.conversation_id=c.id ORDER BY m2.created_at DESC LIMIT 1) AS last_translations,
                      (SELECT m2.sender_user_id FROM messages m2
                       WHERE m2.conversation_id=c.id ORDER BY m2.created_at DESC LIMIT 1) AS last_sender_id,
                      (SELECT m2.sender_name FROM messages m2
                       WHERE m2.conversation_id=c.id ORDER BY m2.created_at DESC LIMIT 1) AS last_sender_name
               FROM conversations c
               LEFT JOIN users u1 ON u1.id = c.user1_id
               LEFT JOIN users u2 ON u2.id = c.user2_id
               WHERE c.user1_id=? OR c.user2_id=? OR c.owner_user_id=?
               ORDER BY COALESCE(c.last_message_at, c.created_at) DESC""",
            (user_id, user_id, user_id, user_id),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    result = []
    for r in rows:
        if r["platform"]:
            conv = {
                "id": r["id"], "type": r["platform"],
                "name": r["platform_thread_id"],
                "last_sender_name": r["last_sender_name"],
                "unread": r["unread"], "last_text": r["last_text"],
                "last_translations": r["last_translations"],
                "last_message_at": r["last_message_at"],
            }
        else:
            other_id = r["user2_id"] if r["user1_id"] == user_id else r["user1_id"]
            other_name = r["user2_name"] if r["user1_id"] == user_id else r["user1_name"]
            conv = {
                "id": r["id"], "type": "inapp",
                "other_user_id": other_id, "name": other_name,
                "unread": r["unread"], "last_text": r["last_text"],
                "last_translations": r["last_translations"],
                "last_sender_id": r["last_sender_id"],
                "last_message_at": r["last_message_at"],
            }
        result.append(conv)
    return result


async def get_messages(conversation_id: int, viewer_user_id: int,
                       limit: int = 50, before_id: int | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Verify viewer is a participant
        async with db.execute(
            """SELECT id FROM conversations
               WHERE id=? AND (user1_id=? OR user2_id=? OR owner_user_id=?)""",
            (conversation_id, viewer_user_id, viewer_user_id, viewer_user_id),
        ) as cur:
            if not await cur.fetchone():
                return []
        where = "WHERE m.conversation_id=?"
        params = [conversation_id]
        if before_id:
            where += " AND m.id < ?"
            params.append(before_id)
        async with db.execute(
            f"""SELECT m.id, m.sender_user_id, m.sender_platform_id, m.sender_name,
                       m.original_text, m.original_lang, m.translations, m.analysis,
                       m.created_at, m.read_at, u.username AS sender_username
                FROM messages m LEFT JOIN users u ON u.id = m.sender_user_id
                {where} ORDER BY m.created_at DESC LIMIT ?""",
            [*params, limit],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return list(reversed(rows))


async def get_reactions_for_messages(message_ids: list[int], viewer_user_id: int) -> dict:
    """Return {message_id: {emoji: {count, mine}}} for the given message IDs."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT message_id, emoji, user_id FROM message_reactions WHERE message_id IN ({placeholders})",
            message_ids,
        ) as cur:
            rows = await cur.fetchall()
    result: dict = {}
    for msg_id, emoji, uid in rows:
        r = result.setdefault(msg_id, {}).setdefault(emoji, {"count": 0, "mine": False})
        r["count"] += 1
        if uid == viewer_user_id:
            r["mine"] = True
    return result


async def toggle_reaction(message_id: int, user_id: int, emoji: str) -> bool:
    """Add reaction if not present, remove if already present. Returns True if now added."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM message_reactions WHERE message_id=? AND user_id=? AND emoji=?",
            (message_id, user_id, emoji),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            await db.execute(
                "DELETE FROM message_reactions WHERE message_id=? AND user_id=? AND emoji=?",
                (message_id, user_id, emoji),
            )
            await db.commit()
            return False
        else:
            await db.execute(
                "INSERT OR IGNORE INTO message_reactions (message_id, user_id, emoji) VALUES (?,?,?)",
                (message_id, user_id, emoji),
            )
            await db.commit()
            return True


async def add_message(
    conversation_id: int,
    sender_user_id: int | None,
    original_text: str,
    original_lang: str,
    translations: dict,
    *,
    sender_platform_id: str | None = None,
    sender_name: str | None = None,
    sent_text: str | None = None,
    analysis: dict | None = None,
) -> int:
    trans_json = json.dumps(translations, ensure_ascii=False) if translations else None
    analysis_json = json.dumps(analysis, ensure_ascii=False) if analysis else None
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO messages
               (conversation_id, sender_user_id, sender_platform_id, sender_name,
                original_text, original_lang, translations, sent_text, analysis)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (conversation_id, sender_user_id, sender_platform_id, sender_name,
             original_text, original_lang, trans_json, sent_text, analysis_json),
        )
        msg_id = cur.lastrowid
        await db.execute(
            "UPDATE conversations SET last_message_at=datetime('now') WHERE id=?",
            (conversation_id,),
        )
        await db.commit()
    return msg_id


async def update_message_translations(msg_id: int, translations: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE messages SET translations=? WHERE id=?",
            (json.dumps(translations, ensure_ascii=False), msg_id),
        )
        await db.commit()


async def get_message(msg_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        row = await conn.execute_fetchall(
            """SELECT m.id, m.conversation_id, m.sender_user_id,
                      m.original_text, m.original_lang, m.translations, m.analysis
               FROM messages m
               JOIN conversations c ON c.id = m.conversation_id
               WHERE m.id = ? AND (c.user1_id = ? OR c.user2_id = ?)""",
            (msg_id, user_id, user_id),
        )
        if not row:
            return None
        r = row[0]
        return {
            "id": r["id"], "conversation_id": r["conversation_id"],
            "sender_user_id": r["sender_user_id"],
            "original_text": r["original_text"], "original_lang": r["original_lang"],
            "translations": json.loads(r["translations"]) if r["translations"] else {},
            "analysis": json.loads(r["analysis"]) if r["analysis"] else {},
        }


async def update_message_analysis(msg_id: int, translations: dict, analysis: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE messages SET translations=?, analysis=? WHERE id=?",
            (json.dumps(translations, ensure_ascii=False),
             json.dumps(analysis, ensure_ascii=False), msg_id),
        )
        await conn.commit()


async def mark_conversation_read(conversation_id: int, reader_user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE messages SET read_at=datetime('now')
               WHERE conversation_id=? AND read_at IS NULL
                 AND (sender_user_id IS NULL OR sender_user_id != ?)""",
            (conversation_id, reader_user_id),
        )
        await db.commit()


async def get_total_unread(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COUNT(*) FROM messages m
               JOIN conversations c ON c.id = m.conversation_id
               WHERE (c.user1_id=? OR c.user2_id=? OR c.owner_user_id=?)
                 AND m.read_at IS NULL
                 AND (m.sender_user_id IS NULL OR m.sender_user_id != ?)""",
            (user_id, user_id, user_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def get_pending_friend_request_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM friendships WHERE addressee_id=? AND status='pending'",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


# ── Push subscriptions ─────────────────────────────────────────────────────────

async def add_push_subscription(user_id: int, endpoint: str, p256dh: str, auth_key: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET
                 user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth""",
            (user_id, endpoint, p256dh, auth_key),
        )
        await db.commit()


async def get_push_subscriptions(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=?",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def remove_push_subscription(endpoint: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
        await db.commit()


# ── Media uploads ──────────────────────────────────────────────────────────────

async def add_media_record(media_id: str, user_id: int, conv_id: int | None, size_bytes: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO media (id, user_id, conv_id, size_bytes) VALUES (?, ?, ?, ?)",
            (media_id, user_id, conv_id, size_bytes),
        )
        await db.commit()


async def update_image_message(msg_id: int, description: str, analysis: dict) -> None:
    analysis_json = json.dumps(analysis, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE messages SET original_text=?, analysis=? WHERE id=?",
            (f"📷 {description}", analysis_json, msg_id),
        )
        await db.commit()


# ── Messenger account ──────────────────────────────────────────────────────────

async def get_messenger_account(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT page_id, page_name, page_access_token FROM messenger_accounts WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_messenger_account(user_id: int, page_id: str,
                                   page_access_token: str, page_name: str | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO messenger_accounts (user_id, page_id, page_access_token, page_name)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 page_id=excluded.page_id,
                 page_access_token=excluded.page_access_token,
                 page_name=excluded.page_name,
                 connected_at=datetime('now')""",
            (user_id, page_id, page_access_token, page_name),
        )
        await db.commit()


async def delete_messenger_account(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messenger_accounts WHERE user_id=?", (user_id,))
        await db.commit()


async def get_user_by_messenger_page(page_id: str) -> dict | None:
    """Find the app user who connected this Messenger page."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT u.id, u.username, ma.page_access_token
               FROM messenger_accounts ma JOIN users u ON u.id = ma.user_id
               WHERE ma.page_id=?""",
            (page_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


# ── Connected social accounts (Facebook / Instagram) ───────────────────────────

async def upsert_social_account(user_id: int, provider: str, provider_user_id: str,
                                access_token: str | None, display_name: str | None) -> None:
    """Store/refresh a user's connected social account. access_token is ciphertext."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO social_accounts
                 (user_id, provider, provider_user_id, access_token, display_name)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id, provider) DO UPDATE SET
                 provider_user_id=excluded.provider_user_id,
                 access_token=excluded.access_token,
                 display_name=excluded.display_name,
                 connected_at=datetime('now')""",
            (user_id, provider, provider_user_id, access_token, display_name),
        )
        await db.commit()


async def get_social_account(user_id: int, provider: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT provider, provider_user_id, access_token, display_name, connected_at
               FROM social_accounts WHERE user_id=? AND provider=?""",
            (user_id, provider),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def delete_social_account(user_id: int, provider: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM social_accounts WHERE user_id=? AND provider=?", (user_id, provider)
        )
        await db.commit()


async def get_users_by_social_ids(provider: str, provider_user_ids: list[str],
                                  exclude_user_id: int) -> list[dict]:
    """Map a list of provider account ids → our users who connected that provider.
    Used to surface a learner's app-using social friends. Returns
    [{user_id, username, provider_user_id}]."""
    if not provider_user_ids:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in provider_user_ids)
        async with db.execute(
            f"""SELECT s.user_id, s.provider_user_id, u.username
                FROM social_accounts s JOIN users u ON u.id = s.user_id
                WHERE s.provider=? AND s.user_id!=? AND s.provider_user_id IN ({placeholders})""",
            (provider, exclude_user_id, *provider_user_ids),
        ) as cur:
            rows = await cur.fetchall()
    return [{"user_id": r["user_id"], "username": r["username"],
             "provider_user_id": r["provider_user_id"]} for r in rows]


# ── Feedback / bug reports ───────────────────────────────────────────────────

async def create_feedback(
    user_id: int, type: str, title: str, description: str,
    screenshot_media_id: str | None = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO feedback (user_id, type, title, description, screenshot_media_id)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, type, title, description, screenshot_media_id),
        )
        await db.commit()
        return cur.lastrowid


async def update_feedback_triage(
    feedback_id: int, triage_summary: str, triage_group: str,
    suggested_prompt: str, priority: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE feedback SET triage_summary=?, triage_group=?, suggested_prompt=?,
                      priority=?, updated_at=datetime('now')
               WHERE id=?""",
            (triage_summary, triage_group, suggested_prompt, priority, feedback_id),
        )
        await db.commit()


async def list_feedback(status: str | None = None) -> list[dict]:
    """List all feedback (admin). Optionally filter by status."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            async with db.execute(
                """SELECT f.*, u.username FROM feedback f
                   JOIN users u ON u.id = f.user_id
                   ORDER BY f.created_at DESC""",
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            return [r for r in rows if r["status"] == status]
        async with db.execute(
            """SELECT f.*, u.username FROM feedback f
               JOIN users u ON u.id = f.user_id
               ORDER BY f.created_at DESC""",
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_feedback(feedback_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT f.*, u.username FROM feedback f
               JOIN users u ON u.id = f.user_id
               WHERE f.id=?""",
            (feedback_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_feedback_status(feedback_id: int, status: str, admin_notes: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE feedback SET status=?, admin_notes=?, updated_at=datetime('now')
               WHERE id=?""",
            (status, admin_notes, feedback_id),
        )
        await db.commit()


async def list_feedback_groups() -> list[dict]:
    """Return unique triage_group values with counts, for grouping similar reports."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT triage_group, COUNT(*) as count, GROUP_CONCAT(id) as ids
               FROM feedback
               WHERE triage_group IS NOT NULL AND triage_group != ''
               GROUP BY triage_group
               ORDER BY count DESC"""
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def list_user_feedback(user_id: int) -> list[dict]:
    """List feedback submitted by a specific user."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, type, title, status, priority, created_at
               FROM feedback WHERE user_id=?
               ORDER BY created_at DESC""",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_admin_user_ids() -> list[int]:
    """Return IDs of all admin users."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT id FROM users WHERE is_admin=1") as cur:
            return [row[0] for row in await cur.fetchall()]


# ── Story sharing ────────────────────────────────────────────────────────────


async def publish_story(user_id: int, text_id: int, visibility: str) -> bool:
    if visibility not in ("private", "friends", "public"):
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_reader_cols(db)
        cur = await db.execute(
            "UPDATE reader_texts SET visibility=? WHERE id=? AND user_id=?",
            (visibility, text_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def rate_story(user_id: int, text_id: int, rating: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO story_ratings (text_id, user_id, rating)
               VALUES (?, ?, ?)
               ON CONFLICT(text_id, user_id) DO UPDATE SET rating=excluded.rating""",
            (text_id, user_id, rating),
        )
        await db.commit()


async def get_story_rating(text_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COALESCE(AVG(rating), 0) as avg_rating,
                      COUNT(*) as rating_count
               FROM story_ratings WHERE text_id=?""",
            (text_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {"avg_rating": 0, "rating_count": 0}


async def get_user_story_rating(user_id: int, text_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT rating FROM story_ratings WHERE user_id=? AND text_id=?",
            (user_id, text_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_story_public(text_id: int, requesting_user_id: int) -> dict | None:
    """Get a story if the requesting user has access (own, public, or friend-shared)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_reader_cols(db)
        async with db.execute(
            """SELECT rt.id, rt.title, rt.prompt, rt.content, rt.target_lang,
                      rt.created_at, rt.image_media_id, rt.visibility,
                      rt.difficulty, rt.user_id as owner_id, u.username as author
               FROM reader_texts rt
               JOIN users u ON rt.user_id = u.id
               WHERE rt.id=?""",
            (text_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
        if d["owner_id"] == requesting_user_id:
            return d
        if d["visibility"] == "public":
            return d
        if d["visibility"] == "friends":
            async with db.execute(
                """SELECT 1 FROM friendships
                   WHERE status='accepted'
                     AND ((requester_id=? AND addressee_id=?)
                       OR (addressee_id=? AND requester_id=?))""",
                (requesting_user_id, d["owner_id"],
                 requesting_user_id, d["owner_id"]),
            ) as cur2:
                if await cur2.fetchone():
                    return d
        return None


async def get_reader_sentences_public(text_id: int) -> list[dict]:
    """Get sentences for a story without user_id ownership check."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT sentence_idx, sentence_text, translation, romanization,
                      CASE WHEN audio_data IS NOT NULL THEN 1 ELSE 0 END AS has_audio
               FROM reader_sentences WHERE text_id=? ORDER BY sentence_idx""",
            (text_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def list_community_stories(
    requesting_user_id: int, target_lang: str | None = None,
    difficulty: str | None = None, min_rating: float | None = None,
    search: str | None = None, sort: str = "newest",
) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_reader_cols(db)
        params: list = [requesting_user_id, requesting_user_id, requesting_user_id]
        sql = """
            SELECT rt.id, rt.title, rt.prompt, rt.target_lang,
                   rt.created_at, rt.image_media_id, rt.visibility,
                   rt.difficulty, rt.user_id as owner_id,
                   u.username as author,
                   LENGTH(rt.content) as content_length,
                   COALESCE(sr.avg_r, 0) as avg_rating,
                   COALESCE(sr.cnt, 0) as rating_count
            FROM reader_texts rt
            JOIN users u ON rt.user_id = u.id
            LEFT JOIN (
                SELECT text_id, AVG(rating) as avg_r, COUNT(*) as cnt
                FROM story_ratings GROUP BY text_id
            ) sr ON rt.id = sr.text_id
            WHERE rt.user_id != ?
              AND (rt.visibility = 'public'
                   OR (rt.visibility = 'friends' AND EXISTS (
                       SELECT 1 FROM friendships f
                       WHERE f.status='accepted'
                         AND ((f.requester_id=? AND f.addressee_id=rt.user_id)
                           OR (f.addressee_id=? AND f.requester_id=rt.user_id))
                   )))
        """
        if target_lang:
            sql += " AND rt.target_lang = ?"
            params.append(target_lang)
        if difficulty:
            sql += " AND rt.difficulty = ?"
            params.append(difficulty)
        if search:
            sql += " AND (rt.title LIKE ? OR rt.prompt LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        sql += " GROUP BY rt.id"
        if min_rating and min_rating > 0:
            sql += f" HAVING avg_rating >= ?"
            params.append(min_rating)
        order = {"newest": "rt.created_at DESC",
                 "rating": "avg_rating DESC, rating_count DESC",
                 "popular": "rating_count DESC, avg_rating DESC"}
        sql += f" ORDER BY {order.get(sort, 'rt.created_at DESC')} LIMIT 100"
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Shared decks ─────────────────────────────────────────────────────────────


async def create_shared_deck(
    creator_id: int, name: str, description: str,
    target_lang: str, visibility: str, items: list[dict],
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO shared_decks (creator_id, name, description, target_lang, visibility)
               VALUES (?, ?, ?, ?, ?)""",
            (creator_id, name, description, target_lang, visibility),
        )
        deck_id = cur.lastrowid
        for i, item in enumerate(items):
            await db.execute(
                """INSERT INTO shared_deck_items
                   (deck_id, source_text, target_text, romanization, notes, sort_order, target_lang)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (deck_id, item["source_text"], item["target_text"],
                 item.get("romanization"), item.get("notes"), i,
                 item.get("target_lang") or target_lang),
            )
        await db.commit()
        return deck_id


async def update_shared_deck(
    user_id: int, deck_id: int,
    name: str | None = None, description: str | None = None,
    visibility: str | None = None,
) -> bool:
    updates, params = [], []
    if name is not None:
        updates.append("name=?"); params.append(name)
    if description is not None:
        updates.append("description=?"); params.append(description)
    if visibility is not None:
        updates.append("visibility=?"); params.append(visibility)
    if not updates:
        return False
    params.extend([deck_id, user_id])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE shared_decks SET {', '.join(updates)} WHERE id=? AND creator_id=?",
            params,
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_shared_deck(user_id: int, deck_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name FROM shared_decks WHERE id=? AND creator_id=?",
            (deck_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        deck_name = row[0]
        await db.execute(
            "DELETE FROM shared_decks WHERE id=? AND creator_id=?", (deck_id, user_id)
        )
        label_name = f"📦 {deck_name}"
        async with db.execute(
            "SELECT id FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
            (user_id, label_name),
        ) as cur:
            lrow = await cur.fetchone()
        if lrow:
            label_id = lrow[0]
            await db.execute("DELETE FROM card_labels WHERE label_id=?", (label_id,))
            await db.execute("DELETE FROM labels WHERE id=?", (label_id,))
        await db.commit()
        return True


async def list_my_decks(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT sd.id, sd.name, sd.description, sd.target_lang,
                      sd.visibility, sd.created_at,
                      u.username as creator,
                      sd.creator_id,
                      COUNT(sdi.id) as card_count,
                      (SELECT COUNT(*) FROM deck_imports di WHERE di.deck_id = sd.id) as import_count,
                      COALESCE(dr.avg_r, 0) as avg_rating,
                      COALESCE(dr.cnt, 0) as rating_count
               FROM shared_decks sd
               JOIN users u ON sd.creator_id = u.id
               LEFT JOIN shared_deck_items sdi ON sd.id = sdi.deck_id
               LEFT JOIN (SELECT deck_id, AVG(rating) as avg_r, COUNT(*) as cnt
                          FROM deck_ratings GROUP BY deck_id) dr ON dr.deck_id = sd.id
               WHERE sd.creator_id=?
                  OR EXISTS(SELECT 1 FROM deck_imports di2
                            WHERE di2.deck_id=sd.id AND di2.user_id=?)
               GROUP BY sd.id ORDER BY sd.created_at DESC""",
            (user_id, user_id),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            r["is_creator"] = r["creator_id"] == user_id
            label_name = f"📦 {r['name']}"
            async with db.execute(
                "SELECT id FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
                (user_id, label_name),
            ) as cur:
                lrow = await cur.fetchone()
                r["import_label_id"] = lrow[0] if lrow else None
        return rows


async def list_community_decks(
    requesting_user_id: int, target_lang: str | None = None,
    search: str | None = None, sort: str | None = None,
) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        params: list = [requesting_user_id, requesting_user_id,
                        requesting_user_id, requesting_user_id]
        sql = """
            SELECT sd.id, sd.name, sd.description, sd.target_lang,
                   sd.visibility, sd.created_at,
                   u.username as creator,
                   COUNT(sdi.id) as card_count,
                   (SELECT COUNT(*) FROM deck_imports di WHERE di.deck_id = sd.id) as import_count,
                   EXISTS(SELECT 1 FROM deck_imports di2
                          WHERE di2.deck_id=sd.id AND di2.user_id=?) as imported,
                   COALESCE(dr.avg_r, 0) as avg_rating,
                   COALESCE(dr.cnt, 0) as rating_count
            FROM shared_decks sd
            JOIN users u ON sd.creator_id = u.id
            LEFT JOIN shared_deck_items sdi ON sd.id = sdi.deck_id
            LEFT JOIN (SELECT deck_id, AVG(rating) as avg_r, COUNT(*) as cnt
                       FROM deck_ratings GROUP BY deck_id) dr ON dr.deck_id = sd.id
            WHERE sd.creator_id != ?
              AND (sd.visibility = 'public'
                   OR (sd.visibility = 'friends' AND EXISTS (
                       SELECT 1 FROM friendships f
                       WHERE f.status='accepted'
                         AND ((f.requester_id=? AND f.addressee_id=sd.creator_id)
                           OR (f.addressee_id=? AND f.requester_id=sd.creator_id))
                   )))
        """
        if target_lang:
            sql += " AND sd.target_lang = ?"
            params.append(target_lang)
        if search:
            sql += " AND (sd.name LIKE ? OR sd.description LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        order_map = {
            "rating": "avg_rating DESC, sd.created_at DESC",
            "popular": "import_count DESC, sd.created_at DESC",
        }
        sql += f" GROUP BY sd.id ORDER BY {order_map.get(sort or '', 'sd.created_at DESC')} LIMIT 100"
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_shared_deck(deck_id: int, requesting_user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT sd.*, u.username as creator
               FROM shared_decks sd JOIN users u ON sd.creator_id = u.id
               WHERE sd.id=?""",
            (deck_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
        uid = requesting_user_id
        cid = d["creator_id"]
        if cid != uid and d["visibility"] != "public":
            # Allow access if user has already imported this deck
            async with db.execute(
                "SELECT 1 FROM deck_imports WHERE user_id=? AND deck_id=?",
                (uid, deck_id),
            ) as cur:
                has_imported = bool(await cur.fetchone())
            if has_imported:
                pass  # importer always keeps access
            elif d["visibility"] == "friends":
                async with db.execute(
                    """SELECT 1 FROM friendships
                       WHERE status='accepted'
                         AND ((requester_id=? AND addressee_id=?)
                           OR (addressee_id=? AND requester_id=?))""",
                    (uid, cid, uid, cid),
                ) as cur2:
                    if not await cur2.fetchone():
                        return None
            else:
                return None
        async with db.execute(
            """SELECT source_text, target_text, romanization, notes, target_lang
               FROM shared_deck_items WHERE deck_id=? ORDER BY sort_order""",
            (deck_id,),
        ) as cur:
            d["items"] = [dict(r) for r in await cur.fetchall()]
        langs = {it["target_lang"] for it in d["items"] if it.get("target_lang")}
        d["mixed_lang"] = len(langs) > 1
        async with db.execute(
            "SELECT 1 FROM deck_imports WHERE user_id=? AND deck_id=?",
            (uid, deck_id),
        ) as cur:
            d["imported"] = bool(await cur.fetchone())
        # The "📦 {name}" label exists for both the creator (tagged at creation)
        # and importers — expose its id so either can study the deck directly.
        d["import_label_id"] = None
        if d["imported"] or cid == uid:
            label_name = f"📦 {d['name']}"
            async with db.execute(
                "SELECT id FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
                (uid, label_name),
            ) as cur:
                lrow = await cur.fetchone()
                if lrow:
                    d["import_label_id"] = lrow[0]
        d["import_count"] = 0
        async with db.execute(
            "SELECT COUNT(*) FROM deck_imports WHERE deck_id=?", (deck_id,),
        ) as cur:
            row2 = await cur.fetchone()
            if row2:
                d["import_count"] = row2[0]
        async with db.execute(
            "SELECT AVG(rating), COUNT(*) FROM deck_ratings WHERE deck_id=?",
            (deck_id,),
        ) as cur:
            rrow = await cur.fetchone()
            d["avg_rating"] = rrow[0] or 0
            d["rating_count"] = rrow[1] or 0
        async with db.execute(
            "SELECT rating FROM deck_ratings WHERE deck_id=? AND user_id=?",
            (deck_id, requesting_user_id),
        ) as cur:
            urow = await cur.fetchone()
            d["user_rating"] = urow[0] if urow else None
        return d


async def rate_deck(user_id: int, deck_id: int, rating: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO deck_ratings (deck_id, user_id, rating)
               VALUES (?, ?, ?)
               ON CONFLICT(deck_id, user_id) DO UPDATE SET rating=excluded.rating""",
            (deck_id, user_id, rating),
        )
        await db.commit()


async def get_deck_rating(deck_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT AVG(rating), COUNT(*) FROM deck_ratings WHERE deck_id=?",
            (deck_id,),
        ) as cur:
            row = await cur.fetchone()
            return {
                "avg_rating": row[0] or 0,
                "rating_count": row[1] or 0,
            }


async def import_deck(user_id: int, deck_id: int) -> dict:
    """Import a shared deck: create cards + label in user's account."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT 1 FROM deck_imports WHERE user_id=? AND deck_id=?",
            (user_id, deck_id),
        ) as cur:
            if await cur.fetchone():
                return {"ok": False, "error": "Already imported"}
        async with db.execute(
            "SELECT name, target_lang FROM shared_decks WHERE id=?", (deck_id,),
        ) as cur:
            deck_row = await cur.fetchone()
            if not deck_row:
                return {"ok": False, "error": "Deck not found"}
        deck_name, target_lang = deck_row["name"], deck_row["target_lang"]
        label_name = f"📦 {deck_name}"
        cur = await db.execute(
            """INSERT OR IGNORE INTO labels (user_id, name) VALUES (?, ?)""",
            (user_id, label_name),
        )
        if cur.lastrowid:
            label_id = cur.lastrowid
        else:
            async with db.execute(
                "SELECT id FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
                (user_id, label_name),
            ) as cur2:
                label_id = (await cur2.fetchone())[0]
        async with db.execute(
            """SELECT source_text, target_text, romanization, notes, target_lang
               FROM shared_deck_items WHERE deck_id=? ORDER BY sort_order""",
            (deck_id,),
        ) as cur:
            items = [dict(r) for r in await cur.fetchall()]
        created = 0
        for item in items:
            item_lang = item.get("target_lang") or target_lang
            async with db.execute(
                """SELECT id FROM cards
                   WHERE user_id=? AND target_text=? AND target_lang=?""",
                (user_id, item["target_text"], item_lang),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                card_id = existing[0]
            else:
                c = await db.execute(
                    """INSERT INTO cards
                       (user_id, source_text, target_text, romanization,
                        target_lang, notes, priority)
                       VALUES (?, ?, ?, ?, ?, ?, 3)""",
                    (user_id, item["source_text"], item["target_text"],
                     item.get("romanization"), item_lang, item.get("notes")),
                )
                card_id = c.lastrowid
                for face in ("source", "target", "pronunciation"):
                    await db.execute(
                        """INSERT OR IGNORE INTO card_faces (card_id, face)
                           VALUES (?, ?)""",
                        (card_id, face),
                    )
                created += 1
            await db.execute(
                "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
                (card_id, label_id),
            )
        await db.execute(
            "INSERT OR IGNORE INTO deck_imports (user_id, deck_id) VALUES (?, ?)",
            (user_id, deck_id),
        )
        await db.commit()
        return {"ok": True, "created": created, "total": len(items),
                "label_id": label_id, "label_name": label_name}


async def label_cards_for_deck(user_id: int, deck_name: str, card_ids: list[int]) -> int | None:
    """Tag the creator's own cards with a "📦 {deck_name}" label so they can study
    the deck they just shared. Returns the label id (or None if no cards)."""
    if not card_ids:
        return None
    label_name = f"📦 {deck_name}"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO labels (user_id, name) VALUES (?, ?)",
            (user_id, label_name),
        )
        if cur.lastrowid:
            label_id = cur.lastrowid
        else:
            async with db.execute(
                "SELECT id FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
                (user_id, label_name),
            ) as cur2:
                row = await cur2.fetchone()
                label_id = row[0] if row else None
        if label_id is None:
            return None
        for cid in card_ids:
            # Only label cards that actually belong to this user.
            async with db.execute(
                "SELECT 1 FROM cards WHERE id=? AND user_id=?", (cid, user_id),
            ) as cur3:
                if not await cur3.fetchone():
                    continue
            await db.execute(
                "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
                (cid, label_id),
            )
        await db.commit()
        return label_id
