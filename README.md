# CFB Analytics Pipeline

Python ETL and ratings engine for the [CFB Analytics Platform](https://github.com/colemccall/cfb-analytics-app). Fetches data from the CFB Data API and 247Sports, computes opponent-adjusted EDGE scores, and exports static JSON for the frontend.

---

## What This Does

The pipeline turns raw college football data into meaningful player and team ratings:

1. **Fetch** — pulls games, rosters, stats, play-by-play, recruiting rankings, transfer portal entries, and coaching changes from the CFB Data API
2. **Score** — computes EDGE (opponent-adjusted per-game production) for every player with qualifying stats
3. **Rate** — maps EDGE scores to 0–99 OVR via fixed position-specific piecewise linear anchors
4. **Export** — writes static JSON files consumed directly by the frontend (no server required)

---

## Architecture

```text
cfb-analytics-pipeline/
├── scripts/          # ETL and compute scripts (run in order)
├── utils/
│   ├── db.py         # psycopg2 connection with auto-detect (direct + pooler)
│   ├── api_client.py # CFB Data API wrapper with .cache/ response caching
│   └── store.py      # bulk_upsert helper with ON CONFLICT DO UPDATE
├── tests/            # pytest unit tests (no DB or API required)
├── data/
│   ├── raw/          # API harvest output (gitignored)
│   ├── computed/     # ratings engine output (gitignored)
│   └── coaching_changes_seed.csv
├── sql/              # schema definitions and migrations
└── .cache/           # API response cache by MD5 hash (gitignored)
```

### Key architectural decisions

**psycopg2 only — never Supabase REST for bulk reads.** The REST API has a hard 1,000-row limit. All reads use `utils/db.get_connection()`. The Supabase client in `utils/supabase_client.py` is a reference-only fallback.

**Auto-detecting DB connection.** `utils/db._get_working_url()` tries `DATABASE_URL` (direct/IPv6) first, then `DATABASE_URL_POOLER` (Session Pooler/IPv4), caching the working one for the process lifetime.

**API response caching.** Every API response is cached as `.cache/{md5}.json`. Re-runs are near-instant. Never delete unless intentionally re-fetching.

**`player_seasons` is the join anchor.** One row per player × season × team. `stats`, `ratings`, and `player_edge` all join on `player_season_id`. Two players named "John Smith" at different schools are distinct `player_seasons` rows.

**Dedup before every bulk upsert.** `bulk_upsert()` uses `ON CONFLICT DO UPDATE`, which raises `CardinalityViolation` if the same conflict key appears twice in one batch. Always dedup before calling.

---

## Setup

```powershell
# Create and activate virtual environment (Windows)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# On corporate/restricted networks with SSL inspection:
pip install -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org
```

Copy `.env.example` to `.env` and fill in your credentials:

```text
CFB_API_KEY=...
DATABASE_URL=postgresql://postgres:...@db.<project>.supabase.co:5432/postgres
DATABASE_URL_POOLER=postgresql://postgres.<project>:...@aws-1-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_KEY=...
```

> **Note:** Special characters in passwords must be URL-encoded in both connection strings (`*` → `%2A`, `%` → `%25`, `+` → `%2B`, `/` → `%2F`).

---

## Script Run Order

Scripts are numbered by their dependency chain. Run them in this order for a complete data refresh:

```text
01  Teams, players, player_seasons, games, stats, transfers, plays
    python scripts/01_harvest_games_players_stats.py
    python scripts/01_harvest_games_players_stats.py --historical   # 2008–2020 backfill

02  Recruiting rankings (247Sports composite)
    python scripts/02_harvest_recruiting.py

03  Transfer portal entries with player linkage
    python scripts/03_harvest_transfers.py

04  NIL valuations from On3  (requires Selenium + Chrome)
    python scripts/04_harvest_nil_valuations.py

05  Coaching changes (seed CSV + ESPN live tracker)
    python scripts/05_load_coaching_changes.py --csv-only

06  EDGE scores per player-season  (requires plays from script 01)
    python scripts/06_compute_edge_scores.py --season 2025

07  Player ratings — Engine A  (requires EDGE from script 06)
    python scripts/07_compute_player_ratings.py --season 2025

08  EA CFB 25 ratings  (requires Selenium)
    python scripts/08_harvest_ea_cfb25_ratings.py

08b EA CFB 26 ratings  (requires Selenium)
    python scripts/08b_harvest_ea_cfb26_ratings.py

10  Team ratings  (requires script 07)
    python scripts/10_compute_team_ratings.py --season 2025

11  Engine B ratings — NIL + recruiting composite  (requires scripts 04, 02)
    python scripts/11_compute_engine_b_ratings.py --season 2025

12  Export static JSON for frontend  (requires scripts 07, 10)
    python scripts/12_export_frontend_json.py
```

---

## The EDGE Rating System

### Core formula

```text
EDGE = Σ (stat_composite_i × opp_mult_i) / √(games_played)
```

Where `i` iterates over each game the player appeared in with recorded stats.

**Stat composite** — a position-specific weighted sum of per-game stats prioritizing impactful plays over volume. Example for QB:

```text
composite = passYds×1.0 + passTD×25 + rushYds×0.7 + rushTD×20 − INT×20
```

**Opponent multiplier** — scales each game's composite by the opponent's SP+ rating (defensive SP+ for offensive players, offensive SP+ for defenders), normalized to `[0.55, 1.45]`. A dominant performance against a top-10 defense counts up to 1.45×; the same output against a weak defense as low as 0.55×.

**Sample size normalization** — divides by √(games_played) to reward sustained production without catastrophically penalizing injury-shortened seasons.

### EDGE → OVR mapping

EDGE scores map to 0–99 Overall Ratings via fixed **position-specific piecewise linear anchors** calibrated from reference players. The anchors don't shift year to year — there's no forced distribution curve. If no one hits the 99 threshold in a season, no one rates 99.

### Era buckets

Three EDGE anchor sets handle the historical stat coverage gap for pre-2016 defenders:

| Era | Seasons | Coverage |
|-----|---------|----------|
| Modern | 2018–present | Full: tackles, sacks, TFLs, hurries, PBUs |
| Transition | 2013–2017 | Partial: most counting stats available |
| Classic | 2008–2012 | INTs only for defenders + recruiting composite blend |

Pre-2016 DL/EDGE ratings are recruiting-caliber estimates — the CFB Data API does not include individual defensive stats (sacks, TFLs, hurries) for those seasons.

### Playing-time tiers

Stats reliability scales with sample size. Four tiers control how much EDGE production vs. recruiting composite drives the rating:

| Tier | Mix | Applies when |
|------|-----|-------------|
| Starter | 100% EDGE | Crossed position-specific qualifying threshold |
| Role player | 75% EDGE + 25% recruiting | Regular contributor, limited sample |
| Reserve | 40% EDGE + 60% recruiting | Spot duty |
| Bench / Redshirt | 100% recruiting composite | No qualifying production |

### Multiple engines

The `ratings.engine` column supports multiple rating systems per player-season:

| Engine | Script | Method |
|--------|--------|--------|
| `edge` | 07 | EDGE formula with opponent adjustment |
| `engine_b` | 11 | 60% recruiting + 40% NIL valuation |
| `ea_cfb25` | 08 | EA Sports CFB 25 scraped ratings |
| `ea_cfb26` | 08b | EA Sports CFB 26 scraped ratings |

---

## Database Schema Highlights

All tables live in Supabase (PostgreSQL). Key relationships:

```
players              — identity only (no team, no year, no position)
  └── player_seasons — one row per player × season × team (the join anchor)
        ├── stats          — JSONB per game and season aggregate
        ├── player_edge    — EDGE score and scaled EDGE per player-season
        └── ratings        — OVR, trajectory, SHAP values, engine tag
recruiting           — references player_id (career-level, not seasonal)
transfers            — references player_id; team-gated fuzzy name matching
plays                — raw play-by-play; player attribution via passer/rusher/receiver/defender IDs
team_ratings         — OVR + sub-scores (pass_off, run_off, pass_def, run_def, special_teams)
coaching_changes     — HC/OC/DC/ST roles with from/to school and year
```

`stats.data` is JSONB — keys follow CFB Data API naming (`passingYds`, `rushingCar`, `defensiveTot`, etc.).

`ratings.shap_values` is JSONB stored as a JSON string (`json.dumps({})`).

---

## Tests

```powershell
pytest tests/ -v
```

41 tests across 4 files — all unit tests with no database or API calls required:

| File | Coverage |
|------|----------|
| `test_edge_anchors.py` | EDGE → OVR piecewise linear mapping for all position groups |
| `test_playtime_tier.py` | Playtime tier classification logic |
| `test_composite_to_100.py` | Recruiting composite normalization |
| `test_clean_nan.py` | NaN/None cleaning for JSON export |

---

## Data Directories

| Path | Contents | Tracked |
|------|----------|---------|
| `data/raw/` | API harvest output (JSON per endpoint per season) | No |
| `data/computed/` | Ratings engine output | No |
| `data/coaching_changes_seed.csv` | Historical coaching data seed file | Yes |
| `.cache/` | API response cache keyed by MD5 hash | No |
| `sql/` | Schema definitions and migration scripts | Yes |

---

## Notes

- Script 04 (NIL) and scripts 08/08b (EA ratings) require Selenium + Chrome. All other scripts are headless.
- Script 01 with `--historical` fetches 2008–2020 data. Plays are the largest payload (~40k per season) and fetched last.
- If you add a new season, re-run scripts 01 → 06 → 07 → 10 → 12 in order.
- Transfer matching (script 03) requires `from_school` to match a `player_seasons` row — ambiguous name matches return `None` rather than guessing.
