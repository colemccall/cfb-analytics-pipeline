-- CFB Analytics Platform — Supabase PostgreSQL Schema
-- Run this in the Supabase SQL Editor (project: cfb-analytics)
-- Tables are created in FK-dependency order.

-- ============================================================
-- 1. TEAMS
-- ============================================================
CREATE TABLE IF NOT EXISTS teams (
    id           SERIAL PRIMARY KEY,
    cfb_api_id   INTEGER UNIQUE,
    school       TEXT    NOT NULL UNIQUE,
    mascot       TEXT,
    abbreviation TEXT,
    conference   TEXT,
    division     TEXT,       -- 'fbs', 'fcs'
    color        TEXT,       -- primary hex color
    alt_color    TEXT,
    logo_url     TEXT,
    stadium_name TEXT,
    city         TEXT,
    state        TEXT,
    capacity     INTEGER,
    updated_at   TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- 2. PLAYERS  (depends on: teams)
-- ============================================================
CREATE TABLE IF NOT EXISTS players (
    id              SERIAL PRIMARY KEY,
    cfb_api_id      INTEGER UNIQUE,
    name            TEXT    NOT NULL,
    team_id         INTEGER REFERENCES teams(id),
    position        TEXT,   -- raw position string from API (QB, RB, WR, etc.)
    position_group  TEXT,   -- normalized group (QB, RB, WR, TE, OL, DL, LB, DB, K, P)
    year            INTEGER, -- 1=FR, 2=SO, 3=JR, 4=SR, 5=GR
    height_in       INTEGER,
    weight_lbs      INTEGER,
    hometown        TEXT,
    hometown_state  TEXT,
    hometown_country TEXT,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_players_position       ON players(position);
CREATE INDEX IF NOT EXISTS idx_players_position_group ON players(position_group);
CREATE INDEX IF NOT EXISTS idx_players_team           ON players(team_id);

-- ============================================================
-- 3. GAMES  (depends on: teams)
-- ============================================================
CREATE TABLE IF NOT EXISTS games (
    id              SERIAL PRIMARY KEY,
    cfb_api_id      INTEGER UNIQUE,
    season          INTEGER NOT NULL,
    week            INTEGER,
    season_type     TEXT DEFAULT 'regular',  -- 'regular', 'postseason'
    home_team_id    INTEGER REFERENCES teams(id),
    away_team_id    INTEGER REFERENCES teams(id),
    home_score      INTEGER,
    away_score      INTEGER,
    neutral_site    BOOLEAN DEFAULT false,
    game_date       DATE,
    venue           TEXT,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_games_season      ON games(season);
CREATE INDEX IF NOT EXISTS idx_games_home_team   ON games(home_team_id);
CREATE INDEX IF NOT EXISTS idx_games_away_team   ON games(away_team_id);

-- ============================================================
-- 4. STATS  (depends on: players, games)
-- Stores per-player, per-game, per-stat-type rows with JSONB data.
-- Also stores season-aggregate rows (game_id IS NULL, season IS NOT NULL).
-- ============================================================
CREATE TABLE IF NOT EXISTS stats (
    id          SERIAL PRIMARY KEY,
    player_id   INTEGER REFERENCES players(id),
    game_id     INTEGER REFERENCES games(id),   -- NULL for season-level aggregates
    season      INTEGER NOT NULL,
    stat_type   TEXT    NOT NULL,  -- 'passing', 'rushing', 'receiving', 'defensive', 'kicking', 'punting', 'ppa'
    data        JSONB   NOT NULL,  -- flexible: { yards, tds, attempts, ... }
    updated_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(player_id, game_id, season, stat_type)
);

CREATE INDEX IF NOT EXISTS idx_stats_player_season ON stats(player_id, season);
CREATE INDEX IF NOT EXISTS idx_stats_season        ON stats(season);
CREATE INDEX IF NOT EXISTS idx_stats_type          ON stats(stat_type);

-- ============================================================
-- 5. RECRUITING  (depends on: players)
-- ============================================================
CREATE TABLE IF NOT EXISTS recruiting (
    id              SERIAL PRIMARY KEY,
    player_id       INTEGER REFERENCES players(id),
    recruit_year    INTEGER NOT NULL,
    stars           INTEGER,    -- 1–5
    national_rank   INTEGER,
    position_rank   INTEGER,
    state_rank      INTEGER,
    composite_score NUMERIC(6,4),  -- 247 composite: 0.7000–1.0000
    committed_team_id INTEGER REFERENCES teams(id),
    source          TEXT DEFAULT '247sports',
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_recruiting_player ON recruiting(player_id);
CREATE INDEX IF NOT EXISTS idx_recruiting_year   ON recruiting(recruit_year);

-- ============================================================
-- 6. TRANSFERS  (depends on: players, teams)
-- ============================================================
CREATE TABLE IF NOT EXISTS transfers (
    id                  SERIAL PRIMARY KEY,
    player_id           INTEGER REFERENCES players(id),
    from_team_id        INTEGER REFERENCES teams(id),
    to_team_id          INTEGER REFERENCES teams(id),
    transfer_year       INTEGER NOT NULL,
    portal_date         DATE,
    portal_entry_count  INTEGER DEFAULT 1,  -- tracks serial transfers (RQ12)
    source              TEXT DEFAULT 'cfb_api',
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transfers_player ON transfers(player_id);
CREATE INDEX IF NOT EXISTS idx_transfers_year   ON transfers(transfer_year);

-- ============================================================
-- 7. NIL_VALUATIONS  (depends on: players)
-- ============================================================
CREATE TABLE IF NOT EXISTS nil_valuations (
    id             SERIAL PRIMARY KEY,
    player_id      INTEGER REFERENCES players(id),
    valuation_usd  INTEGER,
    source         TEXT DEFAULT 'on3',
    as_of_date     DATE,
    updated_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_nil_player ON nil_valuations(player_id);

-- ============================================================
-- 8. COACHING_CHANGES  (depends on: teams)
-- Supports RQ5 (scheme versatility) and RQ7 (OC portability).
-- ============================================================
CREATE TABLE IF NOT EXISTS coaching_changes (
    id            SERIAL PRIMARY KEY,
    team_id       INTEGER REFERENCES teams(id),
    coach_name    TEXT    NOT NULL,
    role          TEXT    NOT NULL,  -- 'HC', 'OC', 'DC', 'ST'
    start_season  INTEGER,
    end_season    INTEGER,           -- NULL means current/active
    prior_team    TEXT,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_coaching_team   ON coaching_changes(team_id);
CREATE INDEX IF NOT EXISTS idx_coaching_season ON coaching_changes(start_season, end_season);

-- ============================================================
-- 9. RATINGS  (depends on: players)
-- ML-generated output. One row per player per season.
-- ============================================================
CREATE TABLE IF NOT EXISTS ratings (
    id                    SERIAL PRIMARY KEY,
    player_id             INTEGER REFERENCES players(id),
    season                INTEGER NOT NULL,
    overall_rating        NUMERIC(5,2),  -- 0–100 XGBoost-derived
    position_rating       NUMERIC(5,2),  -- within-position rank normalized to 0–100
    trajectory_score      NUMERIC(5,2),  -- YoY change: positive = improving
    breakout_probability  NUMERIC(5,4),  -- 0.0000–1.0000
    shap_values           JSONB,         -- { "yards_per_carry": 0.34, "recruit_composite": -0.12, ... }
    model_version         TEXT,
    generated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE(player_id, season)
);

CREATE INDEX IF NOT EXISTS idx_ratings_overall  ON ratings(overall_rating DESC);
CREATE INDEX IF NOT EXISTS idx_ratings_season   ON ratings(season);
CREATE INDEX IF NOT EXISTS idx_ratings_player   ON ratings(player_id);

-- ============================================================
-- 10. RESEARCH_CACHE
-- Precomputed research findings for static JSON export.
-- ============================================================
CREATE TABLE IF NOT EXISTS research_cache (
    id            SERIAL PRIMARY KEY,
    research_key  TEXT UNIQUE NOT NULL,  -- e.g. 'bye_week_analysis_2021_2025'
    data          JSONB       NOT NULL,
    generated_at  TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- ROW LEVEL SECURITY
-- Enable RLS on all tables; allow anonymous SELECT on public-facing tables.
-- Run these after creating tables.
-- ============================================================

ALTER TABLE teams           ENABLE ROW LEVEL SECURITY;
ALTER TABLE players         ENABLE ROW LEVEL SECURITY;
ALTER TABLE games           ENABLE ROW LEVEL SECURITY;
ALTER TABLE stats           ENABLE ROW LEVEL SECURITY;
ALTER TABLE recruiting      ENABLE ROW LEVEL SECURITY;
ALTER TABLE transfers       ENABLE ROW LEVEL SECURITY;
ALTER TABLE nil_valuations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE coaching_changes ENABLE ROW LEVEL SECURITY;
ALTER TABLE ratings         ENABLE ROW LEVEL SECURITY;
ALTER TABLE research_cache  ENABLE ROW LEVEL SECURITY;

-- Public read policies (anon key can SELECT)
CREATE POLICY "Public read teams"      ON teams           FOR SELECT USING (true);
CREATE POLICY "Public read players"    ON players         FOR SELECT USING (true);
CREATE POLICY "Public read games"      ON games           FOR SELECT USING (true);
CREATE POLICY "Public read stats"      ON stats           FOR SELECT USING (true);
CREATE POLICY "Public read recruiting" ON recruiting      FOR SELECT USING (true);
CREATE POLICY "Public read transfers"  ON transfers       FOR SELECT USING (true);
CREATE POLICY "Public read nil"        ON nil_valuations  FOR SELECT USING (true);
CREATE POLICY "Public read coaching"   ON coaching_changes FOR SELECT USING (true);
CREATE POLICY "Public read ratings"    ON ratings         FOR SELECT USING (true);
CREATE POLICY "Public read research"   ON research_cache  FOR SELECT USING (true);
