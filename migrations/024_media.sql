CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    conv_id INTEGER,
    size_bytes INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_media_user ON media(user_id);
