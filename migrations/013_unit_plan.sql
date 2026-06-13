-- Unit-plan-first authoring: the active (in-progress) unit's outline is stored
-- as JSON on the course so each micro-lesson can author 1-2 concepts from it.
-- NOTE: the baseline CREATE TABLE now includes this column, so on fresh DBs this
-- migration is a no-op (the runner swallows "duplicate column name" errors).
ALTER TABLE courses ADD COLUMN active_plan TEXT;
