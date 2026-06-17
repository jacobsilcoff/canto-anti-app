-- Connected social accounts (Facebook now, Instagram later) for identity +
-- mutual-app-friend discovery. The access token is stored encrypted (Fernet,
-- same key as user API keys). Friend matching uses provider_user_id to map a
-- user's app-using social friends back to our own users.
CREATE TABLE IF NOT EXISTS social_accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider         TEXT NOT NULL,                       -- 'facebook' | 'instagram'
    provider_user_id TEXT NOT NULL,                       -- the FB/IG account id
    access_token     TEXT,                                -- encrypted at rest
    display_name     TEXT,
    connected_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, provider),
    UNIQUE(provider, provider_user_id)
);
CREATE INDEX IF NOT EXISTS idx_social_provider_uid ON social_accounts(provider, provider_user_id);
