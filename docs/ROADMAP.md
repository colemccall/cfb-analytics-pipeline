# Roadmap

*Written 2026-08-12, after the v4.2 rating pass shipped.*

What is next, why it is next, and what is parked. Everything here is either already
decided or already measured — the file exists so none of it has to be re-derived from
scratch, and so that "we thought about that" is checkable rather than remembered.

Read `RATING_AND_PROJECTION_MODEL.md` first if you have not. It is the honest account of
what the ratings are and where they break; this file is what we intend to do about it.

---

## The one rule that orders everything below

**A model that cannot show its backtest does not ship.**

That single rule is why the order below is not "most exciting first". We hold exactly one
season of EA Sports CFB 27 data, so every EA-based idea is unvalidated *by construction*
until CFB 28 exists (~mid-2027) and gives us one year-over-year pair. Usage data is
historical, so it can be tested properly today. Usage therefore goes first, even though
the EA ideas are more interesting.

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

- **EA blocking grades as the OL input** (§6 Option 1). OL is the only position with no
  individual measurement anywhere — the current number is 77% recruiting, capped at 88
  because the composite saturates. Defensible without a backtest, because the claim is not
  "EA predicts better" but "we have nothing individual at all". Ship it labelled, keep the
  old value beside it for a season, compare when 2026 is played.
- **A talent prior for the production-blind majority** (Option 2). Two thirds of a roster
  has no production to measure; for them a scouting opinion beats a stale recruiting grade.
  Run it as an experiment under `projection_source`, never as the default, and let 2026 be
  the backtest we currently lack. It must never leak into an *earned* rating.
- **Production and Talent as two published numbers** (Option 3). The category error at the
  root: a backup would read `Production 41 · Talent 78` instead of a misleading single 54.
  Only worth the UI cost if 1 and 2 show the two signals really are distinct.
- **Top-end survivorship** (Option 5). Cohort curves condition on "stayed in college", so
  the players who leave for the NFL are missing from exactly the top of the distribution.
  Needs draft/departure data we do not have.

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
- **Draft and departure data.** Needed for survivorship (above).
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
