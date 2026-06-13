CREATE TABLE IF NOT EXISTS concept_mastery (
    user_id     INTEGER NOT NULL,
    lang        TEXT    NOT NULL,
    concept_key TEXT    NOT NULL,
    correct     INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    last_seen   TEXT,
    PRIMARY KEY (user_id, lang, concept_key)
);
