-- migrate_v5_fixes.sql
-- Run once in Supabase SQL Editor before executing any pipeline scripts.
-- Safe to re-run (all statements use IF NOT EXISTS / IF EXISTS guards).

-- ── player_edge: add player_season_id + columns script 08 expects ──────────
ALTER TABLE player_edge ADD COLUMN IF NOT EXISTS player_season_id INTEGER REFERENCES player_seasons(id);
ALTER TABLE player_edge ADD COLUMN IF NOT EXISTS stats_measured INTEGER;
ALTER TABLE player_edge ADD COLUMN IF NOT EXISTS games_played INTEGER;
ALTER TABLE player_edge DROP CONSTRAINT IF EXISTS player_edge_player_id_season_key;
ALTER TABLE player_edge ADD CONSTRAINT IF NOT EXISTS player_edge_ps_season_key UNIQUE (player_season_id, season);
CREATE INDEX IF NOT EXISTS idx_edge_ps ON player_edge(player_season_id, season);

-- ── team_ratings: add columns script 10 writes ─────────────────────────────
ALTER TABLE team_ratings ADD COLUMN IF NOT EXISTS offense_rating     NUMERIC(5,2);
ALTER TABLE team_ratings ADD COLUMN IF NOT EXISTS defense_rating     NUMERIC(5,2);
ALTER TABLE team_ratings ADD COLUMN IF NOT EXISTS sp_overall         NUMERIC(6,2);
ALTER TABLE team_ratings ADD COLUMN IF NOT EXISTS sp_offense         NUMERIC(6,2);
ALTER TABLE team_ratings ADD COLUMN IF NOT EXISTS sp_defense         NUMERIC(6,2);
ALTER TABLE team_ratings ADD COLUMN IF NOT EXISTS recruiting_score   NUMERIC(6,2);
ALTER TABLE team_ratings ADD COLUMN IF NOT EXISTS avg_starter_rating NUMERIC(5,2);
ALTER TABLE team_ratings ADD COLUMN IF NOT EXISTS starter_count      INTEGER;
ALTER TABLE team_ratings ADD COLUMN IF NOT EXISTS coaching_change    BOOLEAN DEFAULT false;
ALTER TABLE team_ratings ADD COLUMN IF NOT EXISTS sub_ratings        JSONB;
