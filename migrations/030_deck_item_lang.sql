-- Per-item language for shared decks, so a deck can hold cards from multiple
-- languages and each imported card is recreated in its own language.
ALTER TABLE shared_deck_items ADD COLUMN target_lang TEXT;
