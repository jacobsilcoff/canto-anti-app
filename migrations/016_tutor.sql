-- Tutor chat (per-user conversations with the AI tutor) + light points ledger.
CREATE TABLE IF NOT EXISTS tutor_conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    lang       TEXT    NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',
    created_at TEXT    DEFAULT (datetime('now')),
    updated_at TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tutor_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role            TEXT    NOT NULL,           -- 'user' | 'tutor'
    content         TEXT    NOT NULL,           -- user: plain text; tutor: JSON payload
    created_at      TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tutor_msgs_conv ON tutor_messages(conversation_id);

CREATE TABLE IF NOT EXISTS points_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    lang       TEXT    NOT NULL,
    points     INTEGER NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    created_at TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_points_user ON points_ledger(user_id, lang);
