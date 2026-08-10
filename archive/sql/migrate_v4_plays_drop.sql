-- Migration v4.0: Drop plays table, update player_edge schema
-- Run once in Supabase SQL Editor before running scripts 08, 06, 07.

-- 1. Drop plays table — no longer used after v4.0 rewrite (saves storage)
DROP TABLE IF EXISTS public.plays;

-- 2. Rename plays_counted → stats_measured (primary stat volume, not play count)
ALTER TABLE player_edge RENAME COLUMN plays_counted TO stats_measured;

-- 3. Drop deprecated EPA columns (replaced by game-adjusted stat composite)
ALTER TABLE player_edge DROP COLUMN IF EXISTS crunch_epa;
ALTER TABLE player_edge DROP COLUMN IF EXISTS garbage_epa;

-- 4. Add games_played (was approximated as plays_counted/15 before; now tracked directly)
ALTER TABLE player_edge ADD COLUMN IF NOT EXISTS games_played INTEGER;
