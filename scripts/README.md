# scripts/

Every script that harvests, rates, exports or checks. They are numbered in the order data flows through them;
the number is the run order, not a version. Each script's docstring is the authority on what it does — this page
is the map.

The whole flow, from the API to the pages, is in [`../docs/DATA_FLOW.md`](../docs/DATA_FLOW.md).

## Harvest — the API into `data/raw/`

| Script | Writes | Notes |
|---|---|---|
| `01_harvest_games_players_stats.py` | teams, players, player_seasons, games, stats | A whole season end to end (~400 requests). |
| `01b_refresh_in_season.py` | the same files, the API cache | A season being played: the newest week and the season totals only (~12 requests). Stage, validate, merge. |
| `02_harvest_recruiting.py` | recruiting | |
| `03_harvest_transfers.py` | transfers | |
| `04_harvest_nil_valuations.py` | nil_valuations | No working source; the file is `[]`. |
| `05_load_coaching_changes.py` | coaching_changes | Needs `data/coaching_changes_seed.csv` (gitignored; not in a fresh clone). |
| `09_harvest_supplemental.py` | team_advanced_season, team_records, coaches, draft_picks, … | `--dataset NAME --seasons Y`. A season it re-harvests is REPLACED. |
| `19_harvest_headshots.py`, `20_harvest_backdrops.py` | app images metadata | Player headshots (ESPN), menu backdrops (Wikimedia Commons). |

## Rate — `data/raw/` into `data/computed/` (a full recompute runs them in this order)

`06 → 07 → 10 → 11 → 15 → 16 → 10 (projected) → 13 → 14 → 12`

| Script | What it computes |
|---|---|
| `06_compute_edge_scores.py` | Player EDGE: each game's production, opponent-adjusted, summed and divided by √games (`player_edge.json`, `player_game_scores.json`). `--in-progress` rescales a part-season to full-season pace; `--through-week N` cuts the season at week N. |
| `07_compute_player_ratings.py` | Player ratings (engine `edge`): EDGE through the frozen per-position anchors, tiers, the low-game rule (`ratings.json`). |
| `10_compute_team_ratings.py` | Team ratings: roster, stats and results readings and the power ranking (`team_ratings.json`). |
| `11_compute_engine_b_ratings.py` | Engine B (NIL + recruiting). Runs recruiting-only: the NIL source is empty. Not read by the app. |
| `13_team_performance_evaluator.py`, `14_recruiting_roi.py` | Research tables behind the findings pages. |
| `15_predict_trajectories.py`, `16_project_ratings.py` | Engine D next-season projections and the `projected` engine for an unplayed season. |

## A season being played — always in a private copy of the data

| Script | What it writes |
|---|---|
| `17_live_season.py` | `live_{season}.json` and `live_roster_{season}.json`: earned-to-date ratings. Runs 06 → 07 → 10 in a temp copy (`CFB_DATA_DIR`), so nothing partial reaches the files script 15 trains on. `--offline` after a 01b refresh. |
| `17b_weekly_progression.py` | `weekly_teams_{season}.json`, `weekly_players_{season}.json`: the same chain re-run as the season stood after each week. Always rebuilds every week (`--weeks` overwrites, it does not merge). Offline by design. |

## Export — into `../../cfb-analytics-app/data/`

| Script | What it writes |
|---|---|
| `12_export_frontend_json.py` | Every per-season app file (players, rosters, teams, schedules, game logs, manifest). `--season Y` for one season, `--game-logs` for the per-team game logs. |
| `18_export_recruiting.py` | `recruiting_{year}.json` for the Recruiting screen. |

## Checks — read-only, each reproduces a constant or guards a seam

Run before trusting a refresh: `validate_week_ingest.py` (a staged refresh against the stored weeks),
`validate_snapshot_invariance.py` (a week-N snapshot sees only weeks 1..N), `report_rating_moves.py` (what moved, and
whether past weeks held still), `validate_ratings.py --season Y` (the rating distribution).

The others each reproduce a constant named in their docstring, so a constant can be re-measured rather than
trusted: `validate_pace*.py` (the in-season scale), `validate_low_game_weights.py`, `validate_fcs_weight.py`,
`validate_qb_composite.py`, `validate_opponent_strength.py`, `validate_stats_schedule.py`,
`validate_team_overall.py`, `validate_team_roster.py`, `validate_early_reliability.py`, `validate_rankings.py`,
`validate_part_time_rating.py` (should a part-season rating count missed games — the Gaines question, v4.24),
`validate_reliability.py`, `validate_vs_draft.py`. Two of them write a JSON the app reads
(`validate_rankings.py` → `ranking_validation.json`; `validate_reliability.py` without `--no-export`), so they are
not pure gates.

## Not part of the pipeline

`archive/` holds retired scripts. `explore_*.py` were one-off surveys (the API inventory, the low-game exploration);
their results live in `docs/API_INVENTORY.md` and the `low-game-players` METHODS entry.
