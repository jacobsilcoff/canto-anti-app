CREATE TABLE IF NOT EXISTS deck_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(deck_id, user_id),
    FOREIGN KEY (deck_id) REFERENCES shared_decks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_deck_ratings_deck ON deck_ratings(deck_id);
