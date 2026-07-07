-- Long-lived API tokens for programmatic access (e.g. the Even glasses / MentraOS
-- flashcard plugin). We store sha256(token) only, never the raw token, mirroring
-- the sessions table — a DB leak can't be replayed. One active token per user is
-- the norm (regenerating replaces the old one), but the schema allows several.
CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);
