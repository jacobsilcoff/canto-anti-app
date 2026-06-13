-- Mark a unit's theme so the Foundations (reading) track can be distinguished
-- from AI-authored vocab units. Foundations units are skippable (not a hard gate).
ALTER TABLE course_units ADD COLUMN theme TEXT NOT NULL DEFAULT '';
