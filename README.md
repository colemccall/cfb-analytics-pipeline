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
│   ├── store.py      # local JSON read/write — the only persistence layer
│   ├── api_client.py # CFB Data API wrapper with .cache/ response caching
│   ├── matching.py   # name→player matching for scraped sources
│   └── json_utils.py # NaN-safe JSON writing
├── tests/            # pytest unit tests (no network required)
├── data/
│   ├── raw/          # harvest output, one file per entity (gitignored)
│   ├── computed/     # ratings engine output (gitignored)
│   └── coaching_changes_seed.csv
├── archive/sql/      # retired Supabase DDL — reference only, never executed
└── .cache/           # API response cache by MD5 hash (gitignored)
```

### Key architectural decisions

**Local JSON, no database.** Every script reads and writes plain JSON through `utils/store.py` (`read_raw`, `read_computed`, `write_computed`). The project previously ran on Supabase/PostgreSQL; that layer is fully retired, and the only credential required now is a CFB Data API key. The retired DDL lives in `archive/sql/` as field-level documentation, since the JSON files mirror those tables one-to-one.

**API response caching.** Every API response is cached as `.cache/{md5}.json`. Re-runs are near-instant. Never delete unless intentionally re-fetching.

**`player_seasons` is the join anchor.** One row per player × season × team. `stats`, `ratings`, and `player_edge` all join on `player_season_id`. Two players named "John Smith" at different schools are distinct `player_seasons` rows.

**Strict name matching for scraped sources.** `utils/matching.py` accepts a match only when the name is exact and the school agrees, or the name is exact and unique in our data (which covers players who transferred since their last season). A *fuzzy* name hit always requires school confirmation — "Chaden Sullivan" and "Caden Sullivan" are one edit apart and are different people. `resolve_collisions()` then unmatches rows where two scraped players claim the same player of ours.

**NaN never reaches disk.** `write_computed()` and `write_json()` both scrub NaN, which is invalid JSON and breaks the browser's `fetch().json()`. Note that `DataFrame.where(..., other=None)` does *not* do this on float columns — use the helpers.

**A season aggregate that is missing, or holds nothing but usage, is rebuilt from game rows.** `utils/stat_agg.py` owns the rule: `has_box_score()` decides whether a payload actually records production, `aggregate_game_stats()` sums the player's game rows into the season shape. Summing is only correct for counts — `LONG` is a maximum, rates are recomputed from totals rather than averaged, and the game shape's paired strings (`passingC/ATT` = `"25/38"`, `kickingFG` = `"2/3"`) have to be split before any of it works. Scripts 07, 12 and 15 all go through it; rebuilt payloads are marked `rebuilt_from_games` and carry no usage or PPA, because game rows never had them.

**Raw tables are read once per process.** `read_raw()` does not cache. Script 07's loader used to call it per position per season, re-parsing 255 MB of stats 228 times on a `--all-seasons` run; it now caches the tables and builds its stats index once. Any new per-position or per-season loader must do the same.

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

Copy `.env.example` to `.env` and add your API key — it is the only credential needed:

```text
CFB_API_KEY=...
```

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

08  EA CFB 27 ratings  (plain HTTP — no browser needed)
    python scripts/08_harvest_ea_cfb27_ratings.py

10  Team ratings  (requires script 07)
    python scripts/10_compute_team_ratings.py --season 2025

11  Engine B ratings — NIL + recruiting composite  (requires scripts 04, 02)
    python scripts/11_compute_engine_b_ratings.py --season 2025

12  Export static JSON for frontend  (requires scripts 07, 10)
    python scripts/12_export_frontend_json.py

13  Team performance vs. recruiting talent  (requires scripts 02, 07)
    python scripts/13_team_performance_evaluator.py

14  Recruiting class ROI / hit rate  (requires scripts 02, 07)
    python scripts/14_recruiting_roi.py

15  Engine D — next-season projection from career EDGE curves  (requires script 07)
    python scripts/15_predict_trajectories.py
    python scripts/15_predict_trajectories.py --retrain   # refit the model

16  Projected ratings for an unplayed season  (requires 15; writes engine='projected')
    python scripts/16_project_ratings.py --season 2026
```

Scripts 13–15 write straight into the frontend's `data/` directory; 12 writes
the core player/team/roster files. Run 12 before 13–15 on a full refresh, and
run it once more at the end — 15, 16 and the projected team ratings all land
after the first export.

**Not part of the chain:** `scripts/validate_ratings.py` writes nothing. It prints
the per-position distribution (n, mean, p50/p90/p99, how many clear 85 and 90) and
within-position Spearman against EA CFB 27 over matched players. Run it after any
change to 06 or 07 and read the table before shipping — distribution shape is a
hard gate on this project, and EA is a reference for "too generous / too stingy /
is the ceiling right", never a target.

```bash
python scripts/validate_ratings.py --season 2025
python scripts/validate_ratings.py --season 2026 --engine projected
```

### Bringing an unplayed season online

A season with no results still has rosters, and the app treats it as a first-class
projected season. Order matters — each step needs the one before it:

```bash
python scripts/01_harvest_games_players_stats.py --year 2026   # rosters (stats will be empty)
python scripts/02_harvest_recruiting.py --year 2026            # signing class
python scripts/03_harvest_transfers.py  --year 2026            # portal cycle
python scripts/15_predict_trajectories.py --retrain            # career-curve projections
python scripts/16_project_ratings.py --season 2026             # engine='projected' player ratings
python scripts/10_compute_team_ratings.py --season 2026 --engine projected
python scripts/12_export_frontend_json.py                      # exports + manifest.json
```

Script 10 needs `--engine projected` because SP+ and team stats cannot exist for a season
that has not been played; that path computes from the roster alone and calls no API.

After exporting, update the season constants in the app's `js/config.js`
(`LAST_PLAYED_SEASON`, `CURRENT_SEASON`, `PROJECTED_SEASONS`) to match the new
`manifest.json`. `pytest tests/test_export_contract.py` fails if they disagree.

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
| `ea_cfb27` | 08 | EA Sports CFB 27 ratings (2026 roster) |

---

## Data Model

Each entity is one JSON file (`data/raw/players.json`, `data/computed/ratings.json`, …),
loaded as a DataFrame by `utils/store.py`. Relationships are by id, joined in pandas:

```text
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
ea_ratings           — EA CFB 27 ratings, 54 attributes per player, linked to player_id
nil_valuations       — On3 NIL values (currently empty — On3 blocks scraping)
```

`stats.data` is a nested dict — keys follow CFB Data API naming (`passingYds`, `rushingCar`, `defensiveTot`, etc.).

`ratings.shap_values` is a JSON *string* (`json.dumps({})`), not a nested object.

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
| `archive/sql/` | Retired Supabase DDL, reference only | Yes |

---

## Documentation

| Document | What it covers |
|----------|----------------|
| [`docs/RATING_AND_PROJECTION_MODEL.md`](docs/RATING_AND_PROJECTION_MODEL.md) | How ratings and projections are built, **where they measurably fail**, and what EA CFB 27 could fix. Read this before changing either engine — it quantifies the OL problem (77% recruiting), the production-blind majority (67% of any roster), and the backup→starter blind spot (r = +0.10). Analysis only; nothing in it is implemented. |
| [`docs/INTERNAL_REFERENCE.md`](docs/INTERNAL_REFERENCE.md) | File inventory, the export→frontend contract, and known contract gaps. |
| [`docs/AUDIT_FINDINGS.md`](docs/AUDIT_FINDINGS.md) | Historical audit; §9 is the standing argument for absolute anchors over pool-relative scaling. |

---

## Notes

- Scripts 04 (On3 NIL) and 05 (ESPN coaching tracker) require Selenium + Chrome. Everything else runs over plain HTTP — script 08 reads EA's Next.js data route (`/_next/data/{buildId}/…/ratings.json?team=N`) directly, no browser needed.
- On3 actively blocks scraping, so `nil_valuations.json` is empty and Engine B (script 11) runs recruiting-only until a working NIL source lands.
- Script 01 with `--historical` fetches 2008–2020 data. Plays are the largest payload (~40k per season) and fetched last.
- If you add a new season, re-run scripts 01 → 06 → 07 → 10 → 12 in order.
- Transfer matching (script 03) requires `from_school` to match a `player_seasons` row — ambiguous name matches return `None` rather than guessing. Scraped-source matching (scripts 04, 08) uses the shared rules in `utils/matching.py`.
