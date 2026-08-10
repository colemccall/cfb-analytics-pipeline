-- CFB Analytics Platform — Schema v2
-- Introduces player_seasons as the atomic unit (player × season × team).
-- Run in Supabase SQL Editor AFTER running migrate_v1_to_v2.sql.
--
-- Key change from v1:
--   players   → identity only (no team_id, no year)
--   player_seasons → one row per player × season × team (new join anchor)
--   stats, ratings, player_edge, plays → reference player_season_id
--   recruiting, transfers → still reference player_id (career-level data)

-- ============================================================
-- 1. TEAMS  (unchanged)
-- ============================================================
CREATE TABLE IF NOT EXISTS teams (
    id           SERIAL PRIMARY KEY,
    cfb_api_id   INTEGER UNIQUE,
    school       TEXT    NOT NULL UNIQUE,
    mascot       TEXT,
    abbreviation TEXT,
    conference   TEXT,
    division     TEXT,
    color        TEXT,
    alt_color    TEXT,
    logo_url     TEXT,
    stadium_name TEXT,
    city         TEXT,
    state        TEXT,
    capacity     INTEGER,
    updated_at   TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- 2. PLAYERS  — identity anchor only
-- ============================================================
CREATE TABLE IF NOT EXISTS players (
    id               SERIAL PRIMARY KEY,
    cfb_api_id       INTEGER UNIQUE,
    name             TEXT    NOT NULL,
    -- physical/bio — set from most recent roster, rarely changes
    height_in        INTEGER,
    weight_lbs       INTEGER,
    hometown         TEXT,
    hometown_state   TEXT,
    hometown_country TEXT,
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_players_name ON players(name);

-- ============================================================
-- 3. PLAYER_SEASONS  — one row per player × season × team
-- This is the join anchor for stats, ratings, player_edge.
-- ============================================================
CREATE TABLE IF NOT EXISTS player_seasons (
    id              SERIAL PRIMARY KEY,
    player_id       INTEGER NOT NULL REFERENCES players(id),
    season          INTEGER NOT NULL,
    team_id         INTEGER REFERENCES teams(id),
    position        TEXT,           -- raw position string from API
    position_group  TEXT,           -- normalized canonical group
    year            INTEGER,        -- 1=FR, 2=SO, 3=JR, 4=SR, 5=GR
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (player_id, season, team_id)
);

CREATE INDEX IF NOT EXISTS idx_ps_player          ON player_seasons(player_id);
CREATE INDEX IF NOT EXISTS idx_ps_season          ON player_seasons(season);
CREATE INDEX IF NOT EXISTS idx_ps_team            ON player_seasons(team_id);
CREATE INDEX IF NOT EXISTS idx_ps_position_group  ON player_seasons(position_group, season);

-- ============================================================
-- 4. GAMES  (unchanged — depends on teams)
-- ============================================================
CREATE TABLE IF NOT EXISTS games (
    id              SERIAL PRIMARY KEY,
    cfb_api_id      INTEGER UNIQUE,
    season          INTEGER NOT NULL,
    week            INTEGER,
    season_type     TEXT DEFAULT 'regular',
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
-- 5. STATS  — references player_season_id
-- ============================================================
CREATE TABLE IF NOT EXISTS stats (
    id                SERIAL PRIMARY KEY,
    player_season_id  INTEGER NOT NULL REFERENCES player_seasons(id),
    game_id           INTEGER REFERENCES games(id),   -- NULL for season aggregates
    season            INTEGER NOT NULL,               -- denormalized for fast filtering
    stat_type         TEXT    NOT NULL,
    data              JSONB   NOT NULL,
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- Partial unique indexes (same pattern as v1, now on player_season_id)
CREATE UNIQUE INDEX IF NOT EXISTS uq_stats_season_agg
    ON stats (player_season_id, season, stat_type)
    WHERE game_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_stats_game
    ON stats (player_season_id, game_id, season, stat_type)
    WHERE game_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_stats_ps_season ON stats(player_season_id, season);
CREATE INDEX IF NOT EXISTS idx_stats_season    ON stats(season);
CREATE INDEX IF NOT EXISTS idx_stats_type      ON stats(stat_type);

-- ============================================================
-- 6. RECRUITING  — stays on player_id (recruit class = career identity)
-- ============================================================
CREATE TABLE IF NOT EXISTS recruiting (
    id                 SERIAL PRIMARY KEY,
    player_id          INTEGER REFERENCES players(id),
    recruit_year       INTEGER NOT NULL,
    stars              INTEGER,
    national_rank      INTEGER,
    position_rank      INTEGER,
    state_rank         INTEGER,
    composite_score    NUMERIC(6,4),
    committed_team_id  INTEGER REFERENCES teams(id),
    source             TEXT DEFAULT '247sports',
    updated_at         TIMESTAMPTZ DEFAULT now(),
    UNIQUE (player_id, recruit_year, source)
);

CREATE INDEX IF NOT EXISTS idx_recruiting_player ON recruiting(player_id);
CREATE INDEX IF NOT EXISTS idx_recruiting_year   ON recruiting(recruit_year);

-- ============================================================
-- 7. TRANSFERS  — stays on player_id + season (career-level event)
-- ============================================================
CREATE TABLE IF NOT EXISTS transfers (
    id                  SERIAL PRIMARY KEY,
    player_id           INTEGER REFERENCES players(id),
    from_team_id        INTEGER REFERENCES teams(id),
    to_team_id          INTEGER REFERENCES teams(id),
    transfer_year       INTEGER NOT NULL,
    portal_date         DATE,
    portal_entry_count  INTEGER DEFAULT 1,
    source              TEXT DEFAULT 'cfb_api',
    updated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (player_id, transfer_year, from_team_id)
);

CREATE INDEX IF NOT EXISTS idx_transfers_player ON transfers(player_id);
CREATE INDEX IF NOT EXISTS idx_transfers_year   ON transfers(transfer_year);

-- ============================================================
-- 8. NIL_VALUATIONS  — stays on player_id
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
-- 9. COACHING_CHANGES  (unchanged)
-- ============================================================
CREATE TABLE IF NOT EXISTS coaching_changes (
    id            SERIAL PRIMARY KEY,
    team_id       INTEGER REFERENCES teams(id),
    coach_name    TEXT    NOT NULL,
    role          TEXT    NOT NULL,
    start_season  INTEGER,
    end_season    INTEGER,
    prior_team    TEXT,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_coaching_team   ON coaching_changes(team_id);
CREATE INDEX IF NOT EXISTS idx_coaching_season ON coaching_changes(start_season, end_season);

-- ============================================================
-- 10. RATINGS  — one row per player_season_id
-- ============================================================
CREATE TABLE IF NOT EXISTS ratings (
    id                    SERIAL PRIMARY KEY,
    player_season_id      INTEGER NOT NULL REFERENCES player_seasons(id),
    season                INTEGER NOT NULL,               -- denormalized
    overall_rating        NUMERIC(5,2),
    position_rating       NUMERIC(5,2),
    trajectory_score      NUMERIC(5,2),
    breakout_probability  NUMERIC(5,4),
    shap_values           JSONB,
    model_version         TEXT,
    generated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (player_season_id)
);

CREATE INDEX IF NOT EXISTS idx_ratings_overall   ON ratings(overall_rating DESC);
CREATE INDEX IF NOT EXISTS idx_ratings_season    ON ratings(season);
CREATE INDEX IF NOT EXISTS idx_ratings_ps        ON ratings(player_season_id);

-- ============================================================
-- 11. PLAYS  — player attribution via player_id (name-matched at ingest)
-- Still references player_id directly — attribution is a best-effort
-- name match at ingest time and does not require season context.
-- ============================================================
CREATE TABLE IF NOT EXISTS plays (
    id              SERIAL PRIMARY KEY,
    cfb_api_id      BIGINT UNIQUE,
    game_id         INTEGER REFERENCES games(id),
    season          INTEGER NOT NULL,
    week            INTEGER,
    offense_team_id INTEGER REFERENCES teams(id),
    defense_team_id INTEGER REFERENCES teams(id),
    period          INTEGER,
    clock_seconds   INTEGER,
    down            INTEGER,
    distance        INTEGER,
    yards_to_goal   INTEGER,
    home_score      INTEGER,
    away_score      INTEGER,
    offense_score   INTEGER,
    defense_score   INTEGER,
    play_type       TEXT,
    yards_gained    INTEGER,
    epa             NUMERIC(8,4),
    ppa             NUMERIC(8,4),
    passer_player_id   INTEGER REFERENCES players(id),
    rusher_player_id   INTEGER REFERENCES players(id),
    receiver_player_id INTEGER REFERENCES players(id),
    defender_player_id INTEGER REFERENCES players(id),
    play_text       TEXT,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_plays_game     ON plays(game_id);
CREATE INDEX IF NOT EXISTS idx_plays_season   ON plays(season);
CREATE INDEX IF NOT EXISTS idx_plays_offense  ON plays(offense_team_id, season);
CREATE INDEX IF NOT EXISTS idx_plays_passer   ON plays(passer_player_id, season);
CREATE INDEX IF NOT EXISTS idx_plays_rusher   ON plays(rusher_player_id, season);
CREATE INDEX IF NOT EXISTS idx_plays_receiver ON plays(receiver_player_id, season);

-- ============================================================
-- 12. PLAYER_EDGE  — one row per player_season_id
-- ============================================================
CREATE TABLE IF NOT EXISTS player_edge (
    id              SERIAL PRIMARY KEY,
    player_season_id INTEGER NOT NULL REFERENCES player_seasons(id),
    season          INTEGER NOT NULL,               -- denormalized
    edge_score      NUMERIC(8,4),
    edge_scaled     NUMERIC(5,2),
    crunch_epa      NUMERIC(8,4),
    garbage_epa     NUMERIC(8,4),
    plays_counted   INTEGER,
    opponent_avg_sp NUMERIC(6,2),
    model_version   TEXT,
    generated_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (player_season_id)
);

CREATE INDEX IF NOT EXISTS idx_edge_ps     ON player_edge(player_season_id);
CREATE INDEX IF NOT EXISTS idx_edge_season ON player_edge(season);

-- ============================================================
-- 13. EA_RATINGS  (unchanged — references player_id)
-- ============================================================
CREATE TABLE IF NOT EXISTS ea_ratings (
    id          BIGSERIAL PRIMARY KEY,
    player_id   BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    source      TEXT   NOT NULL,
    ea_season   INT    NOT NULL,
    ovr         INT,
    position    TEXT,
    attributes  JSONB  DEFAULT '{}',
    scraped_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (player_id, source, ea_season)
);

CREATE INDEX IF NOT EXISTS idx_ea_ratings_player ON ea_ratings(player_id);
CREATE INDEX IF NOT EXISTS idx_ea_ratings_source ON ea_ratings(source, ea_season);

-- ============================================================
-- 14. RESEARCH_CACHE  (unchanged)
-- ============================================================
CREATE TABLE IF NOT EXISTS research_cache (
    id            SERIAL PRIMARY KEY,
    research_key  TEXT UNIQUE NOT NULL,
    data          JSONB       NOT NULL,
    generated_at  TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- ROW LEVEL SECURITY
-- ============================================================
ALTER TABLE teams            ENABLE ROW LEVEL SECURITY;
ALTER TABLE players          ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_seasons   ENABLE ROW LEVEL SECURITY;
ALTER TABLE games            ENABLE ROW LEVEL SECURITY;
ALTER TABLE stats            ENABLE ROW LEVEL SECURITY;
ALTER TABLE recruiting       ENABLE ROW LEVEL SECURITY;
ALTER TABLE transfers        ENABLE ROW LEVEL SECURITY;
ALTER TABLE nil_valuations   ENABLE ROW LEVEL SECURITY;
ALTER TABLE coaching_changes ENABLE ROW LEVEL SECURITY;
ALTER TABLE ratings          ENABLE ROW LEVEL SECURITY;
ALTER TABLE plays            ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_edge      ENABLE ROW LEVEL SECURITY;
ALTER TABLE ea_ratings       ENABLE ROW LEVEL SECURITY;
ALTER TABLE research_cache   ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public read teams"           ON teams            FOR SELECT USING (true);
CREATE POLICY "Public read players"         ON players          FOR SELECT USING (true);
CREATE POLICY "Public read player_seasons"  ON player_seasons   FOR SELECT USING (true);
CREATE POLICY "Public read games"           ON games            FOR SELECT USING (true);
CREATE POLICY "Public read stats"           ON stats            FOR SELECT USING (true);
CREATE POLICY "Public read recruiting"      ON recruiting       FOR SELECT USING (true);
CREATE POLICY "Public read transfers"       ON transfers        FOR SELECT USING (true);
CREATE POLICY "Public read nil"             ON nil_valuations   FOR SELECT USING (true);
CREATE POLICY "Public read coaching"        ON coaching_changes FOR SELECT USING (true);
CREATE POLICY "Public read ratings"         ON ratings          FOR SELECT USING (true);
CREATE POLICY "Public read plays"           ON plays            FOR SELECT USING (true);
CREATE POLICY "Public read edge"            ON player_edge      FOR SELECT USING (true);
CREATE POLICY "Public read ea_ratings"      ON ea_ratings       FOR SELECT USING (true);
CREATE POLICY "Public read research"        ON research_cache   FOR SELECT USING (true);
