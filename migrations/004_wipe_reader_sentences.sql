-- One-time wipe of cached reader sentence translations.
-- Old translations used the vocabulary-card prompt and produced bad results
-- (e.g. a whole sentence translated as a single word like "for example").
-- Deleting here causes them to be re-generated with the fixed sentence prompt.
DELETE FROM reader_sentences;
