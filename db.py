import contextlib
import hashlib
import importlib.util
import json
import os
import re
import time

import aiosqlite

import tokenizer

DB_PATH = os.getenv("DB_PATH", "data/cards.db")
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


@contextlib.asynccontextmanager
async def connect():
    """Open a DB connection with the PRAGMAs this schema depends on.

    **Every connection in this module must come from here.** `foreign_keys` and
    `synchronous` are per-CONNECTION settings (unlike `journal_mode = WAL`, which
    lives in the DB file header), and SQLite defaults `foreign_keys` to OFF. This
    module opens a fresh connection per call, so setting the pragma once in
    `init()` covered only `init()` — on every other connection the schema's 21
    `ON DELETE CASCADE` clauses were inert.

    That is why deletes here hand-clean their children: they had to. The cascades
    were decorative, and the ones nobody hand-wrote silently leaked — deleting a
    shared deck left its `shared_deck_items`, `deck_imports`, `deck_ratings` and
    `deck_import_cards` rows behind forever. With the pragma on, the declarations
    in the schema are load-bearing again and a new delete path gets cleanup for
    free instead of leaking until somebody notices.

    `synchronous = NORMAL` is likewise per-connection: without it every write paid
    a full fsync, which under WAL is more durability than this app needs.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Must precede any DML: a PRAGMA is a no-op inside an open transaction,
        # and python-sqlite3 opens one implicitly before the first write.
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA synchronous = NORMAL")
        yield db

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
        # Enable Write-Ahead Logging. In the default rollback-journal mode readers
        # block writers and two connections that each hold a read lock and then try
        # to upgrade to a write deadlock and get "database is locked" IMMEDIATELY,
        # regardless of the busy timeout. Our pages fan out many concurrent requests
        # on load — several of which lazily seed rows (Foundations units in
        # /api/courses/active, daily quests in /api/quests) — so that upgrade
        # contention surfaced as sporadic 500s (which the learn page silently
        # rendered as "no course yet"). WAL lets readers and a single writer proceed
        # without blocking each other. journal_mode=WAL is PERSISTENT (stored in the
        # DB file header), so this one statement covers every later connection.
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
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

        # Small reversible snapshots for review undo.  Keeping the server-side
        # SRS state here means a glasses client can revisit an answered card
        # without submitting a second review against an already-mutated face.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS review_history (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                  INTEGER NOT NULL,
                card_id                  INTEGER NOT NULL,
                face                     TEXT NOT NULL,
                previous_next_review     TEXT NOT NULL,
                previous_interval_days   INTEGER NOT NULL,
                previous_ease_factor     REAL NOT NULL,
                previous_repetitions     INTEGER NOT NULL,
                previous_first_seen_date TEXT,
                previous_learning_step   INTEGER,
                xp                       INTEGER NOT NULL DEFAULT 0,
                points_ledger_id         INTEGER,
                review_quest_bumped      INTEGER NOT NULL DEFAULT 0,
                created_at               TEXT NOT NULL DEFAULT (datetime('now')),
                undone_at                TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_history_user "
            "ON review_history(user_id, id DESC)"
        )

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
        if not await _column_exists(db, "labels", "user_id"):
            await db.execute("ALTER TABLE labels ADD COLUMN user_id INTEGER")
        if not await _column_exists(db, "labels", "is_story_label"):
            await db.execute("ALTER TABLE labels ADD COLUMN is_story_label INTEGER NOT NULL DEFAULT 0")
        # The legacy single-user labels table had a GLOBAL UNIQUE(name). The
        # multi-user model needs uniqueness PER USER (idx_labels_user_name below).
        # The global constraint breaks deck import: two users can't both own a
        # "📦 Deck" label, so the importer's INSERT is silently ignored and the
        # follow-up per-user lookup finds nothing → crash. Earlier code only ADDed
        # columns and never actually dropped the constraint (you can't via ALTER),
        # so rebuild the table when the legacy auto-index from UNIQUE is still present.
        async with db.execute("PRAGMA index_list(labels)") as cur:
            _label_idx = await cur.fetchall()
        if any(r[3] == "u" for r in _label_idx):  # origin 'u' = a UNIQUE constraint
            await db.commit()  # close the implicit txn so the PRAGMA takes effect
            await db.execute("PRAGMA foreign_keys=OFF")
            await db.execute("""
                CREATE TABLE labels_rebuild (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    is_story_label INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # Preserve ids so existing card_labels references stay valid; assign
            # any legacy NULL-user labels to the bootstrap admin (user 1), matching
            # bootstrap_admin's legacy-data migration.
            await db.execute("""
                INSERT INTO labels_rebuild (id, user_id, name, is_story_label, created_at)
                SELECT id, COALESCE(user_id, 1), name, COALESCE(is_story_label, 0),
                       COALESCE(created_at, datetime('now'))
                FROM labels
            """)
            await db.execute("DROP TABLE labels")
            await db.execute("ALTER TABLE labels_rebuild RENAME TO labels")
            await db.commit()
            await db.execute("PRAGMA foreign_keys=ON")
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
        # idx_tutor_msgs_conv / idx_points_user are NOT repeated here — migration
        # 016_tutor.sql creates both, and it has already run by this point. Two
        # declarations of one index name cost nothing at runtime (IF NOT EXISTS),
        # but they drift: an index changed in one place and not the other is a
        # silent disagreement about what the schema is.


async def _run_py_migration(path: str, conn) -> None:
    """Run a migration written in Python: a module exposing `async def migrate(conn)`.

    Schema changes stay in .sql. This exists for DATA backfills that SQL can't
    express safely — notably anything that has to read a value back out of a
    `json.dumps()` blob, whose non-ASCII content is \\u-escaped, so a SQL
    `instr()` would quietly match rows with ASCII text and quietly miss every
    other one. The migration runs on the caller's open connection and is
    recorded in schema_migrations exactly like a .sql file."""
    mod_name = "_migration_" + re.sub(r"\W", "_", os.path.basename(path))
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    await module.migrate(conn)


async def _run_migrations(db) -> None:
    """Apply pending migrations from migrations/*.sql and *.py, in filename
    order, once each.

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
        if fname in applied or not fname.endswith((".sql", ".py")):
            continue
        path = os.path.join(MIGRATIONS_DIR, fname)
        try:
            if fname.endswith(".py"):
                await _run_py_migration(path, db)
            else:
                with open(path, encoding="utf-8") as f:
                    await db.executescript(f.read())
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
    async with connect() as db:
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


# The account that owns official / auto-generated community decks (e.g. the
# per-language "Top 100 Words" decks). It never logs in (unusable password) and
# is not a real learner — decks are "official" purely by having this creator_id.
SYSTEM_USERNAME = "__system__"
SYSTEM_DISPLAY_NAME = "Silcoff Labs"


async def get_or_create_system_user(password_hash: str) -> int:
    """Ensure the system deck-owner account exists. Returns its user_id."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (SYSTEM_USERNAME,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row["id"]
        cur = await db.execute(
            """INSERT INTO users
               (username, password_hash, is_admin, display_name, email_verified)
               VALUES (?, ?, 0, ?, 1)""",
            (SYSTEM_USERNAME, password_hash, SYSTEM_DISPLAY_NAME),
        )
        await db.commit()
        return cur.lastrowid


async def get_system_user_id() -> int | None:
    async with connect() as db:
        async with db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (SYSTEM_USERNAME,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


# ── Users ─────────────────────────────────────────────────────────────────────

_USER_COLS = (
    "id, username, email, display_name, password_hash, is_admin, "
    "native_lang, email_verified, created_at, "
    "plan, stripe_customer_id, subscription_status, subscription_period_end, "
    "stripe_subscription_id, cancel_at_period_end, avatar_media_id"
)


async def get_user_by_username(username: str) -> dict | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_USER_COLS} FROM users WHERE username=? COLLATE NOCASE",
            (username,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user_by_email(email: str) -> dict | None:
    async with connect() as db:
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_USER_COLS}{extra} FROM users WHERE {col}=?",
            (token,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user(user_id: int) -> dict | None:
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            "UPDATE users SET plan=? WHERE id=?",
            (plan, user_id),
        )
        await db.commit()


async def list_users() -> list[dict]:
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            "UPDATE users SET email_verified=1, verification_token=NULL WHERE id=?",
            (user_id,),
        )
        await db.commit()


async def set_verification_token(user_id: int, token: str) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE users SET verification_token=? WHERE id=?",
            (token, user_id),
        )
        await db.commit()


async def set_reset_token(user_id: int, token: str | None, expiry: str | None) -> None:
    async with connect() as db:
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
    avatar_media_id: str | None = ...,  # type: ignore[assignment]
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
    if avatar_media_id is not ...:  # explicitly passed (including None to clear)
        fields.append("avatar_media_id=?"); vals.append(avatar_media_id)
    if not fields:
        return
    vals.append(user_id)
    async with connect() as db:
        await db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", vals)
        await db.commit()


async def delete_user(user_id: int):
    async with connect() as db:
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
        await db.execute("DELETE FROM api_tokens WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM users WHERE id=?", (user_id,))
        await db.commit()


async def update_user_password(user_id: int, password_hash: str):
    async with connect() as db:
        await db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        await db.commit()


# ── Sessions ──────────────────────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(token: str, user_id: int, expiry: float) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO sessions (token_hash, user_id, expiry) VALUES (?, ?, ?)",
            (_hash_token(token), user_id, expiry),
        )
        await db.commit()


async def get_session_user(token: str) -> int | None:
    """Return the user_id for a valid session token, or None if missing/expired.
    Expired rows are deleted lazily on lookup."""
    th = _hash_token(token)
    async with connect() as db:
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
    async with connect() as db:
        await db.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
        await db.commit()


async def delete_user_sessions(user_id: int) -> None:
    async with connect() as db:
        await db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        await db.commit()


async def purge_expired_sessions() -> None:
    async with connect() as db:
        await db.execute("DELETE FROM sessions WHERE expiry < ?", (time.time(),))
        await db.commit()


# ── API tokens ────────────────────────────────────────────────────────────────
# Long-lived credentials for the Even Hub WebView. Tokens never expire, are
# stored only as SHA-256 hashes, and are replaced/revoked explicitly.

async def create_api_token(token: str, user_id: int, label: str = "") -> None:
    async with connect() as db:
        await db.execute("DELETE FROM api_tokens WHERE user_id=?", (user_id,))
        await db.execute(
            "INSERT INTO api_tokens (token_hash, user_id, label) VALUES (?, ?, ?)",
            (_hash_token(token), user_id, label),
        )
        await db.commit()


async def get_user_by_api_token(token: str) -> int | None:
    async with connect() as db:
        async with db.execute(
            "SELECT user_id FROM api_tokens WHERE token_hash=?", (_hash_token(token),)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def has_api_token(user_id: int) -> bool:
    async with connect() as db:
        async with db.execute(
            "SELECT 1 FROM api_tokens WHERE user_id=? LIMIT 1", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def revoke_api_tokens(user_id: int) -> None:
    async with connect() as db:
        await db.execute("DELETE FROM api_tokens WHERE user_id=?", (user_id,))
        await db.commit()


# ── Settings ──────────────────────────────────────────────────────────────────

async def count_cards(user_id: int) -> int:
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM cards WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_setting(user_id: int, key: str, default=None):
    async with connect() as db:
        async with db.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(user_id: int, key: str, value):
    async with connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
            (user_id, key, str(value)),
        )
        await db.commit()


# ── Billing & usage metering ────────────────────────────────────────────────

async def get_admin_dashboard_stats() -> dict:
    """Aggregate stats for the admin dashboard — runs in one connection."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row

        # ── Users by tier ──
        async with db.execute(
            """SELECT
                 COUNT(*) AS total,
                 COALESCE(SUM(is_admin),0) AS admins,
                 COALESCE(SUM(CASE WHEN plan='pro' AND stripe_customer_id IS NOT NULL THEN 1 ELSE 0 END),0) AS pro_paid,
                 COALESCE(SUM(CASE WHEN plan='pro' AND stripe_customer_id IS NULL AND NOT is_admin THEN 1 ELSE 0 END),0) AS pro_comped,
                 COALESCE(SUM(CASE WHEN plan='free' AND NOT is_admin THEN 1 ELSE 0 END),0) AS free
               FROM users WHERE username != ?""",
            (SYSTEM_USERNAME,),
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
               FROM users u WHERE u.username != ? ORDER BY u.id""",
            (SYSTEM_USERNAME,),
        ) as cur:
            users = [dict(r) for r in await cur.fetchall()]

        # ── Current streak per user (computed in Python from one query so it
        #    matches get_streak exactly — including each user's own timezone,
        #    since "today" differs per learner) ──
        async with db.execute(
            "SELECT user_id, study_date FROM study_activity ORDER BY user_id, study_date DESC"
        ) as cur:
            _dates_by_user: dict[int, list[str]] = {}
            async for uid, sdate in cur:
                _dates_by_user.setdefault(uid, []).append(sdate)
        async with db.execute(
            "SELECT user_id, value FROM user_settings WHERE key='timezone'"
        ) as cur:
            _tz_by_user = {uid: tz async for uid, tz in cur}
        from datetime import date as _date
        for u in users:
            _today = _date.fromisoformat(
                local_today_str(_tz_by_user.get(u["id"], DEFAULT_TIMEZONE)))
            u["streak"] = _streak_from_dates(_dates_by_user.get(u["id"], []), _today)

        # ── DAU / WAU / MAU (deliberately UTC — a global activity metric, not a
        #    per-learner day) ──
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
                 (SELECT COUNT(*) FROM users WHERE created_at>=datetime('now','-7 days') AND username != ?) AS signups_week,
                 (SELECT COUNT(*) FROM users WHERE created_at>=datetime('now','-30 days') AND username != ?) AS signups_month""",
            (SYSTEM_USERNAME, SYSTEM_USERNAME),
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
    async with connect() as db:
        async with db.execute(
            "SELECT ai_calls FROM usage_counters "
            "WHERE user_id=? AND period=strftime('%Y-%m','now')",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def increment_usage(user_id: int) -> int:
    """Add one AI call to the current month's counter; return the new total."""
    async with connect() as db:
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


# Sentinel user_id for the app-wide (all-users) shared-key DAILY counter, stored
# in usage_counters alongside the per-user rows. Its daily 'YYYY-MM-DD' period
# key never collides with the per-user monthly 'YYYY-MM' rows, and no real user
# has id 0 (AUTOINCREMENT starts at 1), so the row is fully isolated.
_GLOBAL_USAGE_UID = 0


async def get_global_usage_today() -> int:
    """Total shared-key AI calls across ALL users so far today (UTC)."""
    async with connect() as db:
        async with db.execute(
            "SELECT ai_calls FROM usage_counters "
            "WHERE user_id=? AND period=strftime('%Y-%m-%d','now')",
            (_GLOBAL_USAGE_UID,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def increment_global_usage_today() -> int:
    """Add one shared-key AI call to today's app-wide counter; return new total."""
    async with connect() as db:
        await db.execute(
            """INSERT INTO usage_counters (user_id, period, ai_calls)
               VALUES (?, strftime('%Y-%m-%d','now'), 1)
               ON CONFLICT(user_id, period) DO UPDATE SET ai_calls = ai_calls + 1""",
            (_GLOBAL_USAGE_UID,),
        )
        async with db.execute(
            "SELECT ai_calls FROM usage_counters "
            "WHERE user_id=? AND period=strftime('%Y-%m-%d','now')",
            (_GLOBAL_USAGE_UID,),
        ) as cur:
            row = await cur.fetchone()
        await db.commit()
        return row[0] if row else 1


async def get_user_by_stripe_customer(customer_id: str) -> dict | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_USER_COLS} FROM users WHERE stripe_customer_id=?",
            (customer_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_stripe_customer(user_id: int, customer_id: str) -> None:
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {_CARD_COLS} FROM cards WHERE id = ? AND user_id = ?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_face_state(user_id: int, card_id: int, face: str) -> dict | None:
    async with connect() as db:
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
    async with connect() as db:
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

    async with connect() as db:
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
               WHERE c.user_id = ? AND cf.first_seen_date = ?""",
            (user_id, await _local_today(db, user_id)),
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
    async with connect() as db:
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        sql, params = _faces_query(extra, (user_id,) + extra_params)
        async with db.execute(sql, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return await _faces_with_labels(user_id, rows)


async def get_all_cards(user_id: int) -> list[dict]:
    async with connect() as db:
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


async def get_cards_page(
    user_id: int,
    *,
    offset: int = 0,
    limit: int = 60,
    search: str = "",
    target_lang: str = "",
    label_id: int | None = None,
    cefr: set[str] | None = None,
    strength: set[str] | None = None,
    status: set[str] | None = None,
    sort: str = "newest",
) -> dict:
    """Return a bounded, filtered card page without assembling the full deck."""
    start = max(0, offset)
    size = max(1, min(100, limit))
    where = ["c.user_id = ?"]
    params: list = [PRIMARY_FACE, user_id]

    needle = search.strip().lower()
    if needle:
        where.append(
            "(LOWER(c.source_text) LIKE ? OR LOWER(c.target_text) LIKE ? "
            "OR LOWER(c.romanization) LIKE ? OR LOWER(COALESCE(c.notes, '')) LIKE ?)"
        )
        pattern = f"%{needle}%"
        params.extend([pattern] * 4)
    if target_lang:
        where.append("c.target_lang = ?")
        params.append(target_lang)
    if label_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM card_labels cl JOIN labels l ON l.id=cl.label_id "
            "WHERE cl.card_id=c.id AND cl.label_id=? AND l.user_id=?)"
        )
        params.extend([label_id, user_id])
    if cefr:
        values = sorted(cefr)
        where.append(f"c.cefr_level IN ({','.join('?' for _ in values)})")
        params.extend(values)

    strength_sql = {
        "new": "f.first_seen_date IS NULL",
        "learning": "f.first_seen_date IS NOT NULL AND f.learning_step IS NOT NULL",
        "strong": (
            "f.first_seen_date IS NOT NULL AND f.learning_step IS NULL "
            "AND COALESCE(f.interval_days, 1) >= 21 "
            "AND COALESCE(f.ease_factor, 2.5) >= 2.0"
        ),
        "familiar": (
            "f.first_seen_date IS NOT NULL AND f.learning_step IS NULL "
            "AND NOT (COALESCE(f.interval_days, 1) >= 21 "
            "AND COALESCE(f.ease_factor, 2.5) >= 2.0)"
        ),
    }
    if strength:
        where.append("(" + " OR ".join(strength_sql[value] for value in sorted(strength)) + ")")

    status_sql = {
        "active": "COALESCE(c.suspended, 0)=0 AND COALESCE(c.tutor_flag, 0)=0",
        "suspended": "COALESCE(c.suspended, 0)<>0",
        "flagged": "COALESCE(c.tutor_flag, 0)<>0",
    }
    if status:
        where.append("(" + " OR ".join(status_sql[value] for value in sorted(status)) + ")")

    strength_rank = (
        "CASE WHEN f.first_seen_date IS NULL THEN 0 "
        "WHEN f.learning_step IS NOT NULL THEN 1 "
        "WHEN COALESCE(f.interval_days, 1) >= 21 AND COALESCE(f.ease_factor, 2.5) >= 2.0 THEN 3 "
        "ELSE 2 END"
    )
    order_by = {
        "newest": "c.created_at DESC, c.id DESC",
        "oldest": "c.created_at ASC, c.id ASC",
        "priority": "COALESCE(c.priority, 3) DESC, c.id DESC",
        "alpha": "c.target_text COLLATE NOCASE ASC, c.id DESC",
        "cefr": (
            "CASE c.cefr_level WHEN 'A1' THEN 1 WHEN 'A2' THEN 2 WHEN 'B1' THEN 3 "
            "WHEN 'B2' THEN 4 WHEN 'C1' THEN 5 WHEN 'C2' THEN 6 ELSE 99 END, c.id DESC"
        ),
        "strength": f"{strength_rank} ASC, c.id DESC",
    }.get(sort, "c.created_at DESC, c.id DESC")

    joined = "FROM cards c LEFT JOIN card_faces f ON f.card_id=c.id AND f.face=?"
    predicate = " AND ".join(where)
    select_cols = (
        "c.id, c.source_text, c.target_text, c.romanization, c.target_lang, c.notes, "
        "c.priority, c.tutor_flag, c.suspended, c.classifier, c.canonical_card_id, "
        "c.cefr_level, c.created_at, f.ease_factor, f.repetitions, f.interval_days, "
        "f.learning_step, f.first_seen_date"
    )

    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT COUNT(*) {joined} WHERE {predicate}", tuple(params),
        ) as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            f"SELECT {select_cols} {joined} WHERE {predicate} "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?",
            tuple(params) + (size, start),
        ) as cur:
            cards = [dict(row) for row in await cur.fetchall()]

        label_rows = []
        if cards:
            ids = [card["id"] for card in cards]
            placeholders = ",".join("?" for _ in ids)
            async with db.execute(
                f"""SELECT cl.card_id, l.id, l.name
                    FROM card_labels cl JOIN labels l ON l.id=cl.label_id
                    WHERE cl.card_id IN ({placeholders}) AND l.user_id=?""",
                tuple(ids) + (user_id,),
            ) as cur:
                label_rows = await cur.fetchall()

    labels_by_card: dict[int, list[dict]] = {}
    for row in label_rows:
        labels_by_card.setdefault(row["card_id"], []).append(
            {"id": row["id"], "name": row["name"]}
        )
    for card in cards:
        card["labels"] = labels_by_card.get(card["id"], [])
        card["ease_factor"] = card.get("ease_factor") or 2.5
        card["repetitions"] = card.get("repetitions") or 0
        card["interval_days"] = card.get("interval_days") or 1

    return {
        "cards": cards,
        "total": total,
        "offset": start,
        "limit": size,
        "has_more": start + size < total,
    }


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
    async with connect() as db:
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
               WHERE c.user_id = ? AND cf.first_seen_date = ?""",
            (user_id, await _local_today(db, user_id)),
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
    async with connect() as db:
        async with db.execute(
            "SELECT audio_data FROM cards WHERE id = ? AND user_id = ?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_audio(user_id: int, card_id: int, data: bytes):
    async with connect() as db:
        await db.execute(
            "UPDATE cards SET audio_data=? WHERE id=? AND user_id=?",
            (data, card_id, user_id),
        )
        await db.commit()


async def update_face_review(user_id: int, card_id: int, face: str, state: dict):
    async with connect() as db:
        # Ensure the card belongs to this user.
        async with db.execute(
            "SELECT 1 FROM cards WHERE id=? AND user_id=?", (card_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return
        today = await _local_today(db, user_id)
        await db.execute(
            """UPDATE card_faces
               SET interval_days=?, ease_factor=?, repetitions=?, next_review=?,
                   learning_step=?,
                   first_seen_date = CASE WHEN first_seen_date IS NULL THEN ? ELSE first_seen_date END
               WHERE card_id=? AND face=?""",
            (state["interval_days"], state["ease_factor"], state["repetitions"], state["next_review"],
             state.get("learning_step"), today, card_id, face),
        )
        await _mark_study_day(db, user_id, today)
        await db.commit()


async def apply_card_review(
    user_id: int,
    card_id: int,
    face: str,
    state: dict,
    *,
    xp: int = 0,
    lang: str = "yue",
    studied_on: str | None = None,
) -> int | None:
    """Apply a review atomically and return its reversible history id.

    The snapshot is intentionally compact and capped to the newest 100 reviews
    per user.  Undo must happen newest-first, which prevents an old snapshot
    from overwriting a newer answer to the same face.

    `studied_on` credits the streak to the day the learner actually answered
    (offline reviews sync long after the fact); it only moves the activity row,
    never the SRS scheduling, which is always relative to now.
    """
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            """SELECT cf.next_review, cf.interval_days, cf.ease_factor,
                      cf.repetitions, cf.first_seen_date, cf.learning_step
               FROM card_faces cf JOIN cards c ON c.id = cf.card_id
               WHERE cf.card_id=? AND cf.face=? AND c.user_id=?""",
            (card_id, face, user_id),
        ) as cur:
            previous = await cur.fetchone()
        if not previous:
            await conn.rollback()
            return None

        safe_xp = max(0, int(xp))
        history_cur = await conn.execute(
            """INSERT INTO review_history
               (user_id, card_id, face, previous_next_review,
                previous_interval_days, previous_ease_factor,
                previous_repetitions, previous_first_seen_date,
                previous_learning_step, xp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, card_id, face, previous["next_review"],
                previous["interval_days"], previous["ease_factor"],
                previous["repetitions"], previous["first_seen_date"],
                previous["learning_step"], safe_xp,
            ),
        )
        history_id = history_cur.lastrowid

        today = await _local_today(conn, user_id)
        await conn.execute(
            """UPDATE card_faces
               SET interval_days=?, ease_factor=?, repetitions=?, next_review=?,
                   learning_step=?,
                   first_seen_date = CASE
                       WHEN first_seen_date IS NULL THEN ?
                       ELSE first_seen_date
                   END
               WHERE card_id=? AND face=?""",
            (
                state["interval_days"], state["ease_factor"], state["repetitions"],
                state["next_review"], state.get("learning_step"), today, card_id, face,
            ),
        )
        await _mark_study_day(conn, user_id, studied_on or today)

        quest_cur = await conn.execute(
            """UPDATE daily_quests SET progress = progress + 1
               WHERE user_id=? AND quest_date=? AND quest_key='reviews'""",
            (user_id, today),
        )
        quest_bumped = int(quest_cur.rowcount > 0)

        points_ledger_id = None
        if safe_xp:
            points_cur = await conn.execute(
                """INSERT INTO points_ledger (user_id, lang, points, reason)
                   VALUES (?, ?, ?, 'review')""",
                (user_id, lang, safe_xp),
            )
            points_ledger_id = points_cur.lastrowid

        await conn.execute(
            """UPDATE review_history
               SET points_ledger_id=?, review_quest_bumped=? WHERE id=?""",
            (points_ledger_id, quest_bumped, history_id),
        )
        await conn.execute(
            """DELETE FROM review_history
               WHERE user_id=? AND id NOT IN (
                   SELECT id FROM review_history
                   WHERE user_id=? ORDER BY id DESC LIMIT 100
               )""",
            (user_id, user_id),
        )
        await conn.commit()
        return int(history_id)


async def undo_card_review(user_id: int, history_id: int) -> dict | None:
    """Undo the user's latest active review, returning the reversed XP.

    Returning ``None`` means the id is absent/already undone.  ``out_of_order``
    protects newer scheduling work when a stale client attempts an old undo.
    """
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            """SELECT * FROM review_history
               WHERE user_id=? AND undone_at IS NULL
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        ) as cur:
            latest = await cur.fetchone()
        if not latest or latest["id"] != history_id:
            await conn.rollback()
            if latest:
                return {"out_of_order": True, "latest_id": latest["id"]}
            return None

        restored = await conn.execute(
            """UPDATE card_faces
               SET next_review=?, interval_days=?, ease_factor=?, repetitions=?,
                   first_seen_date=?, learning_step=?
               WHERE card_id=? AND face=? AND card_id IN
                   (SELECT id FROM cards WHERE user_id=?)""",
            (
                latest["previous_next_review"], latest["previous_interval_days"],
                latest["previous_ease_factor"], latest["previous_repetitions"],
                latest["previous_first_seen_date"], latest["previous_learning_step"],
                latest["card_id"], latest["face"], user_id,
            ),
        )
        if restored.rowcount == 0:
            await conn.rollback()
            return None

        if latest["points_ledger_id"] is not None:
            await conn.execute(
                "DELETE FROM points_ledger WHERE id=? AND user_id=?",
                (latest["points_ledger_id"], user_id),
            )
        if latest["review_quest_bumped"]:
            await conn.execute(
                """UPDATE daily_quests SET progress = MAX(0, progress - 1)
                   WHERE user_id=? AND quest_date=? AND quest_key='reviews'""",
                (user_id, await _local_today(conn, user_id)),
            )
        await conn.execute(
            "UPDATE review_history SET undone_at=datetime('now') WHERE id=?",
            (history_id,),
        )
        # We deliberately retain today's study_activity marker: another feature
        # may have earned it, and a boolean day row cannot safely encode provenance.
        await conn.commit()
        return {"out_of_order": False, "xp": latest["xp"]}


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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            "UPDATE cards SET priority=? WHERE id=? AND user_id=?",
            (max(1, min(5, priority)), card_id, user_id),
        )
        await db.commit()


async def set_card_tutor_flag(user_id: int, card_id: int, flagged: bool):
    async with connect() as db:
        await db.execute(
            "UPDATE cards SET tutor_flag=? WHERE id=? AND user_id=?",
            (1 if flagged else 0, card_id, user_id),
        )
        await db.commit()


async def set_card_suspended(user_id: int, card_id: int, suspended: bool):
    async with connect() as db:
        await db.execute(
            "UPDATE cards SET suspended=? WHERE id=? AND user_id=?",
            (1 if suspended else 0, card_id, user_id),
        )
        await db.commit()


async def reset_card_to_new(user_id: int, card_id: int):
    async with connect() as db:
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
    async with connect() as db:
        await db.execute("UPDATE cards SET embedding=? WHERE id=?", (embedding_json, card_id))
        await db.commit()


async def get_all_embeddings(user_id: int) -> list[dict]:
    """Return all cards that have embeddings, for similarity search."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, source_text, target_text, embedding
               FROM cards WHERE user_id=? AND embedding IS NOT NULL""",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_cards_missing_embedding(user_id: int, limit: int) -> list[dict]:
    """Cards with no stored embedding yet — for lazy backfill in suggest-cards."""
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            "UPDATE cards SET classifier=? WHERE id=? AND user_id=?",
            (classifier or "", card_id, user_id),
        )
        await db.commit()


async def set_canonical_card(user_id: int, card_id: int, canonical_id: int | None) -> bool:
    """Set (or clear) the canonical card pointer. Returns False if card not found."""
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        await _ensure_reader_cols(db)
        if target_lang is not None:
            async with db.execute(
                """SELECT id, title, prompt, target_lang, created_at, visibility, difficulty
                   FROM reader_texts WHERE user_id=? AND target_lang=? ORDER BY created_at DESC""",
                (user_id, target_lang),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            """SELECT id, title, prompt, target_lang, created_at, visibility, difficulty
               FROM reader_texts WHERE user_id=? ORDER BY created_at DESC""",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_reader_text(user_id: int, text_id: int) -> dict | None:
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            "DELETE FROM reader_texts WHERE id=? AND user_id=?", (text_id, user_id)
        )
        await db.commit()


# ── Learning path (AI course) ─────────────────────────────────────────────────

async def create_course(user_id: int, target_lang: str, level: str) -> int:
    """Create an empty course. Lessons are generated one at a time on demand."""
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, target_lang, level, status, created_at, active_plan "
            "FROM courses WHERE id=? AND user_id=?",
            (course_id, user_id),
        ) as cur:
            course = await cur.fetchone()
        if not course:
            return None
        course = dict(course)
        try:
            active_plan = json.loads(course.pop("active_plan") or "null")
        except (ValueError, TypeError):
            active_plan = None

        async with db.execute(
            "SELECT COUNT(*) FROM lesson_queue WHERE course_id=?", (course_id,)
        ) as cur:
            course["queued_lessons"] = (await cur.fetchone())[0]

        # Completed units (those with assigned lessons). Textbook units sort by
        # the BOOK's chapter order (then their own creation order), so a book
        # read out of order still lists its units the way the book does.
        async with db.execute(
            """SELECT u.id, u.idx, u.title, u.summary, u.theme, u.checkpoint_passed,
                      u.checkpoint_score, u.textbook_id, u.chapter_idx,
                      (SELECT t.title FROM textbooks t WHERE t.id = u.textbook_id) AS book_title
               FROM course_units u WHERE u.course_id=?
               ORDER BY CASE WHEN u.theme='textbook' THEN 1 ELSE 0 END,
                        CASE WHEN u.theme='textbook'
                             THEN COALESCE(u.textbook_id, 0) ELSE 0 END,
                        CASE WHEN u.theme='textbook'
                             THEN COALESCE(u.chapter_idx, 9999) ELSE 0 END,
                        u.idx""",
            (course_id,),
        ) as cur:
            units = [dict(r) for r in await cur.fetchall()]

        # Locking: Foundations (reading) lessons are SKIPPABLE — always available
        # if not done. Textbook units are self-contained — each gets its OWN
        # sequential cursor (first incomplete lesson available, rest locked),
        # independent of the AI path, so a book's lessons aren't gated behind AI
        # lessons and vice-versa. The AI vocab lessons keep strict sequential
        # locking among THEMSELVES (first incomplete one available).
        ai_available_set = False

        def _status_ai(lesson: dict) -> str:
            nonlocal ai_available_set
            if lesson["completed_at"] is not None:
                return "done"
            if not ai_available_set:
                ai_available_set = True
                return "available"
            return "locked"

        def _status_sequential(lessons: list[dict]) -> None:
            """First not-done lesson available, the rest locked (own cursor)."""
            cursor_set = False
            for l in lessons:
                if l["completed_at"] is not None:
                    l["status"] = "done"
                elif not cursor_set:
                    cursor_set = True
                    l["status"] = "available"
                else:
                    l["status"] = "locked"

        for unit in units:
            theme = unit.get("theme")
            is_foundation = theme == "foundations"
            is_textbook = theme == "textbook"
            async with db.execute(
                """SELECT id, lesson_num, title, objective, score, completed_at, crown_level,
                          (SELECT COUNT(*) FROM course_concepts
                           WHERE introduced_lesson_id = course_lessons.id) AS concept_count
                   FROM course_lessons WHERE unit_id=? ORDER BY lesson_num""",
                (unit["id"],),
            ) as cur:
                lessons = [dict(r) for r in await cur.fetchall()]
            if is_foundation:
                for l in lessons:
                    l["status"] = "done" if l["completed_at"] is not None else "available"
            elif is_textbook:
                _status_sequential(lessons)
                # Placeholders for the lessons this unit will have but hasn't
                # authored yet — the roadmap shows the unit's full shape, and
                # each row is a "generate this one" affordance.
                async with db.execute(
                    "SELECT id, idx, spec_json FROM lesson_queue WHERE unit_id=? ORDER BY idx",
                    (unit["id"],),
                ) as cur:
                    unit["queued"] = _queued_lesson_rows(await cur.fetchall())
                unit["queued_remaining"] = len(unit["queued"])
            else:
                for l in lessons:
                    l["status"] = _status_ai(l)
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
            l["status"] = _status_ai(l)

        if pending:
            units.append({
                "id": None, "idx": len(units),
                "title": None, "summary": None, "theme": "",
                "lessons": pending, "in_progress": True,
            })

        # The in-progress chapter's header info (title + lesson budget), so the
        # roadmap can show "Chapter · Lesson 2 of ~4" instead of a bare
        # "In progress".
        if active_plan and (active_plan.get("title") or "").strip():
            course["active_chapter"] = {
                "title":  active_plan["title"],
                "budget": active_plan.get("budget"),
                "lessons_done": len(pending),
            }

        course["units"] = units
        course["lesson_count"] = sum(len(u["lessons"]) for u in units)
        course["done_count"] = sum(
            sum(1 for l in u["lessons"] if l.get("status") == "done")
            for u in units
        )
        return course


async def get_course_vocab(user_id: int, course_id: int) -> list[dict]:
    """Every vocabulary word taught across a course's COMPLETED lessons, in
    teaching order (caller groups by lesson_num/lesson_title).

    Words are pulled from each lesson's stored `concepts_json`, NOT the
    `course_concepts` registry filtered to kind='vocab'. That old approach was
    nearly always empty: the planner is biased toward GRAMMAR skills, and a
    grammar lesson is registered as ONE kind='grammar' concept whose taught words
    live in its `items` array — so the vocab-only view missed everything taught
    inside grammar lessons (most of a grammar-forward course). Here a vocab
    concept contributes itself; a grammar concept contributes its `items` (its
    skill label, e.g. "-er present tense", is not a word and is skipped).

    COMPLETED-only so it reflects what the learner has actually studied and never
    spoils pre-generated lessons ahead of them. Foundations (reading-track) units
    are excluded — they teach script, not vocabulary."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT 1 FROM courses WHERE id=? AND user_id=?", (course_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return []
        async with conn.execute(
            """SELECT l.lesson_num, l.title AS lesson_title, l.concepts_json
               FROM course_lessons l
               LEFT JOIN course_units u ON u.id = l.unit_id
               WHERE l.course_id=? AND l.completed_at IS NOT NULL
                     AND COALESCE(u.theme, '') != 'foundations'
               ORDER BY l.lesson_num, l.id""",
            (course_id,),
        ) as cur:
            lessons = [dict(r) for r in await cur.fetchall()]

    out: list[dict] = []
    seen: set[str] = set()   # first lesson to teach a word wins
    for l in lessons:
        try:
            concepts = json.loads(l["concepts_json"] or "[]")
        except (ValueError, TypeError):
            concepts = []
        if not isinstance(concepts, list):
            continue
        for c in concepts:
            if not isinstance(c, dict):
                continue
            kind = (c.get("kind") or "vocab").strip()
            words = (c.get("items") or []) if kind == "grammar" else [c]
            for w in words:
                if not isinstance(w, dict):
                    continue
                label = (w.get("label") or "").strip()
                if not label or label in seen:
                    continue
                seen.add(label)
                out.append({
                    "label": label,
                    "gloss": (w.get("gloss") or "").strip(),
                    "lesson_title": l["lesson_title"],
                    "lesson_num": l["lesson_num"],
                })
    return out


async def get_active_course(user_id: int, target_lang: str) -> dict | None:
    """The user's most recent active course for a language, or None."""
    async with connect() as db:
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
    async with connect() as db:
        async with db.execute(
            "SELECT 1 FROM courses WHERE id=? AND user_id=?", (course_id, user_id)
        ) as cur:
            if not await cur.fetchone():
                return
        async with db.execute(
            "SELECT images_json FROM textbooks WHERE course_id=? AND user_id=?",
            (course_id, user_id),
        ) as cur:
            visual_rows = await cur.fetchall()
        visual_ids = [
            str(v.get("id")) for row in visual_rows
            for v in _parse_textbook_visuals(row[0]) if v.get("id")
        ]
        await db.execute("DELETE FROM course_concepts WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM course_lessons WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM course_units WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM lesson_queue WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM textbooks WHERE course_id=?", (course_id,))
        if visual_ids:
            await db.executemany(
                "DELETE FROM media WHERE id=?", [(mid,) for mid in visual_ids])
        await db.execute("DELETE FROM courses WHERE id=? AND user_id=?", (course_id, user_id))
        await db.commit()


async def delete_ai_lessons(course_id: int) -> None:
    """Delete ALL units/lessons/concepts from a course and reset it fully.
    Foundations are re-seeded from code so they pick up any fixes."""
    async with connect() as db:
        # Look up course owner + language for mastery cleanup.
        async with db.execute(
            "SELECT user_id, target_lang FROM courses WHERE id=?", (course_id,)
        ) as cur:
            course_row = await cur.fetchone()
        if not course_row:
            return
        user_id, lang = course_row[0], course_row[1]

        await db.execute("DELETE FROM course_lessons WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM course_units WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM course_concepts WHERE course_id=?", (course_id,))
        await db.execute("DELETE FROM lesson_queue WHERE course_id=?", (course_id,))
        await db.execute("UPDATE courses SET active_plan=NULL WHERE id=?", (course_id,))
        await db.execute(
            "DELETE FROM concept_mastery WHERE user_id=? AND lang=?", (user_id, lang)
        )
        # Textbooks SURVIVE a course restart (the parse is the valuable part) —
        # but their chapters' "queued" statuses point at the queue we just wiped,
        # so reset them to let the user re-generate.
        async with db.execute(
            "SELECT id, chapters_json FROM textbooks WHERE course_id=?", (course_id,)
        ) as cur:
            books = await cur.fetchall()
        for tb_id, chapters_json in books:
            chapters = _parse_chapters(chapters_json)
            for ch in chapters:
                if isinstance(ch, dict):
                    ch["status"] = ""
            await db.execute("UPDATE textbooks SET chapters_json=? WHERE id=?",
                             (json.dumps(chapters, ensure_ascii=False), tb_id))
        await db.commit()

    # Re-seed foundations from code so fixes are picked up.
    from foundations import FOUNDATIONS, build_units
    if lang in FOUNDATIONS:
        units = build_units(lang)
        if units:
            await seed_foundation_units(course_id, units)


async def get_active_plan(course_id: int) -> dict | None:
    """The in-progress unit's outline (concepts + cursor), or None between units."""
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            "UPDATE courses SET active_plan=? WHERE id=?",
            (json.dumps(plan) if plan is not None else None, course_id),
        )
        await db.commit()


def _lesson_words(concepts_json: str) -> list[dict]:
    """The native words one lesson taught, from its stored `concepts_json`.

    A vocab concept contributes itself; a GRAMMAR concept contributes its
    `items` (its skill label, e.g. "-er present tense", is not a word). Those
    items are the blind spot this exists for: `create_lesson` registers only the
    top-level concept in `course_concepts`, so the words a grammar lesson
    actually taught were invisible to the planner and to `_filter_new_concepts`
    — which is how a later lesson could re-teach the same vocabulary under a
    fresh key. Same derivation as `get_course_vocab`, minus the completed-only
    filter (the planner must not re-teach what it has already GENERATED, whether
    or not the learner has played it yet)."""
    try:
        concepts = json.loads(concepts_json or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(concepts, list):
        return []
    out = []
    for c in concepts:
        if not isinstance(c, dict):
            continue
        kind = (c.get("kind") or "vocab").strip()
        words = (c.get("items") or []) if kind == "grammar" else [c]
        for w in words:
            if isinstance(w, dict) and (w.get("label") or "").strip():
                out.append({"label": w["label"].strip(),
                            "gloss": (w.get("gloss") or "").strip()})
    return out


async def get_next_lesson_context(course_id: int) -> dict:
    """Return everything needed to generate the next lesson:
    lesson_num, open_lessons, concept_registry, taught_words, lesson_index,
    unit_summaries, recent_summaries, prior_concepts."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            "SELECT COUNT(*) FROM course_lessons WHERE course_id=?", (course_id,)
        ) as cur:
            lesson_num = (await cur.fetchone())[0] + 1

        # Lessons in the in-progress chapter (unitless). Derived, not stored, so
        # the chapter-budget check is self-healing across old/new plan formats.
        async with db.execute(
            "SELECT COUNT(*) FROM course_lessons WHERE course_id=? AND unit_id IS NULL",
            (course_id,),
        ) as cur:
            open_lessons = (await cur.fetchone())[0]

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

        # Every lesson already generated, in teaching order, with the words it
        # taught. Recent summaries only cover the last 3, and the concept
        # registry loses grammar concepts' `items` entirely — so without this
        # the planner is largely blind to what the course has already covered.
        # Foundations units are excluded (they teach script, not vocabulary).
        async with db.execute(
            """SELECT l.lesson_num, l.title, l.summary, l.concepts_json,
                      COALESCE(u.theme, '') AS theme
               FROM course_lessons l
               LEFT JOIN course_units u ON u.id = l.unit_id
               WHERE l.course_id=? AND COALESCE(u.theme,'') != 'foundations'
               ORDER BY l.lesson_num, l.id""",
            (course_id,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        lesson_index, taught_words, seen_words = [], [], set()
        for r in rows:
            lesson_index.append({
                "lesson_num": r["lesson_num"], "title": r["title"] or "",
                "summary": r["summary"] or "",
                "source": "textbook" if r["theme"] == "textbook" else "ai",
            })
            for w in _lesson_words(r["concepts_json"]):
                if w["label"] not in seen_words:      # first lesson to teach it wins
                    seen_words.add(w["label"])
                    taught_words.append(w)

        return {
            "lesson_num":       lesson_num,
            "open_lessons":     open_lessons,
            "concept_registry": concept_registry,
            "taught_words":     taught_words,
            "lesson_index":     lesson_index,
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
    unit_id: int | None = None,
) -> int:
    """Persist a generated lesson. `unit_id` NULL = the in-progress AI chapter
    (assigned to a unit later by close_unit); non-NULL = a textbook unit the
    lesson is attached to directly (the textbook path bypasses active_plan).
    Inserts concepts into the registry. Returns lesson_id."""
    async with connect() as db:
        cur = await db.execute(
            """INSERT INTO course_lessons
               (course_id, lesson_num, title, objective, content, concepts_json, summary, llm_debug_json, unit_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                course_id, lesson_num,
                (title or "").strip(), (objective or "").strip(),
                json.dumps(content) if content is not None else None,
                json.dumps(concepts),
                (summary or "").strip(),
                json.dumps(llm_debug) if llm_debug else None,
                unit_id,
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
    async with connect() as db:
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


async def create_textbook_unit_row(course_id: int, title: str, summary: str = "",
                                   textbook_id: int | None = None,
                                   chapter_idx: int | None = None) -> int:
    """Create an EMPTY textbook unit (theme='textbook') up front. Unlike
    close_unit it does NOT back-assign unitless lessons — textbook lessons attach
    to it directly via create_lesson(unit_id=...), so they never touch the AI
    course's active_plan/close_unit flow. `textbook_id`/`chapter_idx` record which
    chapter of which book the unit came from (migration 044), which survives the
    queue draining and is what lets the unit be found, ordered and regenerated
    per chapter. Returns unit_id."""
    async with connect() as db:
        async with db.execute(
            "SELECT COALESCE(MAX(idx), -1) FROM course_units WHERE course_id=?", (course_id,)
        ) as cur:
            next_idx = (await cur.fetchone())[0] + 1
        cur = await db.execute(
            "INSERT INTO course_units (course_id, idx, title, summary, theme, "
            "                          textbook_id, chapter_idx) "
            "VALUES (?, ?, ?, ?, 'textbook', ?, ?)",
            (course_id, next_idx, (title or "").strip(), (summary or "").strip(),
             textbook_id, chapter_idx),
        )
        await db.commit()
        return cur.lastrowid


async def get_textbook_unit(user_id: int, unit_id: int) -> dict | None:
    """A textbook unit row + its course, ownership-checked. None if not found,
    not owned, or not a textbook unit."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT u.id, u.course_id, u.title, u.theme, u.textbook_id, u.chapter_idx,
                      c.target_lang, c.level
               FROM course_units u JOIN courses c ON c.id = u.course_id
               WHERE u.id=? AND c.user_id=? AND u.theme='textbook'""",
            (unit_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_textbook_units(user_id: int, textbook_id: int) -> list[dict]:
    """Every unit built from one book, in chapter order, with lesson + queue
    counts. Backs "this chapter already has lessons — regenerate?" instead of
    silently building a second unit for the same pages."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT u.id, u.title, u.chapter_idx,
                      (SELECT COUNT(*) FROM course_lessons l WHERE l.unit_id = u.id)
                        AS lesson_count,
                      (SELECT COUNT(*) FROM course_lessons l
                        WHERE l.unit_id = u.id AND l.completed_at IS NOT NULL)
                        AS done_count,
                      (SELECT COUNT(*) FROM lesson_queue q WHERE q.unit_id = u.id)
                        AS queued_remaining
               FROM course_units u
               JOIN courses c ON c.id = u.course_id
               JOIN textbooks t ON t.id = u.textbook_id
               WHERE u.textbook_id=? AND t.user_id=? AND c.user_id=? AND u.theme='textbook'
               ORDER BY COALESCE(u.chapter_idx, 9999), u.idx""",
            (textbook_id, user_id, user_id),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def collapse_unit_chapter_idx(textbook_id: int, index: int) -> None:
    """Re-point a book's units after chapters `index` and `index+1` merged.

    Chapter indices are positions in the book's chapter list, so removing a
    boundary shifts everything after it down one. Units built from either half
    now belong to the merged chapter at `index`."""
    async with connect() as db:
        await db.execute(
            "UPDATE course_units SET chapter_idx=? "
            "WHERE textbook_id=? AND chapter_idx=?",
            (index, textbook_id, index + 1))
        await db.execute(
            "UPDATE course_units SET chapter_idx = chapter_idx - 1 "
            "WHERE textbook_id=? AND chapter_idx > ?",
            (textbook_id, index + 1))
        await db.execute(
            "UPDATE lesson_queue SET chapter_idx=? WHERE textbook_id=? AND chapter_idx=?",
            (index, textbook_id, index + 1))
        await db.execute(
            "UPDATE lesson_queue SET chapter_idx = chapter_idx - 1 "
            "WHERE textbook_id=? AND chapter_idx > ?",
            (textbook_id, index + 1))
        await db.commit()


async def delete_course_unit(user_id: int, unit_id: int) -> dict | None:
    """Delete a unit, its lessons, their concept-registry rows and any lessons
    still queued for it (ownership-checked). Returns {lessons, queued} counts, or
    None when the unit isn't the user's. Mastery history is deliberately left
    alone — it describes the learner, not the lesson."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT u.id, u.course_id FROM course_units u
               JOIN courses c ON c.id = u.course_id
               WHERE u.id=? AND c.user_id=?""",
            (unit_id, user_id),
        ) as cur:
            unit = await cur.fetchone()
        if not unit:
            return None
        async with db.execute(
            "SELECT id FROM course_lessons WHERE unit_id=?", (unit_id,)
        ) as cur:
            lesson_ids = [r[0] for r in await cur.fetchall()]
        for lesson_id in lesson_ids:
            await db.execute(
                "DELETE FROM course_concepts WHERE introduced_lesson_id=?", (lesson_id,))
        await db.execute("DELETE FROM course_lessons WHERE unit_id=?", (unit_id,))
        cur = await db.execute("DELETE FROM lesson_queue WHERE unit_id=?", (unit_id,))
        queued = cur.rowcount
        await db.execute("DELETE FROM course_units WHERE id=?", (unit_id,))
        await db.commit()
    return {"lessons": len(lesson_ids), "queued": queued,
            "course_id": unit["course_id"]}


async def delete_lesson(user_id: int, lesson_id: int) -> dict | None:
    """Delete ONE lesson and the concepts it introduced (ownership-checked).
    Returns {course_id, unit_id} or None. Remaining lessons keep their stored
    lesson_num — position within a unit is rendered from order, not the number."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT l.id, l.course_id, l.unit_id FROM course_lessons l
               JOIN courses c ON c.id = l.course_id
               WHERE l.id=? AND c.user_id=?""",
            (lesson_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await db.execute(
            "DELETE FROM course_concepts WHERE introduced_lesson_id=?", (lesson_id,))
        await db.execute("DELETE FROM course_lessons WHERE id=?", (lesson_id,))
        await db.commit()
    return {"course_id": row["course_id"], "unit_id": row["unit_id"]}


async def update_lesson_content(user_id: int, lesson_id: int, content: dict) -> bool:
    """Overwrite a lesson's stored content (ownership-checked). Returns False if
    the lesson isn't the user's. Used when the learner reports a bad drill or
    teach block and it's re-authored in place — the rest of the lesson, its
    concepts, crowns and mastery history all stay exactly as they were."""
    async with connect() as db:
        cur = await db.execute(
            """UPDATE course_lessons SET content=?
               WHERE id=? AND course_id IN (SELECT id FROM courses WHERE user_id=?)""",
            (json.dumps(content, ensure_ascii=False), lesson_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_course_unit_if_empty(unit_id: int) -> None:
    """Drop a unit row iff it has no lessons (used to roll back a failed
    textbook-unit creation) plus any queue rows still scoped to it."""
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM course_lessons WHERE unit_id=?", (unit_id,)
        ) as cur:
            if (await cur.fetchone())[0]:
                return
        await db.execute("DELETE FROM lesson_queue WHERE unit_id=?", (unit_id,))
        await db.execute("DELETE FROM course_units WHERE id=?", (unit_id,))
        await db.commit()


async def get_open_lesson_stats(course_id: int) -> tuple[int, int]:
    """(total, completed) among the in-progress (unitless) lessons — used to close
    the chapter into a unit at COMPLETION time once its budget is fully done."""
    async with connect() as db:
        async with db.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END), 0)
               FROM course_lessons WHERE course_id=? AND unit_id IS NULL""",
            (course_id,),
        ) as cur:
            row = await cur.fetchone()
    return (row[0], row[1])


# ── Lesson queue (textbook import) ────────────────────────────────────────────
# Queued lesson specs from a textbook PDF import, consumed FIFO by lesson
# generation: while the queue is non-empty the planner is skipped (the book IS
# the plan) and the author is grounded in the item's `source` excerpt.

async def add_lesson_queue(course_id: int, items: list[dict],
                           textbook_id: int | None = None,
                           chapter_idx: int | None = None,
                           front: bool = False,
                           unit_id: int | None = None) -> int:
    """Add queue items ({unit_title, unit_size, spec, source}). Returns count.
    `textbook_id`/`chapter_idx` tag the items with the book chapter they came
    from (so deleting a book drops its still-queued lessons). `unit_id` scopes
    the items to a textbook course_units row so they are consumed only by that
    unit's dedicated authoring route (never the AI path). ``front`` is a legacy
    knob (kept for the old signature); textbook items rely on `unit_id`, not
    ordering, so they pass front=False."""
    if not items:
        return 0
    async with connect() as db:
        order_fn = "MIN" if front else "MAX"
        default_idx = 0 if front else -1
        async with db.execute(
            f"SELECT COALESCE({order_fn}(idx), ?) FROM lesson_queue WHERE course_id=?",
            (default_idx, course_id),
        ) as cur:
            edge = (await cur.fetchone())[0]
        next_idx = edge - len(items) if front else edge + 1
        for i, it in enumerate(items):
            await db.execute(
                """INSERT INTO lesson_queue (course_id, idx, unit_title, unit_size,
                                             spec_json, source, textbook_id, chapter_idx, unit_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (course_id, next_idx + i, (it.get("unit_title") or "").strip(),
                 max(1, int(it.get("unit_size") or 1)),
                 json.dumps(it.get("spec") or {}), it.get("source") or "",
                 textbook_id, chapter_idx, unit_id),
            )
        await db.commit()
        return len(items)


def _row_to_queue_item(row) -> dict:
    item = dict(row)
    try:
        item["spec"] = json.loads(item.pop("spec_json") or "{}")
    except (ValueError, TypeError):
        item["spec"] = {}
    return item


async def peek_lesson_queue(course_id: int) -> dict | None:
    """The next queued lesson spec (lowest idx), or None. Legacy course-wide
    peek — retained for backward compatibility; the textbook path now uses
    peek_lesson_queue_for_unit."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, idx, unit_title, unit_size, spec_json, source
               FROM lesson_queue WHERE course_id=? ORDER BY idx LIMIT 1""",
            (course_id,),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_queue_item(row) if row else None


async def peek_lesson_queue_for_unit(unit_id: int) -> dict | None:
    """The next queued lesson spec for a specific textbook unit, or None."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, idx, unit_title, unit_size, spec_json, source
               FROM lesson_queue WHERE unit_id=? ORDER BY idx LIMIT 1""",
            (unit_id,),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_queue_item(row) if row else None


def _queued_lesson_rows(rows) -> list[dict]:
    """`(id, idx, spec_json)` rows → the `{id, idx, title}` placeholders the Learn
    page reserves a row for, so a textbook unit shows its whole shape ("5
    lessons, 2 built") instead of a bare "3 left" counter."""
    out = []
    for row in rows:
        try:
            spec = json.loads(row["spec_json"] or "{}")
        except (ValueError, TypeError):
            spec = {}
        skill = spec.get("skill") if isinstance(spec.get("skill"), dict) else {}
        title = (spec.get("title") or skill.get("label") or "").strip()
        out.append({"id": row["id"], "idx": row["idx"],
                    "title": title or "Next lesson"})
    return out


async def list_lesson_queue_for_unit(unit_id: int) -> list[dict]:
    """The unit's not-yet-authored lessons, in order (see _queued_lesson_rows)."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, idx, spec_json FROM lesson_queue WHERE unit_id=? ORDER BY idx",
            (unit_id,),
        ) as cur:
            return _queued_lesson_rows(await cur.fetchall())


async def pop_lesson_queue(queue_id: int) -> None:
    async with connect() as db:
        await db.execute("DELETE FROM lesson_queue WHERE id=?", (queue_id,))
        await db.commit()


async def count_lesson_queue(course_id: int) -> int:
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM lesson_queue WHERE course_id=?", (course_id,)
        ) as cur:
            return (await cur.fetchone())[0]


async def count_lesson_queue_for_unit(unit_id: int) -> int:
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM lesson_queue WHERE unit_id=?", (unit_id,)
        ) as cur:
            return (await cur.fetchone())[0]


async def clear_lesson_queue(course_id: int) -> None:
    async with connect() as db:
        await db.execute("DELETE FROM lesson_queue WHERE course_id=?", (course_id,))
        await db.commit()


async def clear_lesson_queue_for_unit(unit_id: int) -> int:
    """Drop remaining queued lessons for one textbook unit. Returns rows removed."""
    async with connect() as db:
        cur = await db.execute("DELETE FROM lesson_queue WHERE unit_id=?", (unit_id,))
        await db.commit()
        return cur.rowcount


# ── Textbook library (import v2) ──────────────────────────────────────────────
# Uploaded books persist with their per-page extracted text + an editable
# chapter structure, so parsing survives the upload: the user can correct page
# ranges and reuse them as source-selection presets for individual lessons.

def _parse_chapters(raw: str | None) -> list[dict]:
    try:
        chapters = json.loads(raw or "[]")
    except (ValueError, TypeError):
        chapters = []
    return chapters if isinstance(chapters, list) else []


def _parse_textbook_visuals(raw: str | None) -> list[dict]:
    try:
        visuals = json.loads(raw or "[]")
    except (ValueError, TypeError):
        visuals = []
    return [v for v in visuals if isinstance(v, dict)] if isinstance(visuals, list) else []


def _parse_bookmarks(raw: str | None) -> list[int]:
    """Bookmarked 1-based page numbers, deduped + sorted."""
    try:
        marks = json.loads(raw or "[]")
    except (ValueError, TypeError):
        marks = []
    return sorted({int(p) for p in marks if isinstance(p, (int, float)) and int(p) >= 1}) \
        if isinstance(marks, list) else []


async def create_textbook(user_id: int, course_id: int, title: str,
                          filename: str, pages: list[str],
                          visuals: list[dict] | None = None,
                          pdf_media_id: str | None = None) -> int:
    async with connect() as db:
        cur = await db.execute(
            """INSERT INTO textbooks (user_id, course_id, title, filename,
                                      num_pages, pages_json, images_json,
                                      pdf_media_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, course_id, (title or "").strip()[:200],
             (filename or "").strip()[:200], len(pages), json.dumps(pages),
             json.dumps(visuals or [], ensure_ascii=False), pdf_media_id),
        )
        await db.commit()
        return cur.lastrowid


async def update_textbook_pages(user_id: int, textbook_id: int,
                                pages: list[str]) -> bool:
    """Overwrite a book's per-page text (ownership-checked).

    Used by the vision re-extraction path to replace garbled pages with clean,
    native-script transcripts. ``num_pages`` is kept in sync so page-range
    validation elsewhere stays correct.
    """
    async with connect() as db:
        cur = await db.execute(
            "UPDATE textbooks SET pages_json=?, num_pages=? WHERE id=? AND user_id=?",
            (json.dumps(pages, ensure_ascii=False), len(pages),
             textbook_id, user_id),
        )
        await db.commit()
        return bool(cur.rowcount)


async def list_textbooks(user_id: int, course_id: int) -> list[dict]:
    """Books for a course (no page text — the list stays light). Each chapter
    row also reports how many of its lessons are still queued."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, title, filename, num_pages, chapters_json, images_json,
                      pdf_media_id, last_page, bookmarks_json, created_at
               FROM textbooks WHERE user_id=? AND course_id=? ORDER BY id""",
            (user_id, course_id),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        out = []
        for r in rows:
            chapters = _parse_chapters(r.pop("chapters_json"))
            visuals = _parse_textbook_visuals(r.pop("images_json"))
            async with db.execute(
                """SELECT chapter_idx, COUNT(*) FROM lesson_queue
                   WHERE textbook_id=? GROUP BY chapter_idx""", (r["id"],),
            ) as cur:
                queued = {row[0]: row[1] for row in await cur.fetchall()}
            for i, ch in enumerate(chapters):
                ch["queued"] = queued.get(i, 0)
            r["chapters"] = chapters
            r["visual_count"] = len(visuals)
            r["has_pdf"] = bool(r.pop("pdf_media_id", None))
            r["bookmarks"] = _parse_bookmarks(r.pop("bookmarks_json", None))
            out.append(r)
        return out


async def get_textbook(user_id: int, textbook_id: int) -> dict | None:
    """One book incl. its pages (ownership-checked)."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, user_id, course_id, title, filename, num_pages,
                      pages_json, chapters_json, images_json, pdf_media_id,
                      last_page, bookmarks_json, created_at
               FROM textbooks WHERE id=? AND user_id=?""",
            (textbook_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    book = dict(row)
    try:
        book["pages"] = json.loads(book.pop("pages_json") or "[]")
    except (ValueError, TypeError):
        book["pages"] = []
    book["chapters"] = _parse_chapters(book.pop("chapters_json"))
    book["visuals"] = _parse_textbook_visuals(book.pop("images_json"))
    book["bookmarks"] = _parse_bookmarks(book.pop("bookmarks_json", None))
    book["has_pdf"] = bool(book.get("pdf_media_id"))
    return book


async def set_textbook_reading(user_id: int, textbook_id: int,
                               last_page: int | None = None,
                               bookmarks: list[int] | None = None) -> bool:
    """Persist reading progress / bookmarks for the textbook reader (partial
    update, ownership-checked). Returns False if the book isn't owned."""
    sets, params = [], []
    if last_page is not None:
        sets.append("last_page=?")
        params.append(max(0, int(last_page)))
    if bookmarks is not None:
        clean = sorted({int(p) for p in bookmarks
                        if isinstance(p, (int, float)) and int(p) >= 1})
        sets.append("bookmarks_json=?")
        params.append(json.dumps(clean))
    if not sets:
        return False
    params += [textbook_id, user_id]
    async with connect() as db:
        cur = await db.execute(
            f"UPDATE textbooks SET {', '.join(sets)} WHERE id=? AND user_id=?",
            params,
        )
        await db.commit()
        return bool(cur.rowcount)


async def rename_textbook(user_id: int, textbook_id: int, title: str) -> bool:
    async with connect() as db:
        cur = await db.execute(
            "UPDATE textbooks SET title=? WHERE id=? AND user_id=?",
            ((title or "").strip()[:200], textbook_id, user_id),
        )
        await db.commit()
        return bool(cur.rowcount)


async def list_textbook_visual_ids(user_id: int, course_id: int) -> list[str]:
    """Media ids to unlink when a whole course is deleted (page visuals AND the
    stored source PDF for each book)."""
    async with connect() as db:
        async with db.execute(
            "SELECT images_json, pdf_media_id FROM textbooks WHERE user_id=? AND course_id=?",
            (user_id, course_id),
        ) as cur:
            rows = await cur.fetchall()
    ids = [
        str(v.get("id")) for row in rows for v in _parse_textbook_visuals(row[0])
        if v.get("id")
    ]
    ids += [str(row[1]) for row in rows if row[1]]
    return ids


async def delete_media_records(media_ids: list[str]) -> None:
    if not media_ids:
        return
    async with connect() as db:
        await db.executemany("DELETE FROM media WHERE id=?", [(mid,) for mid in media_ids])
        await db.commit()


async def update_textbook_chapters(textbook_id: int, chapters: list[dict]) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE textbooks SET chapters_json=? WHERE id=?",
            (json.dumps(chapters, ensure_ascii=False), textbook_id),
        )
        await db.commit()


async def delete_textbook(user_id: int, textbook_id: int) -> bool:
    """Delete a book + its still-queued lessons. Authored lessons are kept."""
    async with connect() as db:
        async with db.execute(
            "SELECT images_json, pdf_media_id FROM textbooks WHERE id=? AND user_id=?",
            (textbook_id, user_id),
        ) as media_cur:
            media_row = await media_cur.fetchone()
        visual_ids = [
            str(v.get("id")) for v in _parse_textbook_visuals(media_row[0])
            if v.get("id")
        ] if media_row else []
        pdf_id = media_row[1] if media_row else None
        media_ids = visual_ids + ([pdf_id] if pdf_id else [])
        cur = await db.execute(
            "DELETE FROM textbooks WHERE id=? AND user_id=?",
            (textbook_id, user_id),
        )
        if cur.rowcount:
            await db.execute(
                "DELETE FROM lesson_queue WHERE textbook_id=?", (textbook_id,))
            # Units built from the book survive (their lessons are kept), but the
            # chapter link must go — a dangling textbook_id would sort them by a
            # book that no longer exists and could collide with a re-upload's id.
            await db.execute(
                "UPDATE course_units SET textbook_id=NULL, chapter_idx=NULL "
                "WHERE textbook_id=?", (textbook_id,))
            if media_ids:
                await db.executemany(
                    "DELETE FROM media WHERE id=?", [(mid,) for mid in media_ids])
        await db.commit()
        return bool(cur.rowcount)


async def get_lesson(user_id: int, lesson_id: int) -> dict | None:
    """Return a lesson (ownership-checked). Content is stored at creation time."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT l.id, l.lesson_num, l.title, l.objective,
                      l.content, l.concepts_json, l.llm_debug_json,
                      l.score, l.completed_at, l.unit_id,
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


async def get_unit_next_lesson(user_id: int, lesson_id: int) -> dict | None:
    """What comes after `lesson_id` inside its own unit: the next lesson (if one
    is authored) and how many of the unit's lessons are still queued.

    Lets the results screen hand the learner straight on to the next lesson of a
    textbook unit — or offer to build it — instead of dropping them back on the
    map to hunt for it. None when the lesson isn't in a unit."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT l.unit_id, l.lesson_num, COALESCE(u.theme, '') AS theme,
                      COALESCE(u.title, '') AS unit_title
               FROM course_lessons l
               JOIN courses c ON c.id = l.course_id
               LEFT JOIN course_units u ON u.id = l.unit_id
               WHERE l.id=? AND c.user_id=?""",
            (lesson_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row or not row["unit_id"]:
            return None
        async with db.execute(
            """SELECT id, title FROM course_lessons
               WHERE unit_id=? AND (lesson_num > ? OR (lesson_num = ? AND id > ?))
               ORDER BY lesson_num, id LIMIT 1""",
            (row["unit_id"], row["lesson_num"], row["lesson_num"], lesson_id),
        ) as cur:
            nxt = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM lesson_queue WHERE unit_id=?", (row["unit_id"],)
        ) as cur:
            queued = (await cur.fetchone())[0]
    return {
        "unit_id": row["unit_id"], "theme": row["theme"],
        "unit_title": row["unit_title"],
        "next": {"id": nxt["id"], "title": nxt["title"]} if nxt else None,
        "queued_remaining": queued,
    }


CROWN_MAX = 3   # skill-tree crown cap; each completion bumps the crown by 1


async def complete_lesson(user_id: int, lesson_id: int, score: int) -> tuple[bool, bool, int, bool]:
    """Record (or improve) lesson completion + score. Ownership-checked.
    Returns (found, first_completion, crown_level, leveled_up). first_completion is
    True only the FIRST time the lesson is finished, so XP is awarded once (replays
    don't re-award). Every completion bumps the crown level by 1 up to CROWN_MAX;
    leveled_up is True when this completion actually raised the crown (i.e. it
    wasn't already maxed)."""
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        async with db.execute(
            "SELECT content FROM concept_content WHERE lang=? AND concept_key=?",
            (lang, concept_key),
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row[0]) if row else None


async def set_concept_content(lang: str, concept_key: str, content: dict) -> None:
    """Cache a verified grammar artifact (shared across users; upsert)."""
    async with connect() as db:
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
    async with connect() as db:
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


async def resync_reader_sentences(text_id: int, new_sents: list[str]) -> bool:
    """Rewrite a text's cached sentence rows to match current tokenisation.

    A tokenizer fix can change how a stored text splits into sentences (e.g.
    the pykakasi Japanese newline bug inserted phantom '。' sentences and
    mangled headings), leaving cached translations/audio at stale indices.
    Re-key the cached content by matching `sentence_text` against the new
    boundaries, drop rows that no longer correspond to any sentence, and
    renumber to the new indices. Unmatched new sentences get no row (they are
    fetched on demand). Idempotent: a no-op when already aligned.

    Returns True if it rewrote anything.
    """
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT sentence_idx, sentence_text, translation, romanization, audio_data
               FROM reader_sentences WHERE text_id=? ORDER BY sentence_idx""",
            (text_id,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return False
        if [r["sentence_text"] for r in rows] == list(new_sents):
            return False  # already aligned

        old_by_text: dict[str, dict] = {}
        for r in rows:
            st = r["sentence_text"]
            if st and st not in old_by_text:
                old_by_text[st] = r
        new_rows = []
        for i, st in enumerate(new_sents):
            old = old_by_text.get(st)
            # Only carry rows that hold something worth keeping; empty
            # placeholders are re-derived on demand.
            if old and (old["translation"] or old["audio_data"] or old["romanization"]):
                new_rows.append(
                    (text_id, i, st, old["translation"], old["audio_data"], old["romanization"])
                )
        await db.execute("DELETE FROM reader_sentences WHERE text_id=?", (text_id,))
        if new_rows:
            await db.executemany(
                """INSERT INTO reader_sentences
                   (text_id, sentence_idx, sentence_text, translation, audio_data, romanization)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                new_rows,
            )
        await db.commit()
        return True


async def upsert_reader_sentence(
    text_id: int, idx: int, sentence_text: str,
    translation: str | None = None, audio_data: bytes | None = None,
    romanization: str | None = None,
):
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as conn:
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
    async with connect() as conn:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        await db.executemany(
            "UPDATE cards SET cefr_level = ? "
            "WHERE user_id = ? AND target_lang = ? AND target_text = ? "
            "AND (cefr_level IS NULL OR cefr_level NOT IN ('A1','A2','B1','B2','C1','C2'))",
            rows,
        )
        await db.commit()


def _utc_today_date():
    """Today in UTC as a `date` object. Only the fallback for users with no
    timezone setting — every per-user day boundary goes through `_local_today`
    / `local_today_str` below."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date()


# ── Per-user day boundaries ────────────────────────────────────────────────────
# Streaks, the daily-XP ring, daily quests and the new-cards-per-day cap all
# answer one question: "what day is it for THIS learner?". That used to be
# 00:00 UTC for everybody — which is 5pm in California and noon in Auckland. Two
# consecutive local study days could therefore land in the SAME UTC day, opening
# a phantom gap that broke a streak the learner had genuinely kept, and the XP
# ring reset mid-afternoon.
#
# Every day boundary now resolves through the user's `timezone` setting (an IANA
# name the client captures from `Intl.DateTimeFormat().resolvedOptions()`).
# Users with no setting keep the old UTC behaviour, so this is a pure widening:
# stored `study_date` rows are already local-or-UTC ISO dates and stay valid.

DEFAULT_TIMEZONE = "UTC"


def resolve_timezone(name: str | None):
    """IANA name → tzinfo, falling back to UTC. Never raises: a day boundary is
    not worth a 500, and UTC is exactly the old behaviour."""
    from datetime import timezone as _timezone
    if not name or name == "UTC":
        return _timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return _timezone.utc


def is_valid_timezone(name: str | None) -> bool:
    """True iff `name` is a resolvable IANA zone (or the literal 'UTC')."""
    if not name or not isinstance(name, str) or len(name) > 64:
        return False
    if name == "UTC":
        return True
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:
        return False


def local_today_str(tz_name: str | None, now=None) -> str:
    """The current date in `tz_name` as an ISO string (what we store in
    study_activity / daily_quests / first_seen_date)."""
    from datetime import datetime, timezone as _timezone
    now = now or datetime.now(_timezone.utc)
    return now.astimezone(resolve_timezone(tz_name)).date().isoformat()


def local_day_bounds(tz_name: str | None, day: str | None = None) -> tuple[str, str]:
    """Half-open UTC bounds [start, end) of a local calendar day, formatted to
    match SQLite's `datetime('now')` so they can be compared against stored
    `created_at` text directly. Used wherever "today's XP" is summed."""
    from datetime import datetime, date, time, timedelta, timezone as _timezone
    zone = resolve_timezone(tz_name)
    d = date.fromisoformat(day) if day else datetime.now(_timezone.utc).astimezone(zone).date()
    start = datetime.combine(d, time.min, tzinfo=zone).astimezone(_timezone.utc)
    end = datetime.combine(d + timedelta(days=1), time.min, tzinfo=zone).astimezone(_timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


async def _tz_for(conn, user_id: int) -> str:
    """The user's timezone, read on an already-open connection (a PK lookup, so
    cheap enough to do inline on every write rather than cache-and-invalidate)."""
    async with conn.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key='timezone'", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    return (row[0] if row and row[0] else DEFAULT_TIMEZONE)


async def _local_today(conn, user_id: int) -> str:
    """Today's ISO date in the user's timezone, on an open connection."""
    return local_today_str(await _tz_for(conn, user_id))


async def get_user_timezone(user_id: int) -> str:
    async with connect() as db:
        return await _tz_for(db, user_id)


async def user_today(user_id: int) -> str:
    """Today's ISO date in the user's timezone (opens its own connection)."""
    async with connect() as db:
        return await _local_today(db, user_id)


def _streak_from_dates(dates_desc: list[str], today=None) -> int:
    """Count the current streak (consecutive days ending today or yesterday)
    from a DESC-sorted list of ISO date strings. Pure — shared by get_streak and
    the admin dashboard so they can never compute the streak differently."""
    if not dates_desc:
        return 0
    from datetime import date, timedelta
    if today is None:
        today = _utc_today_date()
    most_recent = date.fromisoformat(dates_desc[0])
    # Allow streak if most-recent activity is today or yesterday.
    if most_recent < today - timedelta(days=1):
        return 0
    streak = 0
    expected = today if most_recent == today else today - timedelta(days=1)
    for row in dates_desc:
        d = date.fromisoformat(row)
        if d == expected:
            streak += 1
            expected -= timedelta(days=1)
        elif d < expected:
            break
    return streak


async def get_streak(user_id: int) -> int:
    """Return the user's current study streak in days.

    A streak is the number of consecutive days (ending today or yesterday, in
    the USER's timezone) with at least one study action. Counting back from
    today preserves the streak before the user studies on a given day.

    PURE — this never writes. Streak freezes are applied by
    `apply_streak_freezes` (from every study write, and when a user reads their
    OWN streak), so rendering somebody else's streak on a leaderboard can't
    spend their shields or fabricate activity rows on their account.
    """
    from datetime import date
    async with connect() as db:
        today = await _local_today(db, user_id)
        async with db.execute(
            "SELECT study_date FROM study_activity WHERE user_id=? ORDER BY study_date DESC",
            (user_id,),
        ) as cur:
            rows = [r[0] async for r in cur]
    return _streak_from_dates(rows, date.fromisoformat(today))


async def _bridge_streak_gap(conn, user_id: int, today: str) -> int:
    """Spend streak freezes to fill the missed days between the learner's last
    active day and `today`. Returns the number of days actually bridged.

    Deliberately looks only at activity STRICTLY BEFORE today, so the outcome
    never depends on whether the streak happened to be read before the day's
    first study was written. (It used to: the old check required the most recent
    activity to be exactly `today - 2`, so if a review landed before any
    `/api/streak` call, the row for today made the gap invisible and the streak
    reset with the freeze still unspent — a race the learner couldn't see.)

    All-or-nothing: a gap wider than the freezes on hand is left alone rather
    than part-filled, since a partly-bridged gap still breaks the streak and the
    shields would be spent for nothing.
    """
    from datetime import date, timedelta
    today_d = date.fromisoformat(today)
    async with conn.execute(
        "SELECT MAX(study_date) FROM study_activity WHERE user_id=? AND study_date < ?",
        (user_id, today),
    ) as cur:
        row = await cur.fetchone()
    if not row or not row[0]:
        return 0
    try:
        prev_d = date.fromisoformat(row[0])
    except ValueError:
        return 0
    gap = (today_d - prev_d).days - 1        # missed days strictly between
    if gap <= 0:
        return 0
    freezes = await _settings_int(conn, user_id, "streak_freezes")
    if freezes < gap:
        return 0
    # Only protect a streak worth protecting — a single isolated day isn't one.
    async with conn.execute(
        "SELECT study_date FROM study_activity WHERE user_id=? AND study_date<=? "
        "ORDER BY study_date DESC",
        (user_id, row[0]),
    ) as cur:
        prior = [r[0] async for r in cur]
    if _streak_from_dates(prior, prev_d) < STREAK_FREEZE_MIN_STREAK:
        return 0
    # Per-day INSERT OR IGNORE so a concurrent call can't double-spend: we only
    # pay for the days this call actually filled.
    filled = 0
    for n in range(1, gap + 1):
        ins = await conn.execute(
            "INSERT OR IGNORE INTO study_activity (user_id, study_date) VALUES (?, ?)",
            (user_id, (prev_d + timedelta(days=n)).isoformat()),
        )
        filled += 1 if ins.rowcount == 1 else 0
    if not filled:
        return 0
    await conn.execute(
        "INSERT OR REPLACE INTO user_settings (user_id, key, value) "
        "VALUES (?, 'streak_freezes', ?)",
        (user_id, str(max(0, freezes - filled))),
    )
    await conn.execute(
        "INSERT OR REPLACE INTO user_settings (user_id, key, value) "
        "VALUES (?, 'streak_freeze_used_date', ?)",
        (user_id, (today_d - timedelta(days=1)).isoformat()),
    )
    return filled


async def apply_streak_freezes(user_id: int) -> int:
    """Public entry point for freeze bridging — call before reading a user's own
    streak. Study writes bridge themselves via `_mark_study_day`."""
    async with connect() as db:
        bridged = await _bridge_streak_gap(db, user_id, await _local_today(db, user_id))
        if bridged:
            await db.commit()
        return bridged


async def _mark_study_day(conn, user_id: int, day: str | None = None) -> str:
    """Record an active study day (user-local) and bridge any freezable gap, on
    an already-open connection. Every write path funnels through here so the
    streak can never depend on which request arrived first.

    `day` backdates the row — used when an offline review is synced days after
    it was actually answered. Recording the real day first means a backdated
    review can close its own gap for free instead of costing a freeze.
    """
    today = await _local_today(conn, user_id)
    day = day or today
    await conn.execute(
        "INSERT OR IGNORE INTO study_activity (user_id, study_date) VALUES (?, ?)",
        (user_id, day),
    )
    await _bridge_streak_gap(conn, user_id, today)
    return day


async def set_streak(user_id: int, days: int) -> int:
    """Admin override: force a user's current streak to exactly `days`.

    Streaks aren't stored as a number — they're derived from `study_activity`
    rows — so we reshape those rows (all dates in the user's timezone, matching
    get_streak):
      - backfill the most recent `days` days (offsets 0..days-1) ending today, and
      - clear the boundary day at offset `days` so the count stops at exactly
        `days` (and, for days==0, also clear yesterday so the streak lapses).
    Days further back are left untouched. Idempotent. Returns the new streak."""
    from datetime import date, timedelta
    days = max(0, int(days))
    async with connect() as db:
        today = date.fromisoformat(await _local_today(db, user_id))
        # Ensure the streak window is present.
        if days:
            await db.executemany(
                "INSERT OR IGNORE INTO study_activity (user_id, study_date) VALUES (?, ?)",
                [(user_id, (today - timedelta(days=n)).isoformat()) for n in range(days)],
            )
        # Clear the day(s) just past the window so the streak can't run longer.
        # days==0 also clears yesterday so most-recent < yesterday (streak = 0).
        boundary = [(today - timedelta(days=days)).isoformat()]
        if days == 0:
            boundary.append((today - timedelta(days=1)).isoformat())
        await db.executemany(
            "DELETE FROM study_activity WHERE user_id=? AND study_date=?",
            [(user_id, d) for d in boundary],
        )
        await db.commit()
    return await get_streak(user_id)


async def ensure_min_streak(user_id: int, days: int) -> int:
    """Insert-only backfill guaranteeing the streak reads at least `days`.

    Used when a user's timezone changes. Their stored `study_date` rows were
    written against the OLD boundary, so re-reading them against the new one can
    shift the count by a day (an 8pm study session in Los Angeles was filed as
    the next UTC day). Nobody should lose a streak they had already earned to a
    correctness fix, so we top the history back up. Never deletes — unlike
    `set_streak`, this can only preserve, and it is idempotent."""
    from datetime import date, timedelta
    days = max(0, int(days))
    if not days:
        return await get_streak(user_id)
    async with connect() as db:
        today = date.fromisoformat(await _local_today(db, user_id))
        async with db.execute(
            "SELECT 1 FROM study_activity WHERE user_id=? AND study_date=?",
            (user_id, today.isoformat()),
        ) as cur:
            active_today = await cur.fetchone() is not None
        # Anchor where the streak already ends, so we extend it rather than
        # inventing activity for a day the learner hasn't studied yet.
        anchor = today if active_today else today - timedelta(days=1)
        await db.executemany(
            "INSERT OR IGNORE INTO study_activity (user_id, study_date) VALUES (?, ?)",
            [(user_id, (anchor - timedelta(days=n)).isoformat()) for n in range(days)],
        )
        await db.commit()
    return await get_streak(user_id)


async def record_study_activity(user_id: int, day: str | None = None) -> None:
    """Mark a day as an active learning day for the streak. Called for ANY
    meaningful activity — SRS reviews, completing a lesson, or a tutor turn —
    so the 🔥 streak reflects all study, not only flashcard reviews. `day`
    backdates the row for reviews synced after the fact."""
    async with connect() as db:
        await _mark_study_day(db, user_id, day)
        await db.commit()


# ── Streak freeze (lesson redesign B5) ─────────────────────────────────────────
# Earned, not bought: one freeze per N completed lessons, capped. Consumed
# automatically in get_streak to bridge a single missed day. Stored in
# user_settings (streak_freezes, streak_freeze_progress, streak_freeze_used_date).

STREAK_FREEZE_CAP = 2
STREAK_FREEZE_PER_LESSONS = 10
# Don't spend a shield rescuing a single isolated day — that isn't a streak yet.
STREAK_FREEZE_MIN_STREAK = 2


async def _settings_int(db, user_id: int, key: str, default: int = 0) -> int:
    async with db.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key)
    ) as cur:
        row = await cur.fetchone()
    try:
        return int(row[0]) if row else default
    except (ValueError, TypeError):
        return default


async def earn_streak_freeze(user_id: int) -> bool:
    """Advance the freeze-earn counter by one completed lesson. Every
    STREAK_FREEZE_PER_LESSONS lessons grants a freeze (capped at
    STREAK_FREEZE_CAP). Returns True iff a freeze was just awarded."""
    async with connect() as db:
        progress = await _settings_int(db, user_id, "streak_freeze_progress") + 1
        freezes = await _settings_int(db, user_id, "streak_freezes")
        awarded = False
        if progress >= STREAK_FREEZE_PER_LESSONS:
            if freezes < STREAK_FREEZE_CAP:
                freezes += 1
                awarded = True
                progress = 0
            else:
                # At cap — hold progress at the threshold so the next lesson after
                # a freeze is consumed grants one immediately.
                progress = STREAK_FREEZE_PER_LESSONS
        await db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, key, value) "
            "VALUES (?, 'streak_freeze_progress', ?)",
            (user_id, str(progress)),
        )
        if awarded:
            await db.execute(
                "INSERT OR REPLACE INTO user_settings (user_id, key, value) "
                "VALUES (?, 'streak_freezes', ?)",
                (user_id, str(freezes)),
            )
        await db.commit()
        return awarded


async def get_streak_freeze_state(user_id: int) -> dict:
    """Return {freezes, used_date} for surfacing the shield + a consumed toast."""
    async with connect() as db:
        freezes = await _settings_int(db, user_id, "streak_freezes")
        async with db.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key='streak_freeze_used_date'",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    return {"freezes": max(0, freezes), "used_date": row[0] if row else None}


# ── Tutor chat ─────────────────────────────────────────────────────────────────

async def create_tutor_conversation(user_id: int, lang: str) -> int:
    async with connect() as db:
        cur = await db.execute(
            "INSERT INTO tutor_conversations (user_id, lang) VALUES (?, ?)",
            (user_id, lang),
        )
        await db.commit()
        return cur.lastrowid


async def list_tutor_conversations(user_id: int, lang: str, limit: int = 30) -> list[dict]:
    """Most recent first. Lean rows for the conversation drawer."""
    async with connect() as db:
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, lang, title, active_drill_id FROM tutor_conversations WHERE id=? AND user_id=?",
            (conv_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_tutor_conversation(user_id: int, conv_id: int) -> None:
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            """UPDATE tutor_messages SET drill_id=?, drill_skill=?
               WHERE id=? AND conversation_id IN
                 (SELECT id FROM tutor_conversations WHERE user_id=?)""",
            (drill_id, drill_skill, msg_id, user_id),
        )
        await db.commit()


async def set_active_drill(user_id: int, conv_id: int, drill_id: int | None) -> None:
    """Set (start) or clear (end) the conversation's active drill sub-session."""
    async with connect() as db:
        await db.execute(
            "UPDATE tutor_conversations SET active_drill_id=? WHERE id=? AND user_id=?",
            (drill_id, conv_id, user_id),
        )
        await db.commit()


# ── Points ledger (light gamification) ──────────────────────────────────────────

async def add_points(user_id: int, lang: str, points: int, reason: str = "") -> None:
    if points <= 0:
        return
    async with connect() as db:
        await db.execute(
            "INSERT INTO points_ledger (user_id, lang, points, reason) VALUES (?, ?, ?, ?)",
            (user_id, lang, int(points), (reason or "").strip()[:200]),
        )
        # Earning XP is, by definition, study — record the active day in the SAME
        # transaction so the 🔥 streak can NEVER lag behind XP, no matter which
        # feature awarded it (review, lesson, tutor, reader comprehension, …).
        # `_mark_study_day` resolves the user's local day and spends any freeze
        # the gap needs, so XP earned as the first action of the day can't reset
        # a streak the learner had shields for.
        await _mark_study_day(db, user_id)
        await db.commit()


async def get_points_total(user_id: int, lang: str) -> int:
    async with connect() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(points), 0) FROM points_ledger WHERE user_id=? AND lang=?",
            (user_id, lang),
        ) as cur:
            return (await cur.fetchone())[0]


async def get_points_today(user_id: int, lang: str) -> int:
    """XP earned today (all languages) — drives the daily-goal ring. Windowed to
    the user's LOCAL day so the ring rolls over on the same boundary as the 🔥
    streak and daily quests, instead of resetting mid-afternoon for anyone west
    of UTC. (`created_at` is stored as UTC `datetime('now')`, so we compare
    against that local day's UTC bounds.)"""
    async with connect() as db:
        start, end = local_day_bounds(await _tz_for(db, user_id))
        async with db.execute(
            "SELECT COALESCE(SUM(points), 0) FROM points_ledger "
            "WHERE user_id=? AND created_at >= ? AND created_at < ?",
            (user_id, start, end),
        ) as cur:
            return (await cur.fetchone())[0]


# ── Daily quests (gamification) ─────────────────────────────────────────────
# 3 rotating quests per user per day, seeded lazily on first read. The `earn_xp`
# quest is always included (it's the anchor the daily-goal ring already trains
# users on); the other 2 rotate deterministically from user_id + date so friends
# see different mixes. `mode`: "sum" quests accumulate, "max" quests track a
# high-water mark (e.g. best combo). Progress comes from server-side events
# where possible; combo/listening are client-reported (same trust model as
# lesson XP — clamped, low stakes).

QUEST_TEMPLATES = {
    "earn_xp":   {"name": "Earn {n} XP",                   "icon": "⭐", "target": 40, "mode": "sum"},
    "combo":     {"name": "Hit a {n}-combo",               "icon": "⚡", "target": 5,  "mode": "max"},
    "lessons":   {"name": "Complete {n} lessons",          "icon": "📘", "target": 2,  "mode": "sum"},
    "add_cards": {"name": "Add {n} words to your deck",    "icon": "📦", "target": 3,  "mode": "sum"},
    "reviews":   {"name": "Review {n} flashcards",         "icon": "🃏", "target": 10, "mode": "sum"},
    "perfect":   {"name": "Finish a perfect lesson",       "icon": "🏆", "target": 1,  "mode": "sum"},
    "tutor":     {"name": "Send {n} tutor messages",       "icon": "💬", "target": 3,  "mode": "sum"},
    "listening": {"name": "Get {n} listening drills right", "icon": "🔊", "target": 5, "mode": "sum"},
    "lightning": {"name": "Finish a lightning round",       "icon": "⚡", "target": 1,  "mode": "sum"},
}
_QUEST_ROTATION = sorted(k for k in QUEST_TEMPLATES if k != "earn_xp")
# Client-reported quest keys (everything else is bumped by server-side events).
CLIENT_QUEST_KEYS = {"combo", "listening", "lightning"}
CHEST_XP_MIN, CHEST_XP_MAX = 20, 40


def _utc_today() -> str:
    """UTC today as an ISO string. Retained for server-side analytics windows
    (DAU/WAU/MAU) that are deliberately global rather than per-learner — every
    per-user "today" goes through `user_today` / `_local_today`."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()


def quest_keys_for(user_id: int, quest_date: str) -> list[str]:
    """The 3 quest keys for a user+day: earn_xp + 2 deterministic rotations."""
    h = int(hashlib.sha256(f"{user_id}:{quest_date}".encode()).hexdigest(), 16)
    n = len(_QUEST_ROTATION)
    first = h % n
    second = (first + 1 + (h // n) % (n - 1)) % n
    return ["earn_xp", _QUEST_ROTATION[first], _QUEST_ROTATION[second]]


def chest_xp_for(user_id: int, quest_date: str) -> int:
    """Deterministic chest bonus (20–40 XP) so a retried claim can't re-roll."""
    h = int(hashlib.sha256(f"chest:{user_id}:{quest_date}".encode()).hexdigest(), 16)
    return CHEST_XP_MIN + h % (CHEST_XP_MAX - CHEST_XP_MIN + 1)


async def get_daily_quests(user_id: int) -> list[dict]:
    """Today's 3 quests, seeding them on first call. The earn_xp quest's
    progress is re-derived from the points ledger every read (it can never
    drift); the rest accumulate via bump_quest."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        tz = await _tz_for(db, user_id)
        today = local_today_str(tz)
        keys = quest_keys_for(user_id, today)
        xp_start, xp_end = local_day_bounds(tz)
        for key in keys:
            await db.execute(
                """INSERT OR IGNORE INTO daily_quests
                   (user_id, quest_date, quest_key, target)
                   VALUES (?, ?, ?, ?)""",
                (user_id, today, key, QUEST_TEMPLATES[key]["target"]),
            )
        await db.execute(
            """UPDATE daily_quests
               SET progress = (SELECT COALESCE(SUM(points), 0) FROM points_ledger
                               WHERE user_id=? AND created_at >= ? AND created_at < ?)
               WHERE user_id=? AND quest_date=? AND quest_key='earn_xp'""",
            (user_id, xp_start, xp_end, user_id, today),
        )
        await db.commit()
        async with db.execute(
            """SELECT quest_key, target, progress, claimed FROM daily_quests
               WHERE user_id=? AND quest_date=?""",
            (user_id, today),
        ) as cur:
            rows = {r["quest_key"]: dict(r) for r in await cur.fetchall()}
    out = []
    for key in keys:               # template order, not row order
        r = rows.get(key)
        if not r:
            continue
        t = QUEST_TEMPLATES[key]
        progress = min(r["progress"], r["target"])
        out.append({
            "key": key,
            "name": t["name"].format(n=r["target"]),
            "icon": t["icon"],
            "target": r["target"],
            "progress": progress,
            "done": progress >= r["target"],
            "claimed": bool(r["claimed"]),
        })
    return out


async def bump_quest(user_id: int, quest_key: str, amount: int = 1,
                     value: int | None = None) -> None:
    """Advance one of today's quests. No-op if the quest isn't among today's
    seeded rows (cheap enough to call unconditionally from event sites).
    "sum" quests add `amount`; "max" quests raise progress to `value`."""
    tpl = QUEST_TEMPLATES.get(quest_key)
    if not tpl:
        return
    async with connect() as db:
        today = await _local_today(db, user_id)
        if tpl["mode"] == "max":
            if value is None:
                return
            await db.execute(
                """UPDATE daily_quests SET progress = MAX(progress, ?)
                   WHERE user_id=? AND quest_date=? AND quest_key=?""",
                (max(0, int(value)), user_id, today, quest_key),
            )
        else:
            if amount <= 0:
                return
            await db.execute(
                """UPDATE daily_quests SET progress = progress + ?
                   WHERE user_id=? AND quest_date=? AND quest_key=?""",
                (int(amount), user_id, today, quest_key),
            )
        await db.commit()


async def claim_daily_chest(user_id: int) -> int | None:
    """Open today's chest: all 3 quests complete and not yet claimed → mark
    claimed and return the bonus XP (deterministic). Else None. The caller
    appends the XP to points_ledger (reason 'quest')."""
    today = await user_today(user_id)
    quests = await get_daily_quests(user_id)   # seeds + syncs earn_xp
    if len(quests) < 3 or not all(q["done"] for q in quests):
        return None
    if any(q["claimed"] for q in quests):
        return None
    async with connect() as db:
        # Guard against a concurrent double-claim: only flip unclaimed rows.
        cur = await db.execute(
            "UPDATE daily_quests SET claimed=1 WHERE user_id=? AND quest_date=? AND claimed=0",
            (user_id, today),
        )
        await db.commit()
        if cur.rowcount == 0:
            return None
    return chest_xp_for(user_id, today)


# ── Friends weekly XP league (lesson redesign B2) ────────────────────────────

async def get_weekly_league(user_id: int) -> list[dict]:
    """XP earned this ISO week (Mon 00:00 UTC) by the user + accepted friends,
    ranked descending. No new tables — one aggregate over points_ledger.
    Returns [] when the user has no friends (the UI hides the strip)."""
    friends = (await get_friends(user_id))["friends"]
    if not friends:
        return []
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()

    names = {f["user_id"]: f["username"] for f in friends}
    avatars = {f["user_id"]: f.get("avatar_url") for f in friends}
    ids = [user_id] + list(names)
    placeholders = ",".join("?" * len(ids))
    async with connect() as db:
        async with db.execute(
            "SELECT username, avatar_media_id FROM users WHERE id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            names[user_id] = row[0] if row else "you"
            avatars[user_id] = f"/api/media/{row[1]}.jpg" if row and row[1] else None
        async with db.execute(
            f"""SELECT user_id, COALESCE(SUM(points), 0) AS xp FROM points_ledger
                WHERE user_id IN ({placeholders}) AND date(created_at) >= ?
                GROUP BY user_id""",
            (*ids, week_start),
        ) as cur:
            xp = {r[0]: r[1] for r in await cur.fetchall()}

    rows = [{"user_id": uid, "username": names[uid], "avatar_url": avatars.get(uid), "xp": xp.get(uid, 0),
             "you": uid == user_id} for uid in ids]
    rows.sort(key=lambda r: (-r["xp"], r["username"].lower()))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


# ── Unit checkpoints (lesson redesign B3) ────────────────────────────────────

async def get_unit_checkpoint_pool(user_id: int, unit_id: int) -> dict | None:
    """The material a unit checkpoint quiz samples from: the unit row + each of
    its lessons' stored content. Ownership-checked; foundations units have no
    checkpoint. Returns None when the unit doesn't exist / isn't the user's."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT u.id, u.title, u.checkpoint_passed, u.checkpoint_score,
                      c.target_lang, c.id AS course_id
               FROM course_units u JOIN courses c ON c.id = u.course_id
               WHERE u.id=? AND c.user_id=? AND COALESCE(u.theme, '') != 'foundations'""",
            (unit_id, user_id),
        ) as cur:
            unit = await cur.fetchone()
        if not unit:
            return None
        unit = dict(unit)
        async with db.execute(
            """SELECT id, title, content FROM course_lessons
               WHERE unit_id=? ORDER BY lesson_num""",
            (unit_id,),
        ) as cur:
            lessons = [dict(r) for r in await cur.fetchall()]
    for l in lessons:
        try:
            l["content"] = json.loads(l["content"]) if l["content"] else None
        except (ValueError, TypeError):
            l["content"] = None
    unit["lessons"] = lessons
    return unit


async def get_completed_lesson_contents(user_id: int, course_id: int) -> list[dict]:
    """Return {id, title, content} for every COMPLETED, non-foundations lesson in a
    course. Powers the practice hub (B6) — mistakes review + course-wide lightning
    recombine these stored drills. Ownership-checked via the course join."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT l.id, l.title, l.content
               FROM course_lessons l
               JOIN courses c ON c.id = l.course_id
               LEFT JOIN course_units u ON u.id = l.unit_id
               WHERE l.course_id=? AND c.user_id=? AND l.completed_at IS NOT NULL
                     AND COALESCE(u.theme, '') != 'foundations'
               ORDER BY l.lesson_num DESC""",
            (course_id, user_id),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        try:
            r["content"] = json.loads(r["content"]) if r["content"] else None
        except (ValueError, TypeError):
            r["content"] = None
    return rows


async def record_checkpoint(user_id: int, unit_id: int, score: int,
                            passed: bool) -> tuple[bool, bool]:
    """Record a checkpoint attempt (best score kept; passed is sticky).
    Returns (found, first_pass) — first_pass is True only the FIRST time the
    checkpoint is passed, so the bonus XP is awarded once."""
    async with connect() as db:
        async with db.execute(
            """SELECT u.checkpoint_passed FROM course_units u
               JOIN courses c ON c.id = u.course_id
               WHERE u.id=? AND c.user_id=? AND COALESCE(u.theme, '') != 'foundations'""",
            (unit_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return (False, False)
        first_pass = passed and not row[0]
        await db.execute(
            """UPDATE course_units
               SET checkpoint_score  = MAX(COALESCE(checkpoint_score, 0), ?),
                   checkpoint_passed = MAX(checkpoint_passed, ?)
               WHERE id=?""",
            (int(score), 1 if passed else 0, unit_id),
        )
        await db.commit()
    return (True, first_pass)


# ── Embedding cache ───────────────────────────────────────────────────────────
# Shared word→vector cache (a word embeds the same for every user). Vectors are
# stored as packed float32 BLOBs; pack/unpack live in embeddings.py.

async def get_cached_embeddings(lang: str, model: str, words: list[str]) -> dict[str, bytes]:
    """Return {word: vector_blob} for the cached subset of `words`."""
    words = [w for w in {w for w in words} if w]
    if not words:
        return {}
    out: dict[str, bytes] = {}
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            """DELETE FROM friendships
               WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)""",
            (user_id, other_user_id, other_user_id, user_id),
        )
        await db.commit()


async def get_friends(user_id: int) -> dict:
    """Return friends, pending sent requests, and pending received requests."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT f.id, f.requester_id, f.addressee_id, f.status, f.created_at,
                      r.username AS requester_name, a.username AS addressee_name,
                      r.avatar_media_id AS requester_avatar, a.avatar_media_id AS addressee_avatar
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
        other_avatar = r["addressee_avatar"] if r["requester_id"] == user_id else r["requester_avatar"]
        entry = {
            "id": r["id"], "user_id": other_id, "username": other_name,
            "avatar_url": f"/api/media/{other_avatar}.jpg" if other_avatar else None,
            "created_at": r["created_at"],
        }
        if r["status"] == "accepted":
            friends.append(entry)
        elif r["requester_id"] == user_id:
            sent.append(entry)
        else:
            received.append(entry)
    return {"friends": friends, "sent": sent, "received": received}


async def are_friends(user_id: int, other_user_id: int) -> bool:
    """Return whether two distinct users have an accepted friendship."""
    if user_id == other_user_id:
        return False
    async with connect() as db:
        async with db.execute(
            """SELECT 1 FROM friendships
               WHERE status='accepted' AND (
                 (requester_id=? AND addressee_id=?) OR
                 (requester_id=? AND addressee_id=?)
               )""",
            (user_id, other_user_id, other_user_id, user_id),
        ) as cur:
            return await cur.fetchone() is not None


# ── Conversations + Messages ───────────────────────────────────────────────────

async def get_or_create_conversation(user1_id: int, user2_id: int) -> dict:
    """Get or create an in-app 1:1 conversation. IDs are normalised (min < max)."""
    a, b = min(user1_id, user2_id), max(user1_id, user2_id)
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT c.id, c.user1_id, c.user2_id, c.platform, c.platform_thread_id,
                      c.owner_user_id, c.last_message_at,
                      u1.username AS user1_name, u2.username AS user2_name,
                      u1.avatar_media_id AS user1_avatar, u2.avatar_media_id AS user2_avatar,
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
            other_avatar = r["user2_avatar"] if r["user1_id"] == user_id else r["user1_avatar"]
            conv = {
                "id": r["id"], "type": "inapp",
                "other_user_id": other_id, "name": other_name,
                "avatar_url": f"/api/media/{other_avatar}.jpg" if other_avatar else None,
                "unread": r["unread"], "last_text": r["last_text"],
                "last_translations": r["last_translations"],
                "last_sender_id": r["last_sender_id"],
                "last_message_at": r["last_message_at"],
            }
        result.append(conv)
    return result


async def get_messages(conversation_id: int, viewer_user_id: int,
                       limit: int = 50, before_id: int | None = None) -> list[dict]:
    async with connect() as db:
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
    async with connect() as db:
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


async def toggle_reaction(message_id: int, user_id: int, emoji: str) -> bool | None:
    """Toggle a participant's reaction; return None when access is denied."""
    async with connect() as db:
        async with db.execute(
            """SELECT 1 FROM messages m
               JOIN conversations c ON c.id=m.conversation_id
               WHERE m.id=? AND (
                 c.user1_id=? OR c.user2_id=? OR c.owner_user_id=?
               )""",
            (message_id, user_id, user_id, user_id),
        ) as cur:
            if not await cur.fetchone():
                return None
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
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            "UPDATE messages SET translations=? WHERE id=?",
            (json.dumps(translations, ensure_ascii=False), msg_id),
        )
        await db.commit()


async def get_message(msg_id: int, user_id: int) -> dict | None:
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        row = await conn.execute_fetchall(
            """SELECT m.id, m.conversation_id, m.sender_user_id,
                      m.original_text, m.original_lang, m.translations, m.analysis
               FROM messages m
               JOIN conversations c ON c.id = m.conversation_id
               WHERE m.id = ? AND (
                 c.user1_id = ? OR c.user2_id = ? OR c.owner_user_id = ?
               )""",
            (msg_id, user_id, user_id, user_id),
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
    async with connect() as conn:
        await conn.execute(
            "UPDATE messages SET translations=?, analysis=? WHERE id=?",
            (json.dumps(translations, ensure_ascii=False),
             json.dumps(analysis, ensure_ascii=False), msg_id),
        )
        await conn.commit()


async def mark_conversation_read(conversation_id: int, reader_user_id: int) -> bool:
    async with connect() as db:
        async with db.execute(
            """SELECT 1 FROM conversations
               WHERE id=? AND (user1_id=? OR user2_id=? OR owner_user_id=?)""",
            (conversation_id, reader_user_id, reader_user_id, reader_user_id),
        ) as cur:
            if not await cur.fetchone():
                return False
        await db.execute(
            """UPDATE messages SET read_at=datetime('now')
               WHERE conversation_id=? AND read_at IS NULL
                 AND (sender_user_id IS NULL OR sender_user_id != ?)""",
            (conversation_id, reader_user_id),
        )
        await db.commit()
        return True


async def get_total_unread(user_id: int) -> int:
    async with connect() as db:
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
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM friendships WHERE addressee_id=? AND status='pending'",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


# ── Push subscriptions ─────────────────────────────────────────────────────────

async def add_push_subscription(user_id: int, endpoint: str, p256dh: str, auth_key: str) -> None:
    async with connect() as db:
        await db.execute(
            """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET
                 user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth""",
            (user_id, endpoint, p256dh, auth_key),
        )
        await db.commit()


async def get_push_subscriptions(user_id: int) -> list[dict]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=?",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def remove_push_subscription(user_id: int, endpoint: str) -> None:
    async with connect() as db:
        await db.execute(
            "DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?",
            (user_id, endpoint),
        )
        await db.commit()


# ── Media uploads ──────────────────────────────────────────────────────────────

async def add_media_record(media_id: str, user_id: int, conv_id: int | None, size_bytes: int) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO media (id, user_id, conv_id, size_bytes) VALUES (?, ?, ?, ?)",
            (media_id, user_id, conv_id, size_bytes),
        )
        await db.commit()


async def update_image_message(msg_id: int, description: str, analysis: dict) -> None:
    analysis_json = json.dumps(analysis, ensure_ascii=False)
    async with connect() as db:
        await db.execute(
            "UPDATE messages SET original_text=?, analysis=? WHERE id=?",
            (f"📷 {description}", analysis_json, msg_id),
        )
        await db.commit()


async def delete_message(msg_id: int, user_id: int) -> dict | None:
    """Soft-delete a message (clear content, mark deleted). Returns old analysis for media cleanup."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        row = await conn.execute_fetchall(
            """SELECT m.id, m.analysis
               FROM messages m
               JOIN conversations c ON c.id = m.conversation_id
               WHERE m.id = ? AND m.sender_user_id = ?
                 AND (c.user1_id = ? OR c.user2_id = ? OR c.owner_user_id = ?)""",
            (msg_id, user_id, user_id, user_id, user_id),
        )
        if not row:
            return None
        analysis = json.loads(row[0]["analysis"]) if row[0]["analysis"] else {}
        deleted_analysis = json.dumps({"deleted": True}, ensure_ascii=False)
        await conn.execute(
            "UPDATE messages SET original_text='', translations=NULL, analysis=?, sent_text=NULL WHERE id=?",
            (deleted_analysis, msg_id),
        )
        await conn.execute("DELETE FROM message_reactions WHERE message_id=?", (msg_id,))
        await conn.commit()
    return analysis


async def get_unprocessed_image_messages() -> list[dict]:
    """Find image messages whose vision analysis produced no suggestions."""
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        rows = await conn.execute_fetchall(
            """SELECT m.id, m.sender_user_id, m.conversation_id, m.analysis,
                      c.user1_id, c.user2_id
               FROM messages m
               JOIN conversations c ON c.id = m.conversation_id
               WHERE m.analysis LIKE '%"type": "image"%'
                  OR m.analysis LIKE '%"type":"image"%'
               ORDER BY m.id""",
        )
    results = []
    for r in rows:
        analysis = json.loads(r["analysis"]) if r["analysis"] else {}
        if analysis.get("type") != "image":
            continue
        sugg = analysis.get("suggestions") or {}
        has_content = any(
            (isinstance(v, dict) and (v.get("sender") or v.get("receiver")))
            or (isinstance(v, list) and v)
            for v in sugg.values()
        )
        if has_content:
            continue
        url = analysis.get("url", "")
        media_id = url.rsplit("/", 1)[-1].replace(".jpg", "") if url else ""
        results.append({
            "msg_id": r["id"],
            "sender_user_id": r["sender_user_id"],
            "conversation_id": r["conversation_id"],
            "user1_id": r["user1_id"],
            "user2_id": r["user2_id"],
            "media_id": media_id,
            "analysis": analysis,
        })
    return results


# ── Messenger account ──────────────────────────────────────────────────────────

async def get_messenger_account(user_id: int) -> dict | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT page_id, page_name, page_access_token FROM messenger_accounts WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_messenger_account(user_id: int, page_id: str,
                                   page_access_token: str, page_name: str | None) -> None:
    async with connect() as db:
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
    async with connect() as db:
        await db.execute("DELETE FROM messenger_accounts WHERE user_id=?", (user_id,))
        await db.commit()


async def get_user_by_messenger_page(page_id: str) -> dict | None:
    """Find the app user who connected this Messenger page."""
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT provider, provider_user_id, access_token, display_name, connected_at
               FROM social_accounts WHERE user_id=? AND provider=?""",
            (user_id, provider),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def delete_social_account(user_id: int, provider: str) -> None:
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            """UPDATE feedback SET triage_summary=?, triage_group=?, suggested_prompt=?,
                      priority=?, updated_at=datetime('now')
               WHERE id=?""",
            (triage_summary, triage_group, suggested_prompt, priority, feedback_id),
        )
        await db.commit()


async def list_feedback(status: str | None = None) -> list[dict]:
    """List all feedback (admin). Optionally filter by status."""
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        await db.execute(
            """UPDATE feedback SET status=?, admin_notes=?, updated_at=datetime('now')
               WHERE id=?""",
            (status, admin_notes, feedback_id),
        )
        await db.commit()


async def list_feedback_groups() -> list[dict]:
    """Return unique triage_group values with counts, for grouping similar reports."""
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as conn:
        async with conn.execute("SELECT id FROM users WHERE is_admin=1") as cur:
            return [row[0] for row in await cur.fetchall()]


# ── Story sharing ────────────────────────────────────────────────────────────


async def publish_story(user_id: int, text_id: int, visibility: str) -> bool:
    if visibility not in ("private", "friends", "public"):
        return False
    async with connect() as db:
        await _ensure_reader_cols(db)
        cur = await db.execute(
            "UPDATE reader_texts SET visibility=? WHERE id=? AND user_id=?",
            (visibility, text_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def rate_story(user_id: int, text_id: int, rating: int) -> None:
    async with connect() as db:
        await db.execute(
            """INSERT INTO story_ratings (text_id, user_id, rating)
               VALUES (?, ?, ?)
               ON CONFLICT(text_id, user_id) DO UPDATE SET rating=excluded.rating""",
            (text_id, user_id, rating),
        )
        await db.commit()


async def get_story_rating(text_id: int) -> dict:
    async with connect() as db:
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
    async with connect() as db:
        async with db.execute(
            "SELECT rating FROM story_ratings WHERE user_id=? AND text_id=?",
            (user_id, text_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_story_public(text_id: int, requesting_user_id: int) -> dict | None:
    """Get a story if the requesting user has access (own, public, or friend-shared)."""
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
        cur = await db.execute(
            """INSERT INTO shared_decks (creator_id, name, description, target_lang, visibility)
               VALUES (?, ?, ?, ?, ?)""",
            (creator_id, name, description, target_lang, visibility),
        )
        deck_id = cur.lastrowid
        for i, item in enumerate(items):
            await db.execute(
                """INSERT INTO shared_deck_items
                   (deck_id, source_text, target_text, romanization, notes, sort_order, target_lang, cefr_level, labels)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (deck_id, item["source_text"], item["target_text"],
                 item.get("romanization"), item.get("notes"), i,
                 item.get("target_lang") or target_lang,
                 item.get("cefr_level"), _labels_json(item.get("labels"))),
            )
        await db.commit()
        return deck_id


def _labels_json(labels) -> str | None:
    """Normalize a deck item's labels to a JSON array string (or None)."""
    if not labels:
        return None
    if isinstance(labels, str):
        return labels  # already serialized
    clean = [str(x).strip() for x in labels if str(x).strip()]
    return json.dumps(clean) if clean else None


async def upsert_featured_deck(
    creator_id: int, name: str, description: str,
    target_lang: str, items: list[dict],
) -> tuple[int, str]:
    """Create or update an official (system-owned) deck for a language IN PLACE.

    Matches an existing deck by (creator_id, target_lang) so re-seeding preserves
    the deck_id — importers' `deck_imports` and `deck_ratings` rows stay valid.
    Items are a content snapshot (importers already copied them to their own
    cards), so replacing them never affects existing importers. Returns
    (deck_id, "created"|"updated")."""
    async with connect() as db:
        async with db.execute(
            "SELECT id FROM shared_decks WHERE creator_id=? AND target_lang=? AND name=?",
            (creator_id, target_lang, name),
        ) as cur:
            row = await cur.fetchone()
        if row:
            deck_id, action = row[0], "updated"
            await db.execute(
                "UPDATE shared_decks SET description=?, visibility='public' WHERE id=?",
                (description, deck_id),
            )
            await db.execute("DELETE FROM shared_deck_items WHERE deck_id=?", (deck_id,))
        else:
            cur = await db.execute(
                """INSERT INTO shared_decks (creator_id, name, description, target_lang, visibility)
                   VALUES (?, ?, ?, ?, 'public')""",
                (creator_id, name, description, target_lang),
            )
            deck_id, action = cur.lastrowid, "created"
        for i, item in enumerate(items):
            await db.execute(
                """INSERT INTO shared_deck_items
                   (deck_id, source_text, target_text, romanization, notes, sort_order, target_lang, cefr_level, labels)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (deck_id, item["source_text"], item["target_text"],
                 item.get("romanization"), item.get("notes"), i,
                 item.get("target_lang") or target_lang,
                 item.get("cefr_level"), _labels_json(item.get("labels"))),
            )
        await db.commit()
        return deck_id, action


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
    async with connect() as db:
        cur = await db.execute(
            f"UPDATE shared_decks SET {', '.join(updates)} WHERE id=? AND creator_id=?",
            params,
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_shared_deck(user_id: int, deck_id: int) -> bool:
    async with connect() as db:
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT sd.id, sd.name, sd.description, sd.target_lang,
                      sd.visibility, sd.created_at,
                      COALESCE(NULLIF(u.display_name,''), u.username) as creator,
                      sd.creator_id,
                      COUNT(sdi.id) as card_count,
                      (SELECT COUNT(*) FROM deck_imports di WHERE di.deck_id = sd.id) as import_count,
                      COALESCE(dr.avg_r, 0) as avg_rating,
                      COALESCE(dr.cnt, 0) as rating_count,
                      (SELECT rating FROM deck_ratings mr
                       WHERE mr.deck_id = sd.id AND mr.user_id = ?) as user_rating
               FROM shared_decks sd
               JOIN users u ON sd.creator_id = u.id
               LEFT JOIN shared_deck_items sdi ON sd.id = sdi.deck_id
               LEFT JOIN (SELECT deck_id, AVG(rating) as avg_r, COUNT(*) as cnt
                          FROM deck_ratings GROUP BY deck_id) dr ON dr.deck_id = sd.id
               WHERE sd.creator_id=?
                  OR EXISTS(SELECT 1 FROM deck_imports di2
                            WHERE di2.deck_id=sd.id AND di2.user_id=?)
               GROUP BY sd.id ORDER BY sd.created_at DESC""",
            (user_id, user_id, user_id),
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        params: list = [requesting_user_id, requesting_user_id,
                        requesting_user_id, requesting_user_id]
        sql = """
            SELECT sd.id, sd.name, sd.description, sd.target_lang,
                   sd.visibility, sd.created_at,
                   COALESCE(NULLIF(u.display_name,''), u.username) as creator,
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


async def list_featured_decks(target_lang: str | None = None) -> list[dict]:
    """Public decks owned by the system user (official Top-100 word decks etc.).
    Surfaced as onboarding suggestions. Returns [] if the system user or no
    matching deck exists."""
    sys_id = await get_system_user_id()
    if not sys_id:
        return []
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        params: list = [sys_id]
        sql = """
            SELECT sd.id, sd.name, sd.description, sd.target_lang,
                   COUNT(sdi.id) as card_count,
                   COALESCE(dr.avg_r, 0) as avg_rating,
                   COALESCE(dr.cnt, 0) as rating_count
            FROM shared_decks sd
            LEFT JOIN shared_deck_items sdi ON sd.id = sdi.deck_id
            LEFT JOIN (SELECT deck_id, AVG(rating) as avg_r, COUNT(*) as cnt
                       FROM deck_ratings GROUP BY deck_id) dr ON dr.deck_id = sd.id
            WHERE sd.creator_id = ? AND sd.visibility = 'public'
        """
        if target_lang:
            sql += " AND sd.target_lang = ?"
            params.append(target_lang)
        sql += " GROUP BY sd.id ORDER BY sd.created_at DESC"
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_shared_deck(deck_id: int, requesting_user_id: int) -> dict | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT sd.*, COALESCE(NULLIF(u.display_name,''), u.username) as creator
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
            """SELECT source_text, target_text, romanization, notes, target_lang, cefr_level
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
    async with connect() as db:
        await db.execute(
            """INSERT INTO deck_ratings (deck_id, user_id, rating)
               VALUES (?, ?, ?)
               ON CONFLICT(deck_id, user_id) DO UPDATE SET rating=excluded.rating""",
            (deck_id, user_id, rating),
        )
        await db.commit()


async def get_deck_rating(deck_id: int) -> dict:
    async with connect() as db:
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
    async with connect() as db:
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
        # Get-or-create the importer's own deck label. (Don't rely on INSERT OR
        # IGNORE + lastrowid: with the legacy global UNIQUE(name) an ignored insert
        # left lastrowid stale and the per-user lookup empty → NoneType crash.)
        async with db.execute(
            "SELECT id FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
            (user_id, label_name),
        ) as cur:
            lrow = await cur.fetchone()
        if lrow:
            label_id = lrow[0]
        else:
            cur = await db.execute(
                "INSERT INTO labels (user_id, name) VALUES (?, ?)",
                (user_id, label_name),
            )
            label_id = cur.lastrowid
        async with db.execute(
            """SELECT source_text, target_text, romanization, notes, target_lang, cefr_level, labels
               FROM shared_deck_items WHERE deck_id=? ORDER BY sort_order""",
            (deck_id,),
        ) as cur:
            items = [dict(r) for r in await cur.fetchall()]

        # Set-based import: do a handful of bulk queries instead of ~6 per card.
        # A 2000-card deck previously fired ~12k awaited round-trips inside one
        # long write transaction, which timed out. Now it's a few executemany's.
        langs = {(it.get("target_lang") or target_lang) for it in items}
        existing: dict[tuple, int] = {}
        if langs:
            q = ("SELECT id, target_text, target_lang FROM cards "
                 "WHERE user_id=? AND target_lang IN (%s)"
                 % ",".join("?" * len(langs)))
            async with db.execute(q, (user_id, *langs)) as cur:
                for r in await cur.fetchall():
                    existing.setdefault((r["target_text"], r["target_lang"]), r["id"])

        # Figure out which items are genuinely new (deduped by target+lang).
        new_rows: list[tuple] = []
        seen_new: set[tuple] = set()
        for it in items:
            key = (it["target_text"], it.get("target_lang") or target_lang)
            if key in existing or key in seen_new:
                continue
            seen_new.add(key)
            cefr = it.get("cefr_level")
            if cefr not in ("A1", "A2", "B1", "B2", "C1", "C2"):
                cefr = None
            new_rows.append((
                user_id, it["source_text"], it["target_text"],
                # cards.romanization is NOT NULL DEFAULT '' — a deck item with
                # no romanization (None) would otherwise fail the bulk insert.
                it.get("romanization") or "", key[1], it.get("notes"), cefr,
            ))

        created_ids: list[int] = []
        if new_rows:
            async with db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM cards") as cur:
                before_max = (await cur.fetchone())["m"]
            await db.executemany(
                """INSERT INTO cards
                   (user_id, source_text, target_text, romanization,
                    target_lang, notes, cefr_level, priority)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 3)""",
                new_rows,
            )
            # No other writer can interleave inside our held write transaction,
            # so every row we just inserted has id > before_max. Re-read them to
            # map (target_text, lang) -> new id (robust, no rowid-order assumption).
            async with db.execute(
                "SELECT id, target_text, target_lang FROM cards WHERE user_id=? AND id>?",
                (user_id, before_max),
            ) as cur:
                for r in await cur.fetchall():
                    existing[(r["target_text"], r["target_lang"])] = r["id"]
                    created_ids.append(r["id"])

            await db.executemany(
                "INSERT OR IGNORE INTO card_faces (card_id, face) VALUES (?, ?)",
                [(cid, face) for cid in created_ids
                 for face in ("source", "target", "pronunciation")],
            )
            # Remember exactly which cards this import created so un-import can
            # delete only these (never the user's pre-existing cards).
            await db.executemany(
                "INSERT OR IGNORE INTO deck_import_cards (user_id, deck_id, card_id) VALUES (?, ?, ?)",
                [(user_id, deck_id, cid) for cid in created_ids],
            )

        # Tag every deck card (new + pre-existing match) with the deck label.
        label_card_ids = {
            existing[(it["target_text"], it.get("target_lang") or target_lang)]
            for it in items
            if (it["target_text"], it.get("target_lang") or target_lang) in existing
        }
        await db.executemany(
            "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
            [(cid, label_id) for cid in label_card_ids],
        )

        # Attach the deck's baked-in category labels (e.g. "pronoun", "food") so
        # imported cards arrive organised like translate-flow cards. Bulk: create
        # the distinct label set once, then map each card to its item's labels.
        # Only newly-created cards get category labels (pre-existing cards keep
        # their own organisation); the deck label above still tags everything.
        item_by_key = {
            (it["target_text"], it.get("target_lang") or target_lang): it
            for it in items
        }
        all_names: set[str] = set()
        card_label_names: list[tuple[int, str]] = []
        created_set = set(created_ids)
        for key, cid in existing.items():
            if cid not in created_set:
                continue
            it = item_by_key.get(key)
            if not it or not it.get("labels"):
                continue
            try:
                names = json.loads(it["labels"]) if isinstance(it["labels"], str) else it["labels"]
            except (json.JSONDecodeError, TypeError):
                names = []
            for nm in names:
                nm = str(nm).strip()
                if nm:
                    all_names.add(nm)
                    card_label_names.append((cid, nm))
        if all_names:
            await db.executemany(
                "INSERT OR IGNORE INTO labels (user_id, name) VALUES (?, ?)",
                [(user_id, nm) for nm in all_names],
            )
            name_to_id: dict[str, int] = {}
            async with db.execute(
                "SELECT id, name FROM labels WHERE user_id=?", (user_id,),
            ) as cur:
                for r in await cur.fetchall():
                    name_to_id[str(r["name"]).casefold()] = r["id"]
            await db.executemany(
                "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
                [(cid, name_to_id[nm.casefold()]) for cid, nm in card_label_names
                 if nm.casefold() in name_to_id],
            )

        await db.execute(
            "INSERT OR IGNORE INTO deck_imports (user_id, deck_id) VALUES (?, ?)",
            (user_id, deck_id),
        )
        await db.commit()
        created = len(created_ids)
        return {"ok": True, "created": created, "total": len(items),
                "label_id": label_id, "label_name": label_name}


async def unimport_deck(user_id: int, deck_id: int) -> dict:
    """Reverse an import: delete the cards this import CREATED (tracked in
    deck_import_cards), drop the "📦 {deck_name}" label, and remove the
    deck_imports row. Cards that pre-existed the import (it merely tagged them)
    are never deleted — they just lose the deck label. For legacy imports with no
    tracking rows, falls back to deleting cards tagged ONLY with the deck label."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT 1 FROM deck_imports WHERE user_id=? AND deck_id=?",
            (user_id, deck_id),
        ) as cur:
            if not await cur.fetchone():
                return {"ok": False, "error": "Not imported"}
        async with db.execute(
            "SELECT name FROM shared_decks WHERE id=?", (deck_id,),
        ) as cur:
            deck_row = await cur.fetchone()

        # The cards this import created (precise) — delete these outright, in
        # chunked bulk deletes (a 2000-card un-import is a few queries, not ~6k).
        async with db.execute(
            "SELECT card_id FROM deck_import_cards WHERE user_id=? AND deck_id=?",
            (user_id, deck_id),
        ) as cur:
            created_ids = [r["card_id"] for r in await cur.fetchall()]
        removed = 0
        for i in range(0, len(created_ids), 500):
            chunk = created_ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            await db.execute(f"DELETE FROM card_faces WHERE card_id IN ({ph})", chunk)
            await db.execute(f"DELETE FROM card_labels WHERE card_id IN ({ph})", chunk)
            cur = await db.execute(
                f"DELETE FROM cards WHERE user_id=? AND id IN ({ph})",
                (user_id, *chunk),
            )
            removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(chunk)
        await db.execute(
            "DELETE FROM deck_import_cards WHERE user_id=? AND deck_id=?",
            (user_id, deck_id),
        )

        # Remove the "📦 deck" label (and untag any survivors). For legacy imports
        # (no tracking rows), delete cards left tagged ONLY with the deck label.
        if deck_row:
            label_name = f"📦 {deck_row['name']}"
            async with db.execute(
                "SELECT id FROM labels WHERE user_id=? AND name=? COLLATE NOCASE",
                (user_id, label_name),
            ) as cur:
                lbl = await cur.fetchone()
            if lbl:
                label_id = lbl["id"]
                if not created_ids:
                    async with db.execute(
                        "SELECT card_id FROM card_labels WHERE label_id=?", (label_id,),
                    ) as cur:
                        tagged = [r["card_id"] for r in await cur.fetchall()]
                    for cid in tagged:
                        async with db.execute(
                            "SELECT COUNT(*) AS n FROM card_labels WHERE card_id=?", (cid,),
                        ) as cur:
                            n = (await cur.fetchone())["n"]
                        if n <= 1:
                            await db.execute("DELETE FROM card_faces WHERE card_id=?", (cid,))
                            await db.execute("DELETE FROM card_labels WHERE card_id=?", (cid,))
                            await db.execute(
                                "DELETE FROM cards WHERE id=? AND user_id=?", (cid, user_id),
                            )
                            removed += 1
                await db.execute(
                    "DELETE FROM labels WHERE id=? AND user_id=?", (label_id, user_id),
                )
        await db.execute(
            "DELETE FROM deck_imports WHERE user_id=? AND deck_id=?",
            (user_id, deck_id),
        )
        await db.commit()
        return {"ok": True, "removed": removed}


async def label_cards_for_deck(user_id: int, deck_name: str, card_ids: list[int]) -> int | None:
    """Tag the creator's own cards with a "📦 {deck_name}" label so they can study
    the deck they just shared. Returns the label id (or None if no cards)."""
    if not card_ids:
        return None
    label_name = f"📦 {deck_name}"
    async with connect() as db:
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
