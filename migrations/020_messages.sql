-- Conversations + messages for in-app friend chat and external platform (Messenger) import.
CREATE TABLE IF NOT EXISTS conversations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    -- In-app 1:1: both user IDs present (user1_id < user2_id), platform fields NULL
    user1_id            INTEGER REFERENCES users(id) ON DELETE CASCADE,
    user2_id            INTEGER REFERENCES users(id) ON DELETE CASCADE,
    -- External platform: owner_user_id is our app user
    platform            TEXT,               -- 'messenger' | NULL
    platform_thread_id  TEXT,               -- PSID or thread ID on the platform
    owner_user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
    last_message_at     TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conv_users ON conversations(user1_id, user2_id);
CREATE INDEX IF NOT EXISTS idx_conv_platform ON conversations(owner_user_id, platform, platform_thread_id);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_user_id      INTEGER REFERENCES users(id),   -- NULL for external senders
    sender_platform_id  TEXT,       -- external sender ID (e.g. PSID)
    sender_name         TEXT,       -- display name for external senders
    original_text       TEXT NOT NULL,
    original_lang       TEXT NOT NULL DEFAULT 'en',   -- lang code of what was typed
    -- JSON {lang_code: translated_text} — pre-computed on send, looked up by viewer's target lang
    translations        TEXT,
    -- Text actually sent to the external platform (may differ from original)
    sent_text           TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    read_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);

-- Per-user Facebook Messenger page connection.
CREATE TABLE IF NOT EXISTS messenger_accounts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    page_id             TEXT NOT NULL,
    page_access_token   TEXT NOT NULL,
    page_name           TEXT,
    connected_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
