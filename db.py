import hashlib
import json
import os
import time

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "data/cards.db")
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

# Card face values. 'source' = native-language text, 'target' = target-language text,
# 'pronunciation' = romanization (logographic) or audio-only (Latin script).
FACES = ("source", "target", "pronunciation")

# When a word is brand-new we introduce only this face. The other faces of the
# same card stay locked until the primary face graduates out of learning, so a
# single word no longer shows up three times in the same first session.
PRIMARY_FACE = "target"

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
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target_lang TEXT NOT NULL,
                level TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_units (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL,
                idx INTEGER NOT NULL,
                title TEXT,
                theme TEXT,
                objective TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id INTEGER NOT NULL,
                idx INTEGER NOT NULL,
                title TEXT,
                objective TEXT,
                content TEXT,              -- JSON exercises; NULL = not yet generated
                concepts_introduced TEXT,  -- JSON array of concept keys
                summary TEXT               -- brief summary for future-lesson context
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_concepts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL,
                kind TEXT NOT NULL,        -- vocab | grammar
                key TEXT NOT NULL,         -- stable english-gloss snake_case key
                label TEXT,                -- target-language form (curriculum hint; refined on materialization)
                gloss TEXT,                -- English meaning
                introduced_lesson_id INTEGER,
                UNIQUE(course_id, key)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_progress (
                user_id INTEGER NOT NULL,
                lesson_id INTEGER NOT NULL,
                score INTEGER,
                completed_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, lesson_id)
            )
        """)
        # Verified canonical grammar content — SHARED across users, keyed by
        # (lang, concept_key). Expensive to generate (generator + critic pass),
        # cheap to replay; see grammar_lessons.py.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS concept_content (
                lang TEXT NOT NULL,
                concept_key TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (lang, concept_key)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_units_course ON course_units(course_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_lessons_unit ON course_lessons(unit_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_concepts_course ON course_concepts(course_id)")

        # Backfill face rows for any cards that don't have them yet.
        for face in FACES:
            await db.execute(
                "INSERT OR IGNORE INTO card_faces (card_id, face) SELECT id, ? FROM cards",
                (face,),
            )
        await db.commit()

        # Apply any versioned schema migrations layered on top of the baseline above.
        await _run_migrations(db)


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
        await db.executescript(script)
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


async def get_study_session(
    user_id: int,
    label_id: int | None = None,
    target_lang: str | None = None,
) -> dict:
    """Return due reviews + new cards up to the daily cap, with stats."""
    cap = int(await get_setting(user_id, "new_cards_per_day") or 20)

    extra_filter = ""
    extra_params: tuple = ()
    if label_id is not None:
        extra_filter += (
            " AND cf.card_id IN (SELECT cl.card_id FROM card_labels cl "
            "JOIN labels l ON l.id = cl.label_id "
            "WHERE cl.label_id=? AND l.user_id=?)"
        )
        extra_params += (label_id, user_id)
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
                (user_id, PRIMARY_FACE, PRIMARY_FACE) + label_params + (remaining,),
            ) as cur:
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


async def get_all_faces(
    user_id: int,
    label_id: int | None = None,
    target_lang: str | None = None,
) -> list[dict]:
    extra = ""
    extra_params: tuple = ()
    if label_id is not None:
        extra += (
            " AND cf.card_id IN (SELECT cl.card_id FROM card_labels cl "
            "JOIN labels l ON l.id = cl.label_id "
            "WHERE cl.label_id=? AND l.user_id=?)"
        )
        extra_params += (label_id, user_id)
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
    by_card: dict[int, list[dict]] = {}
    for lr in label_rows:
        by_card.setdefault(lr["card_id"], []).append({"id": lr["id"], "name": lr["name"]})
    for c in cards:
        c["labels"] = by_card.get(c["id"], [])
    return cards


async def get_due_count(
    user_id: int,
    label_id: int | None = None,
    target_lang: str | None = None,
) -> int:
    extra = ""
    extra_params: tuple = ()
    if label_id is not None:
        extra += (
            " AND cf.card_id IN (SELECT cl.card_id FROM card_labels cl "
            "JOIN labels l ON l.id = cl.label_id "
            "WHERE cl.label_id=? AND l.user_id=?)"
        )
        extra_params += (label_id, user_id)
    if target_lang is not None:
        extra += " AND c.target_lang = ?"
        extra_params += (target_lang,)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""SELECT COUNT(*) FROM card_faces cf JOIN cards c ON c.id = cf.card_id
               WHERE c.user_id = ? AND cf.next_review <= datetime('now') AND c.suspended = 0
                 AND (
                       cf.first_seen_date IS NOT NULL
                       OR cf.face = ?
                       OR EXISTS (
                           SELECT 1 FROM card_faces p
                           WHERE p.card_id = cf.card_id AND p.face = ?
                             AND p.first_seen_date IS NOT NULL
                             AND p.learning_step IS NULL
                       )
                 ){extra}""",
            (user_id, PRIMARY_FACE, PRIMARY_FACE) + extra_params,
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


async def list_reader_texts(user_id: int, target_lang: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if target_lang is not None:
            async with db.execute(
                """SELECT id, title, prompt, target_lang, created_at
                   FROM reader_texts WHERE user_id=? AND target_lang=? ORDER BY created_at DESC""",
                (user_id, target_lang),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
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


# ── Learning path (AI course) ─────────────────────────────────────────────────

async def _insert_units(db, course_id: int, units: list[dict], start_idx: int):
    """Insert units (with their lessons + concepts) at idx >= start_idx."""
    for offset, unit in enumerate(units):
        ucur = await db.execute(
            "INSERT INTO course_units (course_id, idx, title, theme, objective) VALUES (?, ?, ?, ?, ?)",
            (course_id, start_idx + offset, (unit.get("title") or "").strip(),
             (unit.get("theme") or "").strip(), (unit.get("objective") or "").strip()),
        )
        unit_id = ucur.lastrowid
        for li, lesson in enumerate(unit.get("lessons", [])):
            concepts = lesson.get("new_concepts") or []
            keys = [(c.get("key") or "").strip() for c in concepts if c.get("key")]
            # Foundations lessons arrive with pre-built content; vocab lessons
            # leave it NULL and generate on first open.
            content = lesson.get("content")
            lcur = await db.execute(
                """INSERT INTO course_lessons (unit_id, idx, title, objective, concepts_introduced, content)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (unit_id, li, (lesson.get("title") or "").strip(),
                 (lesson.get("objective") or "").strip(), json.dumps(keys),
                 json.dumps(content) if content else None),
            )
            lesson_id = lcur.lastrowid
            for c in concepts:
                key = (c.get("key") or "").strip()
                if not key:
                    continue
                await db.execute(
                    """INSERT OR IGNORE INTO course_concepts
                       (course_id, kind, key, label, gloss, introduced_lesson_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (course_id, (c.get("kind") or "vocab").strip(), key,
                     (c.get("label") or "").strip(), (c.get("gloss") or "").strip(), lesson_id),
                )


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


async def create_course(user_id: int, target_lang: str, level: str, curriculum: dict) -> int:
    """Persist a generated curriculum (units → lessons → concepts). Returns course_id."""
    title = (curriculum.get("language") or target_lang) + f" {level}"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO courses (user_id, target_lang, level, title) VALUES (?, ?, ?, ?)",
            (user_id, target_lang, level, title),
        )
        course_id = cur.lastrowid
        await _insert_units(db, course_id, curriculum.get("units", []), 0)
        await db.commit()
        return course_id


async def get_course_concept_digest(course_id: int, limit: int = 250) -> str:
    """Compact comma-separated list of taught concept glosses — fed to the
    curriculum generator as `known_summary` when extending a course."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT gloss, label FROM course_concepts WHERE course_id=? LIMIT ?",
            (course_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    items = [(r["gloss"] or r["label"] or "").strip() for r in rows]
    items = [i for i in items if i]
    return ", ".join(items)


async def append_units(user_id: int, course_id: int, curriculum: dict, new_level: str) -> int:
    """Append generated units to an existing course (continuation). Updates the
    course level/title. Returns the number of units added (0 if not owned)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT target_lang FROM courses WHERE id=? AND user_id=?", (course_id, user_id)
        ) as cur:
            course = await cur.fetchone()
        if not course:
            return 0
        async with db.execute(
            "SELECT COALESCE(MAX(idx), -1) AS m FROM course_units WHERE course_id=?", (course_id,)
        ) as cur:
            start_idx = (await cur.fetchone())["m"] + 1
        units = curriculum.get("units", [])
        await _insert_units(db, course_id, units, start_idx)
        title = (curriculum.get("language") or course["target_lang"]) + f" {new_level}"
        await db.execute(
            "UPDATE courses SET level=?, title=? WHERE id=?", (new_level, title, course_id)
        )
        await db.commit()
        return len(units)


async def get_courses(user_id: int, target_lang: str | None = None) -> list[dict]:
    """List the user's courses (optionally filtered by language), newest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT id, target_lang, level, title, status, created_at FROM courses WHERE user_id=?"
        params: tuple = (user_id,)
        if target_lang is not None:
            sql += " AND target_lang=?"
            params += (target_lang,)
        sql += " ORDER BY created_at DESC"
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_course(user_id: int, course_id: int) -> dict | None:
    """Return the full nested course (units → lessons) with per-lesson concept
    counts and unlock status (computed from progress: first not-done lesson is
    'available', earlier ones 'done', later ones 'locked')."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, target_lang, level, title, status, created_at FROM courses WHERE id=? AND user_id=?",
            (course_id, user_id),
        ) as cur:
            course = await cur.fetchone()
        if not course:
            return None
        course = dict(course)

        async with db.execute(
            "SELECT lesson_id, score, completed_at FROM course_progress WHERE user_id=?",
            (user_id,),
        ) as cur:
            done = {r["lesson_id"]: dict(r) for r in await cur.fetchall()}

        async with db.execute(
            "SELECT id, idx, title, theme, objective FROM course_units WHERE course_id=? ORDER BY idx",
            (course_id,),
        ) as cur:
            units = [dict(r) for r in await cur.fetchall()]

        first_available_set = False
        for unit in units:
            async with db.execute(
                """SELECT id, idx, title, objective,
                          (content IS NOT NULL) AS generated, concepts_introduced
                   FROM course_lessons WHERE unit_id=? ORDER BY idx""",
                (unit["id"],),
            ) as cur:
                lessons = [dict(r) for r in await cur.fetchall()]
            for lesson in lessons:
                keys = json.loads(lesson.get("concepts_introduced") or "[]")
                lesson["concept_count"] = len(keys)
                lesson.pop("concepts_introduced", None)
                if lesson["id"] in done:
                    lesson["status"] = "done"
                    lesson["score"] = done[lesson["id"]]["score"]
                elif not first_available_set:
                    lesson["status"] = "available"
                    first_available_set = True
                else:
                    lesson["status"] = "locked"
            unit["lessons"] = lessons
        course["units"] = units
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


async def delete_course(user_id: int, course_id: int):
    """Delete a course and all its units/lessons/concepts/progress (for regeneration)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM courses WHERE id=? AND user_id=?", (course_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return
        async with db.execute(
            "SELECT id FROM course_units WHERE course_id=?", (course_id,)
        ) as cur:
            unit_ids = [r[0] for r in await cur.fetchall()]
        for uid in unit_ids:
            async with db.execute(
                "SELECT id FROM course_lessons WHERE unit_id=?", (uid,)
            ) as cur:
                lesson_ids = [r[0] for r in await cur.fetchall()]
            for lid in lesson_ids:
                await db.execute("DELETE FROM course_progress WHERE lesson_id=?", (lid,))
            await db.execute("DELETE FROM course_lessons WHERE unit_id=?", (uid,))
        await db.execute("DELETE FROM course_units WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM course_concepts WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM courses WHERE id=? AND user_id=?", (course_id, user_id))
        await db.commit()


async def get_lesson(user_id: int, lesson_id: int) -> dict | None:
    """Return a single lesson (ownership-checked) with everything needed to play
    OR generate it: target_lang, title, objective, parsed content (or None),
    its new concepts, the prior-concept pool (for distractors), and done/score."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT l.id, l.unit_id, l.title, l.objective, l.content,
                      l.concepts_introduced, l.idx AS lesson_idx,
                      u.course_id, u.idx AS unit_idx,
                      c.target_lang, c.level
               FROM course_lessons l
               JOIN course_units u ON u.id = l.unit_id
               JOIN courses c ON c.id = u.course_id
               WHERE l.id=? AND c.user_id=?""",
            (lesson_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        lesson = dict(row)
        course_id = lesson["course_id"]
        keys = json.loads(lesson.pop("concepts_introduced") or "[]")

        # New concepts for this lesson (preserve curriculum order).
        concepts: list[dict] = []
        if keys:
            placeholders = ",".join("?" * len(keys))
            async with db.execute(
                f"""SELECT kind, key, label, gloss FROM course_concepts
                    WHERE course_id=? AND key IN ({placeholders})""",
                (course_id, *keys),
            ) as cur:
                by_key = {r["key"]: dict(r) for r in await cur.fetchall()}
            concepts = [by_key[k] for k in keys if k in by_key]
        lesson["concepts"] = concepts

        # Global 1-based lesson number within the course.
        async with db.execute(
            """SELECT COUNT(*) FROM course_lessons l2
               JOIN course_units u2 ON u2.id = l2.unit_id
               WHERE u2.course_id=?
                 AND (u2.idx < ? OR (u2.idx = ? AND l2.idx < ?))""",
            (course_id, lesson["unit_idx"], lesson["unit_idx"], lesson["lesson_idx"]),
        ) as cur:
            lesson["lesson_num"] = (await cur.fetchone())[0] + 1

        # Prior concepts (introduced in earlier lessons) — distractor pool.
        async with db.execute(
            """SELECT cc.label, cc.gloss FROM course_concepts cc
               JOIN course_lessons l2 ON l2.id = cc.introduced_lesson_id
               JOIN course_units u2 ON u2.id = l2.unit_id
               WHERE cc.course_id=?
                 AND (u2.idx < ? OR (u2.idx = ? AND l2.idx < ?))""",
            (course_id, lesson["unit_idx"], lesson["unit_idx"], lesson["lesson_idx"]),
        ) as cur:
            lesson["prior_concepts"] = [dict(r) for r in await cur.fetchall()]

        # Numbered summaries of prior lessons — context for the lesson generator.
        async with db.execute(
            """SELECT lesson_num, title, summary FROM (
                 SELECT l2.id, l2.title, l2.summary,
                        u2.idx AS unit_idx, l2.idx AS lesson_idx,
                        ROW_NUMBER() OVER (ORDER BY u2.idx, l2.idx) AS lesson_num
                 FROM course_lessons l2
                 JOIN course_units u2 ON u2.id = l2.unit_id
                 WHERE u2.course_id=?
               )
               WHERE (unit_idx < ? OR (unit_idx = ? AND lesson_idx < ?))
                 AND summary IS NOT NULL
               ORDER BY lesson_num""",
            (course_id, lesson["unit_idx"], lesson["unit_idx"], lesson["lesson_idx"]),
        ) as cur:
            lesson["prior_lesson_summaries"] = [dict(r) for r in await cur.fetchall()]

        lesson["content"] = json.loads(lesson["content"]) if lesson["content"] else None

        async with db.execute(
            "SELECT score, completed_at FROM course_progress WHERE user_id=? AND lesson_id=?",
            (user_id, lesson_id),
        ) as cur:
            prog = await cur.fetchone()
        lesson["completed"] = prog is not None
        lesson["score"] = prog["score"] if prog else None
        return lesson


async def set_lesson_content(user_id: int, lesson_id: int, content: dict) -> bool:
    """Cache generated exercises on a lesson (ownership-checked)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT 1 FROM course_lessons l
               JOIN course_units u ON u.id = l.unit_id
               JOIN courses c ON c.id = u.course_id
               WHERE l.id=? AND c.user_id=?""",
            (lesson_id, user_id),
        ) as cur:
            if not await cur.fetchone():
                return False
        await db.execute(
            "UPDATE course_lessons SET content=? WHERE id=?",
            (json.dumps(content), lesson_id),
        )
        await db.commit()
        return True


async def set_lesson_summary(lesson_id: int, summary: str) -> None:
    """Store the AI-generated summary for a lesson (used as context for future lessons)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE course_lessons SET summary=? WHERE id=?",
            (summary.strip(), lesson_id),
        )
        await db.commit()


async def complete_lesson(user_id: int, lesson_id: int, score: int) -> bool:
    """Record (or update) lesson completion + score. Ownership-checked."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT 1 FROM course_lessons l
               JOIN course_units u ON u.id = l.unit_id
               JOIN courses c ON c.id = u.course_id
               WHERE l.id=? AND c.user_id=?""",
            (lesson_id, user_id),
        ) as cur:
            if not await cur.fetchone():
                return False
        await db.execute(
            """INSERT INTO course_progress (user_id, lesson_id, score, completed_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(user_id, lesson_id) DO UPDATE SET
                 score=MAX(course_progress.score, excluded.score),
                 completed_at=excluded.completed_at""",
            (user_id, lesson_id, int(score)),
        )
        await db.commit()
        return True


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


async def get_word_statuses(user_id: int, words: list[str], target_lang: str) -> dict[str, str]:
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
        if is_cjk_lang:
            best: str | None = None
            for card_norm, status in card_lookup.items():
                if norm in card_norm:
                    if best is None or status == "known":
                        best = status
            if best is not None:
                result[word] = "weak" if best == "known" else best
    return result


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
