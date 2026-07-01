-- Track which cards a deck-import actually CREATED (vs pre-existing cards it
-- merely tagged with the deck label), so un-importing can delete only the
-- cards the import added and never touch the user's pre-existing vocab.
CREATE TABLE IF NOT EXISTS deck_import_cards (
    user_id INTEGER NOT NULL,
    deck_id INTEGER NOT NULL,
    card_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, deck_id, card_id),
    FOREIGN KEY (deck_id) REFERENCES shared_decks(id) ON DELETE CASCADE,
    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_deck_import_cards_deck
    ON deck_import_cards(user_id, deck_id);
