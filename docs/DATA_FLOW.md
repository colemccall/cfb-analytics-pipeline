# Data flow — from the API to the pages

One page for the whole path. Each box names the script or file that owns that step; the scripts' docstrings and
[`../scripts/README.md`](../scripts/README.md) say how. The site's own version of this page is the `data-flow` entry
under About › Methods (`menu.html#/methods/data-flow`).

```
 CollegeFootballData API  (key: CFB_API_KEY from the environment / gitignored .env)
          │
          ▼   utils/api_client.py — every response cached in .cache/ (no expiry; CFB_REFRESH_YEARS bypasses it)
 ┌────────────────────────────────────────────────────────────────────────────────────────────┐
 │ HARVEST   01 (a whole season) · 01b (a season being played: newest week + season totals)   │
 │           02 recruiting · 03 transfers · 05 coaching · 09 supplemental · 19/20 images      │
 └────────────────────────────────────────────────────────────────────────────────────────────┘
          │   utils/store.py — the only persistence layer
          ▼
   data/raw/*.json        games · teams · players · player_seasons · stats (per game + per season) · …
          │
          ▼
 ┌────────────────────────────────────────────────────────────────────────────────────────────┐
 │ RATE (finished seasons)   06 EDGE → 07 player ratings → 10 team ratings → 11 → 15 → 16 →    │
 │                            10 (projected) → 13 → 14                                         │
 │ RATE (a season in play)   17: 06 --in-progress → 07 → 10 in a private copy  → live_{season} │
 │                           17b: the same, once per completed week            → weekly_*      │
 └────────────────────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
   data/computed/*.json   ratings (several engines) · team_ratings · projections · live · weekly
          │
          ▼   12 export (+ 18 recruiting) — the seam, guarded by tests/test_export_contract.py
   cfb-analytics-app/data/*.json   players_{s} · rosters_{s} · teams · schedules_{s} · live_2026 ·
                                   weekly_*_{s} · gamelog_{s}_{team} · manifest · …
          │
          ▼   js/dataLoader.js — the only code in the app that fetches
   menu.html  (js/menu*.js screens; js/methods.js, findings.js, changes.js explain the numbers)
```

## The refresh while a season is being played

```bash
cd cfb-analytics-pipeline
python scripts/01b_refresh_in_season.py --season 2026 --weeks 9 --stage   # ~12 requests → data/staging/2026/
python scripts/validate_week_ingest.py  --season 2026 --weeks 9           # schema, counts, consistency (read-only)
python scripts/01b_refresh_in_season.py --season 2026 --weeks 9 --merge   # cache + data/raw (idempotent)
python scripts/09_harvest_supplemental.py --dataset team_advanced_season team_records --seasons 2026   # from the cache
python scripts/17_live_season.py --season 2026 --offline                  # live_2026.json
python scripts/17b_weekly_progression.py --season 2026                    # weekly_*_2026.json (every week, ~30 min)
python scripts/12_export_frontend_json.py --season 2026                   # the app's per-season files
python scripts/12_export_frontend_json.py --season 2026 --game-logs       # gamelog_2026_{team}.json
python scripts/report_rating_moves.py --season 2026 --before <copy of data/computed taken first>
```

Before a refresh, copy `data/raw` and `data/computed` aside: they are gitignored, so git cannot restore them.

## Where a number can go wrong, and what checks it

| Step | Failure | Check |
|---|---|---|
| API → raw | a failed request read as "no data"; a week merged twice | `validate_week_ingest.py`; the merge is idempotent by key |
| raw → a week-N snapshot | the snapshot reads something from after week N | `validate_snapshot_invariance.py` |
| computed → app | `NaN` tokens, two seasons in one file, orphaned files | `tests/test_export_contract.py` |
| app → page | a missing number drawn as 0 | the UI rule: missing renders "—" |
