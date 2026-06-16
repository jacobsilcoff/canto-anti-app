-- Bug reports & feature requests with triage
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL DEFAULT 'bug',        -- 'bug' or 'feature'
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    screenshot_media_id TEXT,                -- references media.id (UUID hex), nullable
    status TEXT NOT NULL DEFAULT 'new',       -- new, triaged, in_progress, resolved, closed
    priority TEXT NOT NULL DEFAULT 'medium',  -- low, medium, high, critical
    triage_summary TEXT,                      -- AI-generated summary
    triage_group TEXT,                        -- AI-assigned group for dedup (e.g. "image-upload-broken")
    suggested_prompt TEXT,                    -- AI-suggested Claude prompt to implement fix
    admin_notes TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
