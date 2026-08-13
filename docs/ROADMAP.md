# Roadmap

*Written 2026-08-12, after the v4.2 rating pass shipped.*

What is next, why it is next, and what is parked. Everything here is either already
decided or already measured — the file exists so none of it has to be re-derived from
scratch, and so that "we thought about that" is checkable rather than remembered.

Read `RATING_AND_PROJECTION_MODEL.md` first if you have not. It is the honest account of
what the ratings are and where they break; this file is what we intend to do about it.

**Updated 2026-08-12 after a systematic API survey.** Three items below that were recorded as
blocked are not: `/coaches` covers 2010–2024 with full tenure (the coaching event study is
unblocked), `/draft/picks` joins to our players at 94.5% (survivorship and external validation
are unblocked), and `/stats/season/advanced` carries `lineYards` / `stuffRate` / `powerSuccess`
back to 2010 (a real line-unit rating is possible). See `API_INVENTORY.md`, which is generated,
and `ALTERNATIVES.md`, which turns each into an option with a test attached.

---

## Shipped since this was written: rating v4.3 (2026-08-12)

Everything the API survey unblocked has now been built, and the two "blocked" items above are
closed. What landed:

- **17 supplemental datasets harvested** (`scripts/09_harvest_supplemental.py`) — 210,287 rows
  across coaches, draft picks, advanced season and game stats, havoc, returning production,
  betting lines, pregame win probability, CFP participants, per-player play counts, opponent-
  adjusted EPA, talent, records, ATS, weather and venues. Held whether or not anything consumes
  them yet, so the next question does not start with a week of "can we even get that".
- **The OL player rating is withdrawn** and replaced by a line-unit rating validated against
  draft outcomes (+0.179, against the withdrawn rating's -0.274 vs EA).
- **Defence**: solo/assist split, fumble recoveries, an opportunity denominator that passes its
  placebo test, and small-sample shrinkage. Five of six positions improved against EA.
- **Havoc share was built and rejected on its own evidence.** Published, scored at zero.
- **The coaching event study exists.** Coach carry-over r = +0.343 across 101 coaches.
- **Recruiting ROI is residualized** — its correlation with class recruiting strength falls from
  +0.254 to -0.028, so it finally measures development rather than restating the star ratings.
- **Every ranked finding carries shrinkage and an interval** (`utils/shrinkage.py`).
- **NFL departure is measured**: 22.1% of top-decile players leave against 0.7% in the bottom
  half. §4d survivorship is no longer a disclosure without a number.
- **Draft validation** is the first backtestable external check these ratings have ever had.

What did NOT change, and is still the headline: **usage, then the defensive rating.** The v4.3
defensive work is a floor, not a fix. The clearest evidence is external — our defensive ratings
order NFL draft picks at Spearman 0.13-0.25 against 0.42-0.49 for offence.

---

## Superseded 2026-08-13 — read this before the sections below

The order that follows was written before anyone had measured **how much of a rating is
signal**. That measurement now exists (`scripts/validate_reliability.py`), and it moves three
things:

| what this file says | what was measured |
|---|---|
| Usage is the headline, and "matters most for defense" | `snap_pct` reaches **15 rated defenders out of 43,008 since 2016**. It is offence-only. Coverage among *rated* offensive skill players is 88–99%, not the 29.8% recorded below — that figure counts every stat row, most belonging to players nobody rates. |
| Fix the defensive rating next | DB/CB already extract **83–88%** of the year-over-year signal their measurement allows; QB extracts **63%**. Defence is close to its ceiling and offence has the headroom. |
| Defensive ratings order draft picks at 0.13–0.25 | True for rank agreement *among drafted players*. On separating drafted from undrafted the same ratings score **AUC 0.83**, and a fitted model on the same inputs scores 0.785. The weights are not the problem; the inputs are. |

Three candidate builds were measured and rejected in the same pass — a per-game defensive
denominator (0.5417 against the shipped season index's 0.5448), fitting the defensive weights
to the draft (AUC 0.785 against 0.829), and snap share as a projection feature (0.002 MAE).

**The revised order is in `ARCHITECTURE_REVIEW_2026-08.md` §6.** In short: publish reliability
and per-position confidence first; then per-snap efficiency as a *sub-rating* for offence and
the Production/Talent split for the two thirds of every roster whose number is a recruiting
grade; then the projection, aimed at QB and WR rather than defence; then the playoff model,
which is unblocked and whose benchmarks are on disk. The sections below are kept because the
data facts in them are still correct — only the priority is wrong.

---

## The one rule that orders everything below

**A model that cannot show its backtest does not ship.**

That single rule is why the order below is not "most exciting first". We hold exactly one
season of EA Sports CFB 27 data, so every EA-based idea is unvalidated *by construction*
until CFB 28 exists (~mid-2027) and gives us one year-over-year pair. Usage data is
historical, so it can be tested properly today. Usage therefore goes first, even though
the EA ideas are more interesting.

*(That last sentence is the part 2026-08-13 overturned: usage is testable, was tested, and is
worth 0.002 MAE. The rule stands; the conclusion drawn from it did not survive the test.)*

---

## Next: usage, and the ratings themselves

### 1. Opportunity for everyone, not just offensive skill

**The problem.** A rating cannot currently tell *improved* from *played more*. v3.3 fixed
this for offensive skill players by modelling opportunity — production share, depth-chart
rank on next season's roster, production departing ahead of him — and it was worth ~9% on
yards prediction. Defense got nothing, because there are no touches to count. Defensive
projections ship marked **low confidence** and that label is doing real work.

**What exists already.** The usage endpoint is harvested and has been all along. It lands
*inside* the `season_aggregate` stats payload, not on `player_seasons`:

| Key | What it is |
|---|---|
| `snap_pct` | share of team snaps |
| `snap_pct_pass` | share of passing-down snaps |
| `snap_pct_rush` | share of rushing-down snaps |
| `ppa` | predicted points added |
| `award_tier` | postseason award weight |

**Coverage, measured 2026-08-12** (nonzero `snap_pct` per season-aggregate row):

| Seasons | Aggregates | With usage | Share |
|---|---:|---:|---:|
| 2008–2012 | 16,719 | **0** | **0%** |
| 2013–2015 | 11,573 | 6,558 | 56.7% |
| 2016–2025 | 77,941 | 25,151 | 32.3% |
| **All** | **106,233** | **31,709** | 29.8% |

Read that table carefully, because it is not what it looks like at a glance:

- **Usage does not exist before 2013.** Any model using it either starts at 2013 or has to
  carry an explicit "no usage" branch for a third of the archive. Era-bucketed anchors
  already establish the pattern for handling this honestly.
- **The 2016 "drop" is not a drop.** The count of players with usage is flat at roughly
  2,200–2,800 every season from 2013 on; what doubled in 2016 is the number of players the
  API returns *at all*. Usage covers the rotation, not the roster.
- **So usage is a starters-and-rotation signal**, ~2,500 players a season. That is a
  constraint, not a defect — it is exactly the population whose opportunity we are trying
  to model — but a feature present for 30% of rows cannot be fed to a model as if missing
  means zero. Missing means *unknown*, and for pre-2013 it means unknowable.
- **A payload rebuilt from game rows (`rebuilt_from_games: true`) has no usage at all**, so
  coverage and stat recovery interact: the players the ratings just recovered are precisely
  the ones this signal cannot reach.

**Why it matters most for defense.** Snap share is the closest thing to a touch count a
defender has. A corner who played 85% of snaps and was thrown at four times is a different
player from one who played 30% and was thrown at four times, and today they score the same.

### 2. Then the defensive rating itself

§4e of the model doc is blunt: defensive ratings need reworking before their projections
mean much. Coverage denial (v4.2) and the secondary archetypes were the first pass; snap
share is the input that pass could not have. Do not build more projection machinery on top
of a defensive rating that has not been fixed.

### 3. Then, in the model doc's order

- **EA blocking grades as the OL input** (§6 Option 1). Partly overtaken: the OL player rating
  is now withdrawn entirely and the line is rated as a unit, so there is no "old value" to sit
  beside. EA's OL overall still ships as a labelled cross-check column for 2026. Whether to
  build an EA-derived per-player blocking number for 2026 alone remains open, and remains
  un-backtestable by construction.
- **A talent prior for the production-blind majority** (Option 2). Two thirds of a roster
  has no production to measure; for them a scouting opinion beats a stale recruiting grade.
  Run it as an experiment under `projection_source`, never as the default, and let 2026 be
  the backtest we currently lack. It must never leak into an *earned* rating.
- **Production and Talent as two published numbers** (Option 3). The category error at the
  root: a backup would read `Production 41 · Talent 78` instead of a misleading single 54.
  Only worth the UI cost if 1 and 2 show the two signals really are distinct.
- ~~**Top-end survivorship** (Option 5)~~ — **done in v4.3.** `/draft/picks` supplied the
  departure data. Measured: 22.1% of top-production-decile players leave against 0.7% in the
  bottom half. Fed to the model as `cohort_departure_rate`. It does not improve MAE (8.19 ->
  8.22 offence) and was shipped anyway, because a disclosed limitation with a number beside it
  is a different thing from one without.

---

## Parked, with a reason

### Roster construction research

How winning rosters actually get built: recruiting vs the transfer portal vs in-house
development. Three scripts, in order:

1. `transfer_roi.py` — script 14's hit-rate method, pointed at transfers instead of
   recruiting classes.
2. `player_development.py` — expected vs actual outcome from a player's entry profile
   (stars, size, position), across the full 18 years. **Boise State 2010–2011 is the
   calibration case**: multiple 2–3 star recruits and walk-ons who were drafted.
3. `roster_composition.py` — regress script 13's team performance residual on acquisition
   pathway mix.

**Blocked on NIL**, which is a required input and has no working source (below).

### Playoff prediction

Predict the 2026 12-team playoff, and run the identical pipeline on 2024 and 2025 to show
how it *would* have done. Game-level win probability → Monte Carlo the full schedule
(~10k runs) → conference champions → bracket → title odds. Do **not** predict standings
directly.

Features all exist today: team rating differential, projected roster strength, returning
production share, portal net movement, three-year recruiting composite, home/away/neutral
plus rest and travel, and script 13's performance residual (which captures programs that
persistently beat their talent).

The backtest is the credibility gate: strictly out-of-sample (predicting 2024 trains only
on ≤2023), reporting Brier score, log loss, a calibration curve, and how many actual
playoff teams landed in the top 12. Publish the misses beside the hits.

**Numbering:** these are scripts **17, 18, 19** — `16_project_ratings.py` already exists,
and an older design note that called them 16/17/18 predates it.

**State in the UI:** no injury data (one quarterback injury swings a season), coaching
changes too thin to use (22 seed rows), preseason projections inherently noisy.

### Data sources we do not have

- **NIL.** On3 blocks scraping; script 04 produces 0 rows and Engine B runs recruiting-only
  as a result. Spotrac is a candidate and has not been investigated. Hard dependency of the
  roster-construction thread.
- **Pre-2016 defensive detail.** No hurries or pass breakups before 2015, so DL and EDGE
  ratings for 2008–2015 are recruiting-caliber estimates rather than production ratings,
  and classic-era thresholds sit at 75% of modern to compensate. A Sports Reference scraper
  would fix it.
- ~~**Draft and departure data**~~ — harvested. 4,858 picks 2008-2026, 83.7% joining to our
  players.
- **Headshots.**

---

## Known debt

Small, real, and each one has bitten at least once.

- **`players_{season}.json` is ~8 MB and `trajectory_detail.json` 5.5 MB.** Both want a
  slim/detail split — the grid needs a dozen fields per player and downloads all of them.
- **Script 07's distribution bounds for K and P are stale**, and now cry wolf every season.
  v4.2 deliberately pulled specialists down — EA has 5 kickers at 85+ and 1 punter, we had 17
  and 38 — but the validator still wants `mean 55–70`, and the shipped distribution means
  ~50. The old bounds are what let a punter outrank his own team's receivers without tripping
  anything, so they should be re-derived from the intended design (an average specialist is an
  average player; the ceiling is ~88–90). **Not done in this pass on purpose:** changing a gate
  in the same commit that ships the output it judges is how goalposts move. Do it as its own
  change, with the reasoning written down.
- **`research_cache` is empty**, so `data/research/index.json` exports `[]` and the site's
  Published Findings section shows its empty state permanently.
- **`team_advanced_games.json` is 107 MB.** Harvested for completeness and currently unread; the
  season-level file is what the ratings use. Slim it or drop it if it never finds a consumer.
- **Coordinator changes are gone.** `/coaches` is head coaches only, and the retired seed CSV had
  OC/DC rows. Script 10's coaching-change flag is now HC-only, which is a narrower signal than
  before even though it covers 2,584 coach-seasons instead of 20.
- **`player_seasons.year` is not a class year** and never was — it is constant across a
  career for 84% of players with three or more seasons, and holds an outright calendar year
  for 114,612 of 269,552 rows. Script 15 derives a real one; script 12 still exports the raw
  value, so player cards can misstate class.
- **Projections overstate stability at the very top.** 60% of 90+ players are projected to
  decline; 86% actually do. Stated in the UI, not fixed.
- **Stale `SUPABASE_*` keys in `.env`** with nothing reading them. Supabase was retired in
  Aug 2026; `psycopg2`, `supabase` and `DATABASE_URL` are gone and must not come back.
- **GitHub Pages source branch unconfirmed** (no `gh` CLI on this machine). `clean-arch` is
  kept synced to `main` as insurance. A stale `local-arch` branch is unrelated to this work.

### Fixed in this pass, noted so it is not "fixed" again

- Script 07 re-read all of `data/raw` — 255 MB of stats alone — once per position per
  season, which is 228 full parses on a `--all-seasons` run. Tables are now read once per
  process and the stats index built once.
- Script 07's K/P distribution bounds were stale and warned on every run. Re-derived in v4.3 as
  its own change, separately from the ratings they judge.
- `player_seasons.year` is still not a class year and script 12 still exports the raw value.
