# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workspace layout

```text
CFB-Analytics-Portfolio/
├── cfb-analytics-pipeline/    # Private Python ETL + ML pipeline
├── cfb-analytics-app/         # Public vanilla JS/HTML frontend (GitHub Pages)
├── cfb-analytics-computer-vision/ # Experimental CV prototypes
└── cfb-analytics-v1-archived/ # Prior version — reference only, do not modify
```

## Pipeline (`cfb-analytics-pipeline/`)

### Environment

```bash
# Activate venv (always required before running scripts)
.venv/Scripts/activate        # Windows bash
.venv/Scripts/Activate.ps1    # PowerShell

# Run a script
python scripts/01_harvest_games_players_stats.py
python scripts/07_compute_player_ratings.py --season 2025
python scripts/12_export_frontend_json.py

# Tests
pytest tests/ -q
```

No build step. 41 unit tests in `tests/` — no network or credentials required.

### Key architecture decisions

**Local JSON only — there is no database.** Every script reads and writes plain JSON via
`utils/store.py` (`read_raw`, `read_computed`, `read_ratings`, `write_computed`). The project
previously ran on Supabase/PostgreSQL; that layer was fully retired in Aug 2026. The only
credential needed is `CFB_API_KEY`. Retired DDL is in `archive/sql/` as field documentation
only — never executed. **Do not reintroduce psycopg2, supabase, or DATABASE_URL.**

**Always engine-filter when reading ratings.** `data/computed/ratings.json` holds every
engine's output in one file keyed by `(player_season_id, season, engine)`. Reading it
unfiltered returns multiple rows per player-season, which double-counts on export and
corrupts any `max()`/`mean()` over `overall_rating`. Use `read_ratings("edge")`, never
`read_computed("ratings")` directly.

**NaN must never reach disk.** `write_computed()` and `json_utils.write_json()` both scrub it.
Note `DataFrame.where(..., other=None)` does *not* — it cannot put None in a float64 column,
so NaN survives and gets written as the literal token `NaN`, which is invalid JSON and breaks
the browser's `fetch().json()`. Always go through the helpers.

**Ratings are absolute, never pool-relative.** EDGE positions map through fixed
`EDGE_OVR_ANCHORS`; OL/K/P map through fixed `COMPOSITE_OVR_ANCHORS` via `composite_to_ovr()`.
Percentile-of-the-pool scaling is a bug — it guarantees somebody rates 99 every season and
destroys cross-era comparability. See `docs/AUDIT_FINDINGS.md` §9.

**No offensive lineman carries an earned rating.** Withdrawn in v4.3. The number that used to
be there read `team_rush_ypa` and `team_sack_rate` from the player's own stats payload, where
they are never written, so 55% of the formula silently collapsed and what shipped was
`0.25 + 0.30·recruiting + 0.10·class + 0.05·award`: r = 0.877 with the recruiting composite, 20%
of rated linemen on exactly 80.0, and Spearman **−0.274** against EA. The "88 cap" was that
arithmetic, not a policy. Script 07 emits OL rows with `overall_rating = None`,
`rating_status = "not_rated"` and a reason — a *missing* row would make a lineman vanish from
his own roster, so withheld and absent must stay distinguishable.

**The line is rated as a unit instead** (`utils/line_unit.py`, computed in script 10, stored in
`sub_ratings.line_unit`). Five metrics from `/stats/season/advanced` and `/stats/season`: line
yards, stuff rate, power success, second-level yards, sack rate allowed per dropback. **Bounds
are era-bucketed (2008–13, 2014–20, 2021+)** because the provider changed how it computes line
play at 2014 and again at 2021 — pooled bounds drifted the median from 52 in 2008 to 77 in 2023.
Those breaks are deliberately *not* script 07's `ERA_ANCHORS` (2013, 2018); they track different
phenomena. Missing inputs renormalise the remaining weights and must never contribute a zero —
that is the exact failure that killed the player rating. Validated at +0.179 against linemen
drafted.

**Anchors are calibrated against our own EDGE history; EA is the reference, never the target.**
Each anchor's x-coordinate is the edge_score posted by the Nth-best player at that position in a
typical (median) season over the last five. EA CFB 27 is consulted only to answer *"are we too
generous, too stingy, is the ceiling right"* — it never supplies a number, and rank-matching to
it every season would reintroduce the pool-relative scaling above. Where EA and our anchors
already agreed (TE), nothing was changed.

**Coverage denial is credited additively, not multiplicatively** (`COVERAGE_CREDIT` in script 06,
CB/S/DB only). A shutdown corner's counting stats are low *because* he covers — quarterbacks
throw elsewhere. `def_context_modifier` already knew which defenses were good, but it multiplies,
and 1.1 × a suppressed composite is still suppressed. The credit is additive, scaled by the
team's season-long pass defense and by the player's share of his own secondary's tackles. Tuned
against EA as an external consensus: Spearman 0.639 → 0.658, while a placebo crediting *randomly
chosen* defenses the same amount scores 0.629, below not crediting at all.

**Pass denial is measured against the offense actually faced** (`shortfall_vs_expectation` in
script 06). Raw YPA allowed cannot separate a defense that shut down good passing teams from one
that drew a soft schedule, so each game is compared to what that offense does in its *other*
games and the shortfalls are attempt-weighted across the season. Per game only for the
comparison — the credit itself stays a season figure, because one game's passing line is mostly
noise and a season of them is the defense. Within-position agreement with EA rose in every season
tested (2025 .6507 → .6588, 2024 .5357 → .5421, 2023 .2756 → .2818), and the shuffled-team placebo
scores *below* crediting nobody.

**A tackle needs a denominator, and one candidate failed its test.** v4.3 added three things
to the defensive composite and rejected a fourth:

- **Solo vs assisted** (`SOLO × 1.25 + ASSISTED × 0.65`). Calibrated to be aggregate-neutral —
  solos are 56.4% of tackles, so the average defender's credit is unchanged and only the mix
  moves. `defensiveSOLO` does not exist before 2013; `season_records_solo()` asks the data
  rather than hardcoding a year, so a season that stops publishing it degrades to plain totals
  instead of reading every tackle as an assist.
- **Fumble recoveries.** `fumblesREC` only. **`fumblesFUM` is a fumble the defender COMMITTED** —
  974 rows, 84% in games where he also had a return/INT/recovery, 455 also carrying
  `fumblesLOST`. Crediting it would pay a corner for coughing up a return. Forced fumbles are
  not published per player anywhere.
- **Opportunity index** — counting stats scaled by `clip(median_plays_pg / this_defence_pg,
  0.85, 1.20)` from `/stats/season/advanced` `defense.plays` (2008+). Passes the placebo test:
  real +0.0085 mean Spearman vs EA, shuffled **−0.0025**, i.e. below doing nothing.
- **Havoc share: computed, published, NOT scored** (`HAVOC_CREDIT = {}`). Replacing each unit's
  havoc with one shared constant scored *better* (+0.0019) than the real denominator (+0.0011),
  which means the credit was re-weighting TFL/PBU/recoveries the composite already counts.
  Re-enabling it requires a fresh ablation, not just a value.

**Rate features are shrunk toward the position mean** (`shrunk_rate` in script 07:
`(events + 12·prior) / (tackles + 12)`). `instinct = (INT+PBU)/max(TOT,1)` gave one tackle and
one breakup a perfect 1.0, and **5,848 player-seasons posted a ratio ≥ 1.0 against a
normalisation ceiling of 0.3** — the feature was a constant for most of the pool. `FEATURE_BOUNDS`
for `disruption_rate` and `instinct_score` were re-derived afterwards; leaving them stale would
have kept it saturated.

**Specialists occupy a narrow band by design.** K and P top out near 88. Their impact range is
genuinely smaller than a skill player's, and the tell that this was wrong was a punter
outranking the receivers on his own team page.

**A defensive back's overall IS his three archetypes** (`SECONDARY_ARCHETYPE_WEIGHTS` in
script 06): ball hawk (INTs/PBUs), lockdown (playing time × pass denial, no box-score input),
run support (tackles/TFLs/sacks — the only place tackles count as production). CB 40/40/20,
S 20/30/50, DB even thirds. Costs ~0.005–0.015 Spearman against EA versus a flat composite;
bought deliberately so the number equals the sub-ratings printed beside it. `ARCHETYPE_SCALE`
puts the three on one 0–10 axis and **must be re-measured whenever their inputs change** —
stale constants once left coverage capped at 7.1 while run support reached 20. Re-measured in
v4.3 after fumble recoveries entered ball hawk: 12.9 → 13.4, with run support unmoved at 14.8,
which independently confirms the tackle changes were aggregate-neutral.

**Team ratings blend three signals** (script 10): SP+ 50%, our player ratings 30%, team stats
20%, renormalizing when a signal is absent — which is how 2026 works with neither SP+ nor
stats. Team stats were harvested and silently ignored before v2.1.

**`avg_top()` returns `None`, never 50.** It used to return a hard-coded 50.0 for a position
with nobody rated, which is a trap rather than a default: an empty position silently became an
average one. With the OL player rating withdrawn that would have made 40% of every team's run
offence an identical constant and nothing would have errored. Callers renormalise via `blend()`.
The rule is universal — a team with no rated kicker gets no special-teams number rather than a
fabricated one.

**API response caching.** `utils/api_client._get()` caches every response as `.cache/{md5}.json`
(~2 GB). Re-runs are instant. Gitignored. Never delete unless re-fetching is intentional.

**"The stats row exists" is not "the stats came back."** The harvest writes a
`season_aggregate` whenever usage *or* PPA *or* a box score returned, so a row can hold nothing
but a snap share — and ~350–465 player-seasons a year get no aggregate at all despite having a
full set of game rows. Rating inner-joined on that row, so both kinds of player were dropped
outright: Jayden Virgin-Morgan played four seasons at Boise State with 12–14 game rows each and
was rated in none of them. `utils/stat_agg.py` owns the shared rule — `has_box_score()` decides
whether a payload records production, `aggregate_game_stats()` rebuilds one by summing game rows
(counts sum, `LONG` is a max, rates are recomputed from totals, and the paired strings
`"25/38"` / `"2/3"` must be split first or every rebuilt QB reads zero attempts). Scripts 07, 12
and 15 all go through it; if they disagree, a player is rated on evidence the site never shows.
Rebuilt payloads are marked `rebuilt_from_games` and carry **no usage** — game rows never had it.

**Raw tables are read once per process.** `read_raw()` does not cache, and script 07's loader
used to call it per position per season — 228 re-parses of `data/raw` (255 MB of stats alone) on
a `--all-seasons` run. Script 07 now holds `_RAW`, `_RATINGS` and a prebuilt stats index. Any new
per-position or per-season loader must do the same or the run time goes quadratic again.

**A player has exactly one team per season.** The harvest upsert is keyed
`(player_id, season, team_id)` and only ever *added*, so when the API later corrected a
player's team the old row survived forever — 7,206 (player, season) pairs across 4,121
players ended up on two teams at once, double-counting rosters and corrupting any career
trajectory built across them. `_dedupe_player_seasons()` in script 01 now makes each
season's harvest authoritative (newest row per player-season wins). Never reintroduce a
pure append.

**Projections are labelled at every layer.** Seasons that haven't been played carry
`engine="projected"` rows from script 16, never `engine="edge"`. Every row exports
`provenance`, `projection_source`, `projection_confidence`, `projection_low/high` and
`ea_ovr`. Script 12's `ratings_for(season)` picks the frame; the frontend's `ovrPill()`
derives the PROJ badge from the season itself so a call site cannot forget it. **Position
ceilings apply to projections exactly as to earned ratings** — script 16 reads them from
script 07's anchors rather than restating them (OL projected to 93 against its documented
cap of 88 until it did).

**A season a player missed is not a season a player declined.** Script 15 flags an
*interrupted* season — injury or redshirt — as `avail < 0.60 ∧ prior_max ≥ 0.50 ∧
avail ≤ 0.60 × prior_max`, where availability is games played over the team's games (raw
game counts are not comparable across 12-game, 13-game and 2020 seasons). The test is
relative to the player's **own** prior best, not an absolute starter threshold: a rotation
corner who never cleared 70% still gets hurt. It reads prior seasons only — a test asserts
truncating a career cannot change an earlier verdict, or the model trains on information it
will not have. Career shape is then computed twice, raw and healthy-only, and **both** are
fed to the model; the gap between them is the signal. `pct_accel_healthy` matters most —
acceleration is a second difference, so one lost season poisons it twice (+116 vs −12 for
Jaden Mickey). `bounceback` is a distinct label from `breakout`: returning to a level already
posted is a different claim than exceeding it.

**`player_seasons.year` is NOT a class year — never use it as one.** It is a static player
attribute, constant across the entire career for 84% of players with 3+ seasons and never
incrementing, and it holds an outright calendar year for 114,612 of 269,552 rows (almost all
pre-2017). Cohort cells keyed on it were mixing a player's freshman, sophomore and junior
seasons together. Script 15 derives it: `season − recruit_year + 1` where recruiting has the
player (52%), else `season − first_observed_season + 1` as a floor. Script 12 still exports
the raw value to the frontend, so player cards can misstate class.

**`manifest.json` is the season contract.** The pipeline writes `first_season`,
`last_played_season`, `current_season`, `projected_seasons`. `js/config.js` mirrors them,
and `tests/test_export_contract.py` fails if they drift — that split-brain shipped once
(script 12 said 2026, config.js said 2025).

**`player_seasons` is the join anchor.**
- `players` is identity-only (no `team_id`, no `year`, no `position`)
- `player_seasons` (one row per player × season × team) is what everything else joins to
- `stats`, `ratings`, `player_edge` all reference `player_season_id`
- `recruiting` and `transfers` reference `player_id` (career-level data)

Two players named "Sammy Brown" at different schools are distinct `player_seasons` rows.
Without this, fuzzy name matching contaminates records across unrelated players sharing a name.

**Name matching for scraped sources lives in `utils/matching.py`** (used by scripts 04, 08).
Rules: exact name + school agreeing → match; exact name unique in our data → match (covers
players who transferred since their last season); *fuzzy* name → **only** with school
confirmation, because "Chaden Sullivan" and "Caden Sullivan" are one edit apart and are
different people. Then `resolve_collisions()` unmatches rows where two scraped players claim
the same player of ours. Script 03 has its own transfer-specific variant requiring
`from_school` confirmation.

**Position group normalization.** Raw API positions map to 12 canonical groups
(QB/RB/WR/TE/OL/EDGE/DL/LB/CB/S/K/P). EDGE = OLB/DE pass rushers; DL = interior only.

### Script run order

```text
01 → teams / players / player_seasons / games / stats  (--historical for 2008–2020)
02 → recruiting
03 → transfers
04 → NIL valuations (Selenium; On3 blocks it — currently 0 rows)
05 → coaching changes (seed CSV + ESPN tracker, Selenium)
06 → EDGE scores per player-season
07 → player ratings, engine='edge'  (needs 06)
08 → EA CFB 27 ratings (plain HTTP via EA's Next.js data route — no browser)
09 → supplemental harvest: 17 datasets the API survey found and we were not using
     (coaches, draft picks, advanced season/game stats, havoc, returning production,
      betting lines, pregame WP, CFP field, play counts, wepa, talent, records, ATS,
      weather, venues). `--list` to see them. **Scripts 06 and 10 need
      `team_advanced_season` from here**, or the opportunity index and the line rating
      silently degrade to no-ops.
10 → team_ratings (needs 07, 09)
11 → engine_b ratings, NIL + recruiting composite (needs 02, 04)
12 → static JSON export for frontend (needs 07, 10)
13 → team performance vs. recruiting talent
14 → recruiting class ROI
15 → Engine D, career-curve next-season projection (needs 06, 07)
16 → projected ratings for the upcoming season, engine='projected' (needs 15)
10 --engine projected → projected team ratings (needs 16)
12 --season {projected} → re-export, so the site gets 15/16's output
```

Scripts 13–15 write straight into the frontend's `data/`. Run 12 first on a full refresh, and
again at the end: 15, 16 and the projected team ratings all land after the first export.

`scripts/validate_ratings.py` is not part of the chain — it writes nothing. Run it after any
change to 06/07 and read the table before shipping: per-position distribution plus
within-position Spearman against EA. Distribution shape is a hard gate.

`scripts/validate_vs_draft.py` is the external check. EA is one season and can never be a
backtest; the draft is eighteen years deep and was decided without seeing our numbers. Drafted
players average a peak rating of 82.8 against 62.8 undrafted, and P(drafted | rating band) is
monotone across all six bands. **Our defensive ratings order draft picks at Spearman 0.13–0.25
against 0.42–0.49 for offence** — the clearest external evidence for why usage and the defensive
rating are the next phase.

### Multi-engine ratings

| Engine | Script | Method |
|--------|--------|--------|
| `edge` | 07 | EDGE formula, opponent-adjusted, era-bucketed anchors |
| `engine_b` | 11 | 60% recruiting + 40% NIL (recruiting-only while NIL is empty) |
| `engine_d` | 15 | Career EDGE curve + cohort development, variance-calibrated (own file, `trajectory.json`) |
| `projected` | 16 | Upcoming-season rating: engine_d → cohort carry → recruiting → EA CFB 27 |
| `ea_cfb27` | 08 | EA Sports CFB 27 ratings, 54 attributes (own file, `ea_ratings.json`) |

**Every ranked research finding is shrunk and carries an interval** (`utils/shrinkage.py`).
Ranking 2,310 noisy team-seasons puts luck at the top by construction. `shrink_mean` for
continuous quantities, `shrink_rate` for proportions, both at 80% to match the projection bands.

**Recruiting ROI measures development, not recruiting.** The raw hit rate correlated +0.266 with
the class's own recruiting composite — it restated the star ratings. Script 14 now fits expected
peak OVR per *recruit* from his own composite and scores the class on the residual; correlation
with class recruiting falls to −0.028.

**ERA_ANCHORS** in script 07: modern (2018+), transition (2013–2017), classic (2008–2012).
Classic-era defensive thresholds are 75% of modern, compensating for missing hurries/PBUs
pre-2015.

### Dependency note

xgboost is pinned to **2.1.4**. 2.1.3 crashes in `load_model` against scikit-learn ≥1.6
(`AttributeError: 'super' object has no attribute '__sklearn_tags__'`). scikit-learn is a
required dependency even though nothing imports it directly — xgboost's sklearn API needs it.

---

## Frontend (`cfb-analytics-app/`)

### Local development

```bash
python -m http.server 8000    # then open http://localhost:8000
```

Pure vanilla HTML/JS/CSS. No framework, no bundler, no npm. Served directly by GitHub Pages.

### Architecture

**All data is static JSON in `data/`** — no backend, no Supabase, no API keys. Everything is
generated by pipeline scripts 12–15. `js/dataLoader.js` is the single fetch layer, with an
in-memory cache. (It was once `supabaseClient.js`; that file is deleted — don't resurrect it.)

**Script load order.** Two blocks per page. At the very top of `<body>`, before any
content, so the theme applies and the chrome exists before first paint:

1. `js/config.js` — `CONFIG`, palettes, `ratingColor()`, `ratingTextColor()`,
   `seasonList()`, `fillSeasonSelect()`, entity links (`teamLink`/`playerLink`)
2. `js/shell.js` — applies the theme, injects the sidebar + mobile tab bar

Then before `</div><!-- /.main-content -->`:
3. `js/dataLoader.js` — the single fetch layer (`_load` cache, `fetchAllPlayers()`,
   `fetchTeams()`, `fetchSeasonGames()`, …)
4. `js/ui.js` — render primitives (`ovrPill`, `posBadge`, `deltaChip`, `onThemeChange`, …)
5. `js/dataTable.js` — `createDataTable()` / `createFilterBar()` (pages with tables)
6. Page module (`home.js`, `playerSearch.js`, `teamsPage.js`, `ratingsDisplay.js`,
   `researchDisplay.js`, `season2026.js`)

**Pages:** `index.html` (editorial home), `season2026.html` ('26 hub), `teams.html`,
`players.html`, `ratings.html`, `research.html`, `info.html`. No page carries its own
nav markup or theme script — `shell.js` owns all of it, keyed off `<body data-page="…">`.

**Per-season files only.** The loader fetches `players_{season}.json`,
`rosters_{season}.json`, `schedules_{season}.json`, `transfers_{season}.json`,
`similar_players_{season}.json`, `team_stats_{season}.json`,
`ratings_by_position_{season}.json`, plus the season-agnostic `teams.json`,
`team_ratings.json`, `team_history.json`, `player_transfers.json`, and the research
outputs (`team_performance.json`, `recruiting_roi.json`, `trajectory.json`).
Bare `players.json` / `transfers.json` / `similar_players.json` were stale duplicates
and have been deleted, as have `data/rosters.json` (57 MB) and `data/schedules.json`
(14 MB). `tests/test_export_contract.py` fails if any of them reappears.

**Season pickers must use `fillSeasonSelect()`** — it builds options from
`CONFIG.FIRST_SEASON`..`CONFIG.CURRENT_SEASON`. Never hardcode year lists in HTML; they drift
from what the pipeline actually exported.

**Stat tiles show counted values, never constants.** The home page counters previously
displayed hardcoded numbers (and a `Math.max(gems, 412)` floor that faked the count). Anything
displayed as a statistic must be computed from the loaded data.

**Theme system (v3.1):** two themes, `dark` and `light`, set on `<html>` by `shell.js`
before first paint. Resolution order: `?theme=` URL override → `localStorage["cfb-theme"]`
(legacy `dynasty-dark`/`mid` migrate to `dark`) → `prefers-color-scheme`. Tokens live in
`css/styles.css` (`:root` = dark, `[data-theme="light"]` = overrides).

**Contrast is enforced by a tool.** After any token or palette edit run
`node tools/contrast-check.mjs` (text ≥7:1, muted ≥4.5:1, borders ≥1.8:1,
`--border-strong` ≥3:1, every rating/position fill ≥3:1 vs its guarded text).
It exits non-zero on failure. Nothing renders below 12px, and 12px is reserved for
uppercase labels.

**Accent policy.** `--voice` is the editorial gold (eyebrows, mastheads, active nav);
`--accent` aliases it. Team/player surfaces re-point `--accent` to ink through ONE
inheritance block on `.teams-layout, .modal, .entity-scope`, so team colors and data
colors carry the personality there. Never fork a component to achieve this.

**Domain palettes live only in `js/config.js`** — `POS_COLORS` and `RATING_RAMP`, both
theme-keyed. There is deliberately no CSS copy. Any JS that computes a color must
repaint via `onThemeChange()` or it goes stale when the theme switches.

**UI conventions:** list-rendered elements get `animate-up` inside a `.stagger-children`
wrapper; new page sections get `animate-pop`. Hover states use `transition`, not
`animation`; chrome does not animate on navigation. **Badge/pill text color comes from
the `ratingTextColor()` luminance guard** — this replaces the older "always `#111`" rule,
which was wrong for light theme's deep fills.

**Responsive `@media` blocks must stay at the END of `css/styles.css`.** They share
specificity with the desktop rules they override, so source order is what makes them
win; moving them earlier silently disables them (this shipped as a real bug once).

**SHAP visualization.** `playerSearch.js:renderRatingBreakdown()` renders bars from
`player.shap`. Feature keys must match `CONFIG.SKILL_ATTRS` to get readable labels.

**Scatter plot.** `ratingsDisplay.js:renderScatterPlot()` draws Rating vs Recruiting Stars as
inline SVG with a regression line. Players 8+ points above the line are highlighted as
overperformers — the platform's core value proposition.

## Current direction

**`cfb-analytics-pipeline/docs/ROADMAP.md` is the live plan** — what is next, what is parked
and why, and the known debt. Read it with `docs/RATING_AND_PROJECTION_MODEL.md`, which is the
honest account of what the ratings are and where they measurably fail.

**The docs set, and what each is for:**

| File | Answers |
|---|---|
| `docs/FORMULAS.md` | Every rating formula **as the code actually computes it** |
| `docs/ALTERNATIVES.md` | What we could compute instead, with costs and the test that would settle it |
| `docs/HOW_PROJECTIONS_WORK.md` | The projection engine in plain language, including what MAE is |
| `docs/RESEARCH_METHODS.md` | Each research finding: inputs, formula, what it can support, confounds |
| `docs/API_INVENTORY.md` | **Generated** by `scripts/explore_api.py` — all 74 endpoints, season coverage, join-key match rates, and what the API verifiably does *not* have |
| `docs/RATING_AND_PROJECTION_MODEL.md` | The historical record of what changed and why |

All of it is published on the site at **`methods.html`**, driven by `js/methods.js`.

**Before concluding "the API doesn't have X", read `docs/API_INVENTORY.md`.** It is generated,
it records absences with their evidence, and re-running the survey costs zero requests because
both hits and misses are cached.

**Shipped v3.2 (Aug 2026):** 2026 is a real, projected season across the whole app —
harvested rosters, `engine='projected'` player and team ratings, EA CFB 27 side by side.
Engine D was rebuilt on career EDGE curves + cohort development (MAE 8.24 vs 9.32 naive,
80% intervals covering 79.8%), and every projection ships drivers, a plain-English
explanation and historical comparables. Full record:
`cfb-analytics-app/CHANGELOG_v3.2_projections.md`.

**Shipped v3.3 / rating v4.2 (Aug 2026):** projections split by position family with OL no
longer projected at all, an opportunity model for offensive skill, secondary archetypes, a
position-by-position rating recalibration against EA as an external reference, coverage denial
measured against the offense actually faced, and the recovery of players whose season aggregate
the API never wrote. Full record: `cfb-analytics-app/CHANGELOG_v3.3_ratings.md`.

**Next phase: usage, then the defensive rating.** Snap share and PPA are already harvested,
inside the `season_aggregate` payload, and unlike anything EA-based they are historical and can
therefore be backtested. They are what lets a rating tell *improved* from *played more*, and
they are the only opportunity signal a defender can have. See the roadmap.

**Parked with a reason, not forgotten:** roster construction (`transfer_roi.py` →
`player_development.py` → `roster_composition.py`, blocked on a working NIL source), the playoff
prediction model (scripts 17–19; a strictly out-of-sample 2024/2025 backtest is its ship gate),
headshots, and pre-2016 defensive detail.
