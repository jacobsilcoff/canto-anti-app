-- Explicit, bounded drill sub-sessions inside a tutor conversation.
-- A drill groups a contiguous run of messages; the learner ends it explicitly,
-- ended drills collapse in the UI, and drill turns are excluded from the normal
-- chat's LLM context (only a short "practiced: <skill>" note remains).
ALTER TABLE tutor_conversations ADD COLUMN active_drill_id INTEGER;   -- NULL = no active drill
ALTER TABLE tutor_messages ADD COLUMN drill_id INTEGER;              -- NULL = normal chat msg; else groups a drill
ALTER TABLE tutor_messages ADD COLUMN drill_skill TEXT;             -- the construction label (same for a group)
