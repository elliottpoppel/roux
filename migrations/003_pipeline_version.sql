-- Add pipeline_version to expert_places for incremental re-enrichment.
-- Places enriched with an older pipeline version will be automatically
-- re-enriched when the pipeline is upgraded.
-- Run in Supabase SQL Editor.

ALTER TABLE expert_places ADD COLUMN IF NOT EXISTS pipeline_version INTEGER DEFAULT 1;

-- Backfill: existing enriched places get version 1
UPDATE expert_places SET pipeline_version = 1 WHERE pipeline_version IS NULL AND last_enriched_at IS NOT NULL;
