-- Story ratings
CREATE TABLE IF NOT EXISTS story_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(text_id, user_id),
    FOREIGN KEY (text_id) REFERENCES reader_texts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_story_ratings_text ON story_ratings(text_id);

-- Shared decks
CREATE TABLE IF NOT EXISTS shared_decks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    target_lang TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'friends',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (creator_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_shared_decks_creator ON shared_decks(creator_id);

CREATE TABLE IF NOT EXISTS shared_deck_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id INTEGER NOT NULL,
    source_text TEXT NOT NULL,
    target_text TEXT NOT NULL,
    romanization TEXT,
    notes TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (deck_id) REFERENCES shared_decks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS deck_imports (
    user_id INTEGER NOT NULL,
    deck_id INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY(user_id, deck_id),
    FOREIGN KEY (deck_id) REFERENCES shared_decks(id) ON DELETE CASCADE
);
