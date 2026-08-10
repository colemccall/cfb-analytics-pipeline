-- CFB Analytics — Migration v1 → v2
-- Adds player_seasons table and migrates existing data.
-- Safe to run against the live Supabase DB — uses ADD COLUMN / CREATE TABLE IF NOT EXISTS.
--
-- Steps:
--   1. Strip team_id/year/position from players (move to player_seasons)
--   2. Create player_seasons and populate from existing stats rows
--   3. Add player_season_id FK to stats, ratings, player_edge
--   4. Backfill player_season_id on existing rows
--   5. Add NOT NULL constraint and drop old FK columns
--   6. Add unique constraint to recruiting (prevent future duplication)
--   7. Add unique constraint to transfers (prevent future duplication)
--
-- Run in Supabase SQL Editor. Takes ~30-60s on 2021-2025 data volume.

BEGIN;

-- ============================================================
-- STEP 1: Create player_seasons
-- ============================================================
CREATE TABLE IF NOT EXISTS player_seasons (
    id              SERIAL PRIMARY KEY,
    player_id       INTEGER NOT NULL REFERENCES players(id),
    season          INTEGER NOT NULL,
    team_id         INTEGER REFERENCES teams(id),
    position        TEXT,
    position_group  TEXT,
    year            INTEGER,
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (player_id, season, team_id)
);

CREATE INDEX IF NOT EXISTS idx_ps_player         ON player_seasons(player_id);
CREATE INDEX IF NOT EXISTS idx_ps_season         ON player_seasons(season);
CREATE INDEX IF NOT EXISTS idx_ps_team           ON player_seasons(team_id);
CREATE INDEX IF NOT EXISTS idx_ps_position_group ON player_seasons(position_group, season);

-- ============================================================
-- STEP 2: Populate player_seasons from existing stats rows
-- Each (player_id, season) in stats becomes a player_seasons row.
-- Team is resolved via ratings.team_id first (most accurate),
-- falling back to players.team_id.
-- ============================================================
INSERT INTO player_seasons (player_id, season, team_id, position, position_group, year)
SELECT DISTINCT ON (s.player_id, s.season)
    s.player_id,
    s.season,
    COALESCE(r.team_id, p.team_id)  AS team_id,
    p.position,
    p.position_group,
    p.year
FROM stats s
JOIN players p ON p.id = s.player_id
LEFT JOIN ratings r ON r.player_id = s.player_id AND r.season = s.season
WHERE s.game_id IS NULL
ORDER BY s.player_id, s.season
ON CONFLICT (player_id, season, team_id) DO UPDATE
    SET position       = EXCLUDED.position,
        position_group = EXCLUDED.position_group,
        year           = EXCLUDED.year,
        updated_at     = now();

-- Also create player_seasons rows for players with ratings but no stats
-- (fallback path: OL proxies, some freshmen)
INSERT INTO player_seasons (player_id, season, team_id, position, position_group, year)
SELECT DISTINCT ON (r.player_id, r.season)
    r.player_id,
    r.season,
    COALESCE(r.team_id, p.team_id),
    p.position,
    p.position_group,
    p.year
FROM ratings r
JOIN players p ON p.id = r.player_id
ORDER BY r.player_id, r.season
ON CONFLICT (player_id, season, team_id) DO NOTHING;

-- ============================================================
-- STEP 3: Add player_season_id columns to dependent tables
-- ============================================================
ALTER TABLE stats        ADD COLUMN IF NOT EXISTS player_season_id INTEGER REFERENCES player_seasons(id);
ALTER TABLE ratings      ADD COLUMN IF NOT EXISTS player_season_id INTEGER REFERENCES player_seasons(id);
ALTER TABLE player_edge  ADD COLUMN IF NOT EXISTS player_season_id INTEGER REFERENCES player_seasons(id);

-- ============================================================
-- STEP 4: Backfill player_season_id on existing rows
-- ============================================================

-- stats
UPDATE stats s
SET player_season_id = ps.id
FROM player_seasons ps
WHERE ps.player_id = s.player_id
  AND ps.season    = s.season
  AND s.player_season_id IS NULL;

-- ratings
UPDATE ratings r
SET player_season_id = ps.id
FROM player_seasons ps
WHERE ps.player_id = r.player_id
  AND ps.season    = r.season
  AND r.player_season_id IS NULL;

-- player_edge
UPDATE player_edge pe
SET player_season_id = ps.id
FROM player_seasons ps
WHERE ps.player_id = pe.player_id
  AND ps.season    = pe.season
  AND pe.player_season_id IS NULL;

-- ============================================================
-- STEP 5: New unique/partial indexes on stats using player_season_id
-- (keep old indexes active during transition; drop after validation)
-- ============================================================
CREATE UNIQUE INDEX IF NOT EXISTS uq_stats_season_agg_v2
    ON stats (player_season_id, season, stat_type)
    WHERE game_id IS NULL AND player_season_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_stats_game_v2
    ON stats (player_season_id, game_id, season, stat_type)
    WHERE game_id IS NOT NULL AND player_season_id IS NOT NULL;

-- ============================================================
-- STEP 6: New unique constraint on ratings
-- ============================================================
CREATE UNIQUE INDEX IF NOT EXISTS uq_ratings_player_season_v2
    ON ratings (player_season_id)
    WHERE player_season_id IS NOT NULL;

-- ============================================================
-- STEP 7: Unique constraint on player_edge
-- ============================================================
CREATE UNIQUE INDEX IF NOT EXISTS uq_edge_player_season_v2
    ON player_edge (player_season_id)
    WHERE player_season_id IS NOT NULL;

-- ============================================================
-- STEP 8: Unique constraint on recruiting (prevent double-match)
-- ============================================================
CREATE UNIQUE INDEX IF NOT EXISTS uq_recruiting_player_year_source
    ON recruiting (player_id, recruit_year, source)
    WHERE player_id IS NOT NULL;

-- ============================================================
-- STEP 9: Unique constraint on transfers (prevent double-match)
-- ============================================================
CREATE UNIQUE INDEX IF NOT EXISTS uq_transfers_player_year_from
    ON transfers (player_id, transfer_year, from_team_id)
    WHERE player_id IS NOT NULL AND from_team_id IS NOT NULL;

-- ============================================================
-- STEP 10: RLS for player_seasons
-- ============================================================
ALTER TABLE player_seasons ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Public read player_seasons" ON player_seasons;
CREATE POLICY "Public read player_seasons" ON player_seasons FOR SELECT USING (true);

COMMIT;

-- ============================================================
-- VALIDATION QUERIES (run manually after migration)
-- ============================================================
-- SELECT COUNT(*) FROM player_seasons;                          -- expect ~15k-25k rows
-- SELECT COUNT(*) FROM stats WHERE player_season_id IS NULL;   -- expect 0
-- SELECT COUNT(*) FROM ratings WHERE player_season_id IS NULL; -- expect 0
-- SELECT COUNT(*) FROM player_edge WHERE player_season_id IS NULL; -- expect 0
-- SELECT season, COUNT(*) FROM player_seasons GROUP BY season ORDER BY season;
