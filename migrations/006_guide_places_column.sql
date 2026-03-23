-- Add guide_places column to guides table for deferred processing.
-- Stores the raw extracted places JSON from guide articles.
-- Cleared to NULL after batch processing completes.
-- Run in Supabase SQL Editor.

ALTER TABLE guides ADD COLUMN IF NOT EXISTS guide_places JSONB;
