# Internal Reference — Pre-Phase Audit
Generated: 2026-05-24
Scope: Both repos — cfb-analytics-pipeline + cfb-analytics-app

> **⚠ Historical snapshot — superseded 2026-08-10. Do not treat as current.**
>
> This captured the codebase as of 2026-05-24. Several of its findings have since
> been acted on and its architecture map is out of date:
>
> - The "mixed architecture" it describes is gone. Scripts 04, 05, and 11 were
>   ported to local JSON and the entire Supabase layer was deleted, including
>   `utils/db.py`. Nothing in the pipeline talks to a database.
> - The broken `build_player_index()` joins it flags in scripts 04/08/08b are
>   moot: 08 and 08b were deleted (they never produced usable output) and
>   replaced by `08_harvest_ea_cfb27_ratings.py`; 04 now uses `utils/matching.py`.
> - Script 05's hardcoded year fallback is fixed.
> - `api_client.py`'s `verify=False` is fixed.
> - It predates scripts 13, 14, and 15 entirely.
>
> For current architecture see `README.md`; for design rationale and the bug
> record see `AUDIT_FINDINGS.md`. Kept because its reasoning explains why
> several of those changes were made.

---

## Architecture Map

### Pipeline Data Flow

```
01_harvest_games_players_stats.py
   ├── reads: CFB Data API (via api_client._get() with .cache/)
   └── writes: data/raw/teams.json
               data/raw/players.json
               data/raw/player_seasons.json
               data/raw/games.json
               data/raw/stats_season_*.json    (season aggregates)
               data/raw/stats_postseason_*.json
               data/raw/stats_game_*.json      (per-game, per-player)
               data/raw/plays.json             (large, ~40k/season)

02_harvest_recruiting.py
   ├── reads: data/raw/players.json, data/raw/player_seasons.json (name index)
   ├── reads: CFB Data API + 247Sports scrape fallback
   └── writes: data/raw/recruiting.json

03_harvest_transfers.py
   ├── reads: data/raw/players.json, data/raw/player_seasons.json
   ├── reads: CFB Data API + On3 scrape fallback
   └── writes: data/raw/transfers.json

04_harvest_nil_valuations.py  [MIXED ARCH — still hits DB]
   ├── reads: Supabase DB (players + teams via build_player_index())
   ├── reads: On3 via Selenium
   └── writes: Supabase DB nil_valuations table

05_load_coaching_changes.py  [MIXED ARCH — still hits DB]
   ├── reads: data/coaching_changes_seed.csv
   ├── reads: Supabase DB (teams table via build_team_index())
   └── writes: Supabase DB coaching_changes table

06_compute_edge_scores.py
   ├── reads: data/raw/player_seasons.json, players.json, plays.json (or stats_game_*.json)
   ├── reads: SP+ API via api_client (cached)
   └── writes: data/raw/player_edge.json

07_compute_player_ratings.py
   ├── reads: data/raw/{player_edge,player_seasons,players,recruiting,stats_*}.json
   └── writes: data/computed/ratings.json

08_harvest_ea_cfb25_ratings.py  [MIXED ARCH — still hits DB]
   ├── reads: Supabase DB (players + teams via build_player_index())
   ├── reads: TeamCrafters website via requests/BeautifulSoup
   └── writes: Supabase DB ea_ratings table

08b_harvest_ea_cfb26_ratings.py  [MIXED ARCH — still hits DB]
   ├── reads: Supabase DB (players + teams via build_player_index())
   ├── reads: ea.com via Selenium
   └── writes: Supabase DB ea_ratings table + data/ea_cfb26_raw.json (first 500)

09_backfill_defender_ids.py  [ONE-TIME UTILITY — hits DB]
   ├── reads: Supabase DB plays table
   └── writes: Supabase DB plays.defender_player_id (regex update)

10_compute_team_ratings.py
   ├── reads: data/raw/{player_seasons,players,teams,games}.json
   ├── reads: data/computed/ratings.json
   ├── reads: SP+ API via api_client (cached)
   └── writes: data/computed/team_ratings.json

11_compute_engine_b_ratings.py  [MIXED ARCH — still hits DB]
   ├── reads: Supabase DB (player_seasons + recruiting + nil_valuations JOIN)
   └── writes: Supabase DB ratings table (engine='engine_b')

12_export_frontend_json.py
   ├── reads: data/raw/*.json + data/computed/*.json (ALL local JSON)
   └── writes: ../../cfb-analytics-app/data/players_{season}.json
               ../../cfb-analytics-app/data/teams.json
               ../../cfb-analytics-app/data/team_ratings.json
               ../../cfb-analytics-app/data/rosters_{season}.json
               ../../cfb-analytics-app/data/schedules_{season}.json
               ../../cfb-analytics-app/data/transfers_{season}.json
               ../../cfb-analytics-app/data/similar_players.json
               ../../cfb-analytics-app/data/team_history.json
               ../../cfb-analytics-app/data/team_stats_{season}.json
               ../../cfb-analytics-app/data/ratings_by_position_{season}.json
               ../../cfb-analytics-app/data/research/index.json
```

### Frontend Data Flow

> **The app repo owns its own documentation.** For the authoritative frontend
> architecture see `cfb-analytics-app/README.md` (script load order, theme system,
> design-system primitives) and `cfb-analytics-app/CHANGELOG_v3.1_redesign.md`.
> The summary below covers only the pipeline→frontend contract.

Script 12 (plus 13–15) writes `cfb-analytics-app/data/*.json`. `js/dataLoader.js` is the
single fetch layer, with an in-memory `_load()` cache; nothing else fetches.

| Export | Consumed by |
|--------|-------------|
| `players_{season}.json` | `fetchAllPlayers` / `fetchPlayers` / `fetchPlayerProfile` / `fetchPlayerStats`; career views scan all seasons |
| `rosters_{season}.json` | `fetchTeamRoster` → team roster tab |
| `schedules_{season}.json` | `fetchTeamSchedule`; `fetchSeasonGames` (home marquee + '26 hub) |
| `transfers_{season}.json` | `fetchTeamTransfers` → team transfers tab (2021+ only) |
| `similar_players_{season}.json` | player modal comps |
| `team_stats_{season}.json` | team ratings tab stats panel |
| `ratings_by_position_{season}.json` | Ratings page position leaderboards |
| `teams.json`, `team_ratings.json`, `team_history.json` | `fetchTeams`, `fetchTeamOvrMap`, team history tab |
| `player_transfers.json` | player modal career path |
| `team_performance.json` (13), `recruiting_roi.json` (14), `trajectory.json` (15) | Research page tables + home storylines |
| `research/index.json` | research "Published Findings" — **currently `[]`, `research_cache` is empty** |

**Resolved in v3.2 (2026-08-10):**

- ~~**Season constant is duplicated.**~~ — RESOLVED. Script 12 now exports
  `manifest.json` (`first_season`, `last_played_season`, `current_season`,
  `projected_seasons`, `seasons`). `js/config.js` mirrors it, and
  `tests/test_export_contract.py::test_frontend_season_constants_agree` fails the build if
  they drift. `config.js` is loaded synchronously before any fetch, so a runtime read of
  the manifest was not an option — the test is the mechanism.
- ~~**Orphaned exports.**~~ — RESOLVED. `data/rosters.json` and `data/schedules.json`
  (71 MB, zero consumers) deleted; the module docstring now describes what is actually
  written, and a contract test asserts the bare duplicates stay gone.

**Known contract gaps (as of 2026-08-10):**

- **Payload.** `players_{season}.json` is ~8.3 MB and is fetched by home, players and
  ratings, though only the modal needs its `stats_*` and `shap` blobs. A slim grid export
  plus on-demand detail remains the fix.
- **`trajectory_detail.json` is 5.5 MB** and fetched whole the first time any player modal
  opens on a projected season. Acceptable (it is lazy and cached) but it wants the same
  slim/detail treatment eventually.
- **`research_cache` is still empty**, so `research/index.json` is `[]`. Either wire the
  publishing path or delete it — one mechanism.
- **Class year is the API's value and is unreliable.** Several players hold the same
  `year` across four seasons. It feeds the cohort development curves, consistently in both
  training and prediction, so it does not bias the projection — but it is not a
  trustworthy display field.

**Projected seasons (v3.2).** A season that has not been played carries
`engine="projected"` rows from script 16 rather than `engine="edge"`. Consumers must go
through `ratings_for(T, season)` — reading `T["ratings"]` directly returns an empty frame
for such a season, which is how `ratings_by_position_2026.json` was silently `{}`. Every
projected row exports `provenance`, `projection_source`, `projection_confidence`,
`projection_low`, `projection_high` and `ea_ovr`; contract tests assert all of them.

## File Inventory

### Pipeline — utils/

| File | Lines | Purpose | Dependencies | State |
|------|-------|---------|-------------|-------|
| utils/db.py | 85 | psycopg2 connection + bulk_upsert | python-dotenv, psycopg2 | Active (scripts 04,05,08,08b,09,11) |
| utils/api_client.py | 287 | CFB Data API wrapper with disk cache | requests, pathlib, hashlib | Active (all API-calling scripts) |
| utils/store.py | 68 | Local JSON read/write (local-arch) | pandas, pathlib, json | Active (scripts 01-03,06-07,10,12) |
| utils/supabase_client.py | 17 | Supabase Python SDK singleton | supabase==2.10.0 | DEAD — no callers in local-arch scripts |

### Pipeline — scripts/

| File | Lines | Purpose | Arch | State |
|------|-------|---------|------|-------|
| 00_dump_supabase_to_json.py | 113 | One-time DB → local JSON migration | DB | Archive candidate |
| 01_harvest_games_players_stats.py | 624 | Main harvest: teams/players/games/stats/plays | Local JSON | Active, runs first |
| 02_harvest_recruiting.py | 304 | Recruiting data harvest | Local JSON | Active |
| 03_harvest_transfers.py | 294 | Transfer portal harvest | Local JSON | Active |
| 04_harvest_nil_valuations.py | 297 | NIL valuations via Selenium | DB + Selenium | Mixed arch, broken SQL |
| 05_load_coaching_changes.py | 320 | Coaching changes seed + ESPN scrape | DB + Selenium | Mixed arch |
| 06_compute_edge_scores.py | 604 | EDGE score computation | Local JSON + API | Active |
| 07_compute_player_ratings.py | 1526 | Player OVR ratings | Local JSON | Active, largest file |
| 08_harvest_ea_cfb25_ratings.py | 375 | EA CFB25 scraper (TeamCrafters) | DB + requests | Mixed arch, broken SQL |
| 08b_harvest_ea_cfb26_ratings.py | 416 | EA CFB26 scraper (ea.com Selenium) | DB + Selenium | Mixed arch, broken SQL |
| 09_backfill_defender_ids.py | 132 | One-time plays defender ID backfill | DB | Archive candidate |
| 10_compute_team_ratings.py | 730 | Team OVR ratings | Local JSON + API | Active |
| 11_compute_engine_b_ratings.py | 238 | Engine B: 60% recruit + 40% NIL | DB | Mixed arch |
| 12_export_frontend_json.py | 940 | Export all frontend JSON | Local JSON only | Active, runs last |

### Pipeline — other/

| File | Lines | Purpose | State |
|------|-------|---------|-------|
| requirements.txt | 19 | Python dependencies | Active |
| sql/schema.sql | 367 | Supabase DB schema + RLS | Reference (pre-v2 base + v5 migration) |
| docs/AUDIT_FINDINGS.md | 181 | Audit findings from v2.0 | Reference |
| README.md | 3 | Empty | Needs content |
| data/raw_backup_2026-05-22.zip | — | Snapshot backup of data/raw/ from 2026-05-22 | Backup — do not delete; not in script run order |
| data/computed/team_season_stats.json | — | Intermediate team season aggregates consumed by script 12 | Active input to script 12; writer is script 10 (secondary output not in architecture map — see Quirk 30) |

### Frontend — files

Inventory moved to `cfb-analytics-app/README.md` (it drifted twice while duplicated here).
Shape as of v3.1: `js/` = config, shell, dataLoader, ui, dataTable + one module per page;
`css/` = styles.css (tokens/layout) + components.css (components + design-system
primitives), 1,446 lines total; `tools/contrast-check.mjs` gates palette changes.

### Frontend — data/ (generated by pipeline scripts 12–15)

Per-season families: `players_`, `rosters_`, `schedules_`, `similar_players_`,
`team_stats_`, `ratings_by_position_` (2008–2025; schedules also 2026),
`transfers_` (2021–2025 only — the portal era).
Season-agnostic: `teams.json`, `team_ratings.json`, `team_history.json`,
`player_transfers.json`, `team_performance.json`, `recruiting_roi.json`,
`trajectory.json`, `research/index.json`.

Bare `players.json` / `transfers.json` / `similar_players.json` were deleted as stale
duplicates. `rosters.json` and `schedules.json` are the same kind of leftover and are
still present — see the contract gaps above.

---

## Active vs. Dead Code

### Confirmed Active
- `utils/db.py`: Used by scripts 04, 05, 08, 08b, 09, 11 (all DB-touching scripts)
- `utils/api_client.py`: Used by scripts 01, 02, 03, 06, 08b, 10 (+ caching)
- `utils/store.py`: Used by scripts 01, 02, 03, 06, 07, 10, 12
- Scripts 01, 02, 03, 06, 07, 10, 12: Core local-arch pipeline, fully active
- All 4 JS files, all 6 HTML files, both CSS files: Active

### Confirmed Dead / Archive Candidates
- `utils/supabase_client.py` (17 lines): `get_client()` — zero callers in any local-arch script. Still listed in requirements.txt as `supabase==2.10.0`. Should be removed in clean-arch.
- `scripts/00_dump_supabase_to_json.py`: One-time migration utility; already completed. Move to `scripts/archive/`.
- `scripts/09_backfill_defender_ids.py`: One-time backfill; already completed. Move to `scripts/archive/`.
- `styles.css` lines 207–223: `.nav-brand`, `.nav-links`, `.nav-spacer` — kept as fallback per inline comment ("keep so old references don't break"), but no current HTML uses them.

### Architecturally Inconsistent (Mixed Arch)
- Scripts 04, 05, 08, 08b, 11: Still use Supabase DB connection. The rest of the pipeline is fully local-JSON. These are not wrong for their purpose (NIL, coaching, EA, engine_b), but they break the "all computation is local" principle.
- The broken `build_player_index()` SQL in scripts 04, 08, 08b references `players.team_id` which does not exist in the v2 schema (players is identity-only; team association is in `player_seasons`). These scripts are currently non-functional at the player-matching step.

### Dead Fields / Never-Used Code
- `scripts/07_compute_player_ratings.py` line 102: `POSITION_CEILING: dict[str, int] = {}` — defined empty, never populated. The ceiling guard at lines 1365–1366 checks `if POSITION_CEILING.get(pg)` which is always falsy. The variable is dead.
- `scripts/07` line 1477: `return True  # always allow upsert` — `validate_distribution()` is informational only; it never blocks. The return value is unused.
- `scripts/10_compute_team_ratings.py` line 657: `"avg_starter_rating": None,  # deprecated in v2; use sub_ratings` — dead field in output dict.
- `styles.css` lines 1099–1101: `.draft-info`, `.draft-meta` — "Legacy — no longer used but keep so old references don't break" per inline comment.

---

## Known Quirks & Constraints

### From CLAUDE.md (Do Not Change)
1. **psycopg2 only** for bulk DB reads — Supabase REST has hard 1000-row limit.
2. **Auto-detecting connection**: `_get_working_url()` tries `DATABASE_URL` (IPv6) then `DATABASE_URL_POOLER` (IPv4); caches result for process lifetime.
3. **API cache**: `.cache/{md5}.json` per endpoint. Never delete unless re-fetch is intentional.
4. **Dedup before bulk_upsert**: `ON CONFLICT DO UPDATE` fails on duplicate conflict keys (`CardinalityViolation`). Always dedup. Pattern established in scripts 02/03.
5. **player_seasons is the join anchor**: `stats`, `ratings`, `player_edge` all reference `player_season_id`. `recruiting` and `transfers` still use `player_id` (career-level).
6. **Transfer matching requires team confirmation**: `fuzzy_match_player()` in script 03 returns `None` for ambiguous multi-match (prevents wrong player association).
7. **Partial unique indexes on stats**: Two separate indexes to handle NULL game_id correctly.
8. **Script run order is fixed**: 01 → 02 → 03 → [04,05] → 06 → 07 → [08,08b] → 10 → 11 → 12.

### From Code Audit (Bugs / Quirks Found)
9. **api_client.py line 55**: `verify=False` — SSL certificate verification disabled on all API requests. Security risk; intentional workaround for a specific environment issue (not documented). Do not silently remove — needs a comment explaining why or proper fix.
10. **scripts/04, 08, 08b — broken build_player_index()**: SQL query `SELECT p.id, p.name, t.school FROM players p LEFT JOIN teams t ON t.id = p.team_id` references `players.team_id` which does not exist in v2 schema. These scripts will crash at the player-index-build step. Fix requires joining through `player_seasons` instead.
11. **script 07 docstring line 22–25**: References `python scripts/06_train_ratings.py` — old filename. Actual file is `07_compute_player_ratings.py`. Wrong self-reference.
12. **script 07 line 102**: `POSITION_CEILING = {}` never populated. All ceiling guards are no-ops.
13. **script 10 lines 357, 669**: `import pandas as pd` inside function bodies (`compute_roster_quality()` and `export_team_ratings()`). Should be at module top-level. Causes re-import overhead on every call.
14. **script 08 line 177**: `import re` inside `_parse_page()` function. Should be at module top.
15. **script 05 line 230**: `start_season = int(years[0]) if years else 2024` — hardcoded fallback year. Will quietly produce wrong data after 2024. Should be `datetime.date.today().year`.
16. **script 02 line 166**: `for school in ps_map.get(int(pid), {""}) :` — trailing space before colon. Harmless but non-PEP-8.
17. **index.html lines 269–284**: `posColor_fn()` and `_ratingColor()` defined inline — duplicates of `posColor()` and `ratingColor()` from config.js but with a different palette. The `_ratingColor()` uses a green/yellow/orange/red scale vs config.js's theme-aware blue/gold scale. These are intentionally slightly different for the home page summary display, but the duplication is confusing.
18. ~~**supabaseClient.js naming**~~ — **RESOLVED (v3.0):** renamed to `js/dataLoader.js`; the Supabase-era file no longer exists.
19. **schema.sql line 33**: `players` table still has `team_id` column in the base CREATE statement. The v2 design (CLAUDE.md) says players is "identity-only." The DB does have this column; scripts 01+ no longer write to it but it's not been dropped. The broken SQL in 04/08/08b exploits this stale column.
20. **index.html sidebar**: Has a `<div class="sidebar-season-wrap">` (season selector) not present in any other page's sidebar. The other pages have their own season selectors in the main filter bar.
21. ~~**teams.html inline `_load` coupling**~~ — **RESOLVED (v3.0):** the inline script was extracted to `js/teamsPage.js`, which declares its dependency on `dataLoader.js` through the documented load order.
22. ~~**research.html lacks the modal**~~ — **RESOLVED (v3.0):** research.html now loads `dataLoader.js`, `ui.js`, `dataTable.js` and `playerSearch.js`, so research tables link to players and open the modal like every other page.
23. **script 12 DEFAULT_OUTPUT**: `Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"` — hardcoded relative path that assumes the two repos sit as siblings in the same parent directory. Works for this workspace layout (`CFB-Analytics-Portfolio/`).
24. **script 12 animateCounter "stat-players" counter**: Hard-coded to 8421 (line 294 equivalent in index.html). This is in index.html, not script 12, and is a static fake number not tied to actual data.

### From Second-Pass Audit (Additional Findings)

- **[S2-1] `data/computed/team_season_stats.json` writer is ambiguous**: Script 12 reads this file at line 131 via `read_computed("team_season_stats")`, but the architecture map only documents script 10 writing `team_ratings.json`. The `team_season_stats.json` file exists in `data/computed/` — it is most likely a secondary output of script 10, but this is not documented in script 10's docstring or in CLAUDE.md. Verify before modifying script 10.
- **[S2-2] Transfers data gap in frontend**: `transfers_{season}.json` exists for 2021–2025 only — there is no portal data before 2021. `fetchTeamTransfers()` (`dataLoader.js`) returns an empty array outside that range; since v3.0 the UI states the reason ("portal data begins in 2021") rather than showing a bare empty table.
- **[S2-3] Legacy non-season-specific data files**: `data/players.json`, `data/ratings_by_position.json`, `data/rosters.json`, `data/schedules.json`, `data/transfers.json` exist in `cfb-analytics-app/data/` root. These are stale outputs from a prior export format. They are not loaded by any current JS code (all fetches use season-specific filenames). They are safe to remove in clean-arch after confirming no lingering HTML references.
- **[S2-4] `bulk_upsert()` list conflict_col is valid**: Script 11 calls `bulk_upsert("ratings", deduped, ["player_season_id", "engine"])` passing a list. Confirmed correct — `db.py` line 63 handles `isinstance(conflict_col, str)` check and converts accordingly. This is NOT a bug.
- **[S2-5] Script 11 wrong filename in its own docstring**: `11_compute_engine_b_ratings.py` line 17 references `python scripts/11_compute_engine_b.py` — missing `_ratings` suffix. Self-referential documentation error.
- **[S2-6] Script 11 late import in function body**: `from collections import defaultdict` is imported inside `compute_nil_position_medians()` at line 124. Should be at module top per PEP 8.

### From docs/AUDIT_FINDINGS.md (Durable Design Decisions — Do Not Re-litigate)

- **[AF-1] 12-group position system**: EDGE (OLB/DE), CB, S split intentionally from DB/DL. MLB → LB fix from v2.0.
- **[AF-2] 4-tier playing-time system**: Controls trust in stats vs recruiting ceiling. No artificial OVR caps.
- **[AF-3] Per-game opponent quality**: Per-game SP+ multiplier, not season-average. Correct for Jeanty 2024 playoff case.
- **[AF-4] validate_distribution() is informational only**: Targets are guidance; never a hard block. Only change weights if the entire group distribution is off.
- **[AF-5] EDGE per-game stat composite for defense**: Uses game_aggregate stats rows from script 01. Not play-text regex (that approach had <2% coverage).

---

## Open Questions (Resolved Before Editing)

All of the following were resolved during audit:

1. ~~**`supabaseClient.js` rename**~~ — **DONE (v3.0):** now `js/dataLoader.js`, referenced by all seven pages.

2. **`utils/supabase_client.py` removal**: No active callers in local-arch. Remove file and remove `supabase==2.10.0` from requirements.txt in clean-arch. Scripts 04, 05, 08, 08b, 11 import `get_client()` directly — they need different treatment (they still use the DB).

3. **Broken SQL in 04/08/08b**: `players.team_id` does not exist in v2. Fix requires joining through `player_seasons` (e.g., `SELECT p.id, p.name, ps.team_id, t.school FROM players p JOIN player_seasons ps ON ps.player_id = p.id JOIN teams t ON t.id = ps.team_id WHERE ps.season = CURRENT_YEAR`). This is a clean-arch fix, not a phase-0 fix.

4. **`09_backfill_defender_ids.py`**: Archive alongside `00_dump_supabase_to_json.py`. Both are one-time utilities that have already run.

5. **`api_client.py verify=False`**: Needs a code comment explaining why it's disabled. May be a self-signed cert environment. Do not remove silently.

6. **`POSITION_CEILING`**: Either populate it (define per-position max OVR values) or remove the dict and the dead guard at lines 1365–1366. In clean-arch, leaning toward removing — the EDGE_OVR_ANCHORS already act as implicit ceilings via the piecewise mapping.

7. **Hardcoded year 2024 in script 05 line 230**: Fix to `datetime.date.today().year` in clean-arch.

8. **Late imports in scripts 08 and 10**: Move `import re` (08 line 177) and `import pandas as pd` (10 lines 357/669) to module top in clean-arch.

9. **`_ratingColor()` in index.html**: Keep as-is for now. It's slightly different from `ratingColor()` by design (simpler green/yellow scale for the home page summary). Clean-arch could consolidate if config.js exposes a simpler variant.

10. **README.md**: Currently empty (3 lines of whitespace). Needs content in clean-arch — minimum: what this repo does, how to set up, run order.
