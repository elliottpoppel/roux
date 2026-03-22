-- Remove orphan expert_places that no user_place points to.
-- These were left behind when CID re-resolution changed place_ids
-- but didn't clean up the old expert_place records.
-- CASCADE on place_reviews, place_dishes, guide_mentions handles the rest.
-- Run in Supabase SQL Editor.

-- Preview first (uncomment to check before deleting):
-- SELECT ep.id, ep.name, ep.city
-- FROM expert_places ep
-- LEFT JOIN user_places up ON ep.google_place_id = up.place_id
-- WHERE up.id IS NULL;

DELETE FROM expert_places
WHERE google_place_id NOT IN (
    SELECT DISTINCT place_id FROM user_places WHERE place_id IS NOT NULL AND place_id != ''
);
