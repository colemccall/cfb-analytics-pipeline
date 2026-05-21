-- Phase K: Team Ratings table.
-- Composite team rating per (team, season): SP+, roster avg, recruiting, sub-ratings.
-- Populated by scripts/10_compute_team_ratings.py.
-- Run in Supabase SQL Editor before running script 10.

CREATE TABLE IF NOT EXISTS team_ratings (
    id                 SERIAL PRIMARY KEY,
    team_id            INTEGER NOT NULL REFERENCES teams(id),
    season             INTEGER NOT NULL,
    overall_rating     NUMERIC(5,2),     -- 0-100 composite
    offense_rating     NUMERIC(5,2),     -- 0-100 offensive component
    defense_rating     NUMERIC(5,2),     -- 0-100 defensive component
    sp_overall         NUMERIC(6,2),     -- raw SP+ overall (signed)
    sp_offense         NUMERIC(6,2),     -- raw SP+ offense
    sp_defense         NUMERIC(6,2),     -- raw SP+ defense
    recruiting_score   NUMERIC(5,2),     -- 0-100 — 5-year rolling avg recruit composite
    avg_starter_rating NUMERIC(5,2),     -- avg of starter-tier player ratings
    starter_count      INTEGER,
    coaching_change    BOOLEAN DEFAULT FALSE,
    sub_ratings        JSONB,            -- {pace_score, redzone_off_eff, third_down_def_eff, ...}
    updated_at         TIMESTAMPTZ DEFAULT now(),
    UNIQUE (team_id, season)
);

CREATE INDEX IF NOT EXISTS idx_team_ratings_season
    ON team_ratings (season, overall_rating DESC);

ALTER TABLE team_ratings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Public read team_ratings" ON team_ratings;
CREATE POLICY "Public read team_ratings" ON team_ratings FOR SELECT USING (true);
