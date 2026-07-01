-- Enrich shared-deck items so imported cards arrive with the same richness as
-- translate-flow cards: a CEFR level (drives strength/CEFR badges) and a set of
-- category labels (JSON array of names). Populated for official Top-100 decks
-- from the committed seed files; NULL for user-created decks.
ALTER TABLE shared_deck_items ADD COLUMN cefr_level TEXT;
ALTER TABLE shared_deck_items ADD COLUMN labels TEXT;
