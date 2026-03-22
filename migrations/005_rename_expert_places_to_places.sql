-- Rename expert_places to places.
-- It's the shared database of all places, not just "expert" ones.
-- FK columns (expert_place_id) stay as-is to avoid collision with user_places.place_id.
-- Run in Supabase SQL Editor.

ALTER TABLE expert_places RENAME TO places;
