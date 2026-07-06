-- User profile picture: references a row in `media` (see 024_media.sql).
ALTER TABLE users ADD COLUMN avatar_media_id TEXT;
