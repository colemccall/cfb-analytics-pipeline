# Alternatives — what we could compute instead

*Written 2026-08-12. A companion to `FORMULAS.md`, which describes what we compute today.
Nothing here is built. Each option carries what it needs, what it costs, and — the part that
matters — **how we would know whether it worked**.*

An option with no test attached is an opinion. Every entry below has to name the measurement
that would settle it, because this project's standing rule is that distribution shape is a hard
gate and a model that cannot show its backtest does not ship.

**Status key:** ✅ recommended · 🔬 worth an experiment · ⛔ rejected, with reason ·
🚧 blocked on data

---

## A. Offensive line

The problem, from `FORMULAS.md` §2: 55% of the formula reads keys that do not exist, so the
live rating is `0.25 + 0.30·recruiting + 0.10·experience + 0.05·award`, recruiting is 67% of
the only signal that varies, and agreement with EA is **−0.274**.

**The constraint that rules out most ideas:** there is no per-lineman blocking data anywhere in
the API — no pancakes, no sacks allowed, no pressures allowed. Verified by a full key scan; see
`API_INVENTORY.md`. Any option that needs individual blocking production is not merely
expensive, it is impossible.

| # | Option | Needs | Test | Status |
|---|---|---|---|---|
| A1 | **Withdraw the player rating; publish a line-*unit* rating** built from `lineYards`, `stuffRate`, `powerSuccess`, `secondLevelYards`, sack rate allowed | `/stats/season/advanced` — **confirmed back to 2010** (120 teams) | Unit rating should correlate with team rushing success and with EA's *average* OL rating per team. If it does not beat our current OL average on that, it is no better. | ✅ |
| A2 | Allocate the unit rating to individuals by snaps or starts | snap data (2013+, ~2,500 players/yr) | — | ⛔ Invents per-player variance that does not exist. It would look like a player rating and be a team rating with noise on top. |
| A3 | **EA blocking composite for 2026**, labelled, alongside — never replacing — the withdrawn number | `ea_ratings.json`; 1,278 of 1,534 linemen matched (83%) | `passBlock` vs `runBlock` correlate 0.53, so they carry independent information. Judge against 2026 outcomes when the season is played. | 🔬 One season only; not backfillable; cannot be backtested by construction. |
| A4 | Keep one OL overall, fix the inputs by joining team stats properly | `team_season_stats.json` (on disk) | — | ⛔ Rejected by the user. It would still be unit quality × recruiting wearing a player's name. |
| A5 | **Draft outcome as a validation target** — do the linemen we rate highly get drafted? | `/draft/picks` — **confirmed 2010–2024, ~255/yr** | Rank correlation between our OL ordering and draft position. This is not a rating; it is the only external check on OL we will ever have. | ✅ |

**Recommended combination:** A1 + A5 now, A3 as a labelled experiment. The user's decision to
withdraw the per-player number stands; A1 is what makes that withdrawal constructive rather
than merely honest, because it replaces a fake number with a real one at the level the data
actually supports.

**Consequence to handle:** `avg_top()` in script 10 returns a hard-coded 50.0 for an empty
position, and OL is 40% of run offence. Withdrawing OL ratings without renormalising — or
without substituting the A1 unit rating — turns 40% of every team's run offence into a constant.

---

## B. Defense

The problem, from `FORMULAS.md` §3: tackles correlate **0.70–0.82** with the rating at every
position, and a tackle count is mostly snaps × opponent run rate × time on the field. A defence
that gets off the field denies its own players the statistic we reward them for.

### B1–B2. The free wins

| # | Option | Needs | Test | Status |
|---|---|---|---|---|
| B1 | **Split solo from assisted**: `SOLO·w₁ + (TOT − SOLO)·w₂`, `w₂ < w₁` | `defensiveSOLO` — on disk, all 402,156 rows, currently unread | Within-position Spearman vs EA must not fall; the front seven should improve if made plays matter more than proximity. | ✅ |
| B2 | **Credit takeaways**: forced fumbles and recoveries into ball-hawk (secondary) and run-stop (front seven) | `fumblesFUM`, `fumblesREC` — on disk, 42,328 rows | Same EA check. A strip-sack currently scores only the sack, which is plainly incomplete. | ✅ |

Both are universal across all 18 seasons and need no new data. They are the cheapest honest
improvements available anywhere in the system.

### B3–B6. Giving tackles a denominator

This is the substantive fix, and the options differ in what they divide by.

| # | Denominator | Needs | Trade-off |
|---|---|---|---|
| B3a | Opponent **rushing attempts faced** | `/games/teams` `rushingAttempts` — **already cached** | Most direct for run-stop stats; says nothing about coverage snaps |
| B3b | **Defensive plays faced** | `/stats/season/advanced` `defense.plays` — confirmed 2010+ | Universal, simple, available for the whole archive |
| B3c | **Defensive time of possession** | `possessionTime` — already cached, season-level already computed | Captures "our offence never let us rest"; noisier than play counts |
| B4 | **Havoc share** — the player's disruption ÷ his unit's havoc, using `havoc.frontSeven` / `havoc.db` | `/stats/season/advanced` | Elegant: measures share of the unit's disruption rather than raw volume, and the front-seven/DB split matches our position groups almost exactly |
| B6 | **Per-snap**, where snap share exists | `snap_pct` — on disk, **zero before 2013**, ~2,500 players/yr after | Strictly better where present; needs an explicit "unknown" branch, never a zero |

**Recommendation:** B3b as the primary denominator (universal, whole archive), with B4 layered
on for the secondary and front seven because it answers a different question — *how much of
what your unit did was you*.

**Test for all of them:** the distribution gate (`scripts/validate_ratings.py`), EA
within-position Spearman, and a specific check that defenders on bad defences stop being
over-rated — measure the correlation between team defensive quality and individual OVR before
and after. If that correlation does not fall, the denominator did not work.

### B5, B7, B8. The rest

| # | Option | Needs | Status |
|---|---|---|---|
| B5 | **Leverage weighting** — weight sacks, INTs and PBUs by down, distance, field position, score | `/plays/stats` (per-play context confirmed); ~2,450-call harvest | 🔬 Real, but earn the harvest first |
| B7 | **Shrink small-sample ratios** toward the position mean | nothing | ✅ `instinct = (INT+PBU)/max(TOT,1)` currently gives a player with one tackle and one breakup a perfect 1.0 |
| B8 | **Benchmark against `/wepa/players/*` and `/ppa/players/season`** | new endpoints | ✅ Not an input — an independent check on whether our ordering is sane. Note both are **offence-only**, so they cannot validate defence. |

### What is impossible, and should stop being wished for

- **Missed tackles** — not published anywhere.
- **Per-play tackle attribution** — `/plays/stats` defines a `Tackle` type but returns zero
  records of it across 2014, 2019 and 2024 samples.
- **QB hurry attribution per play** — same, zero records.
- **Targets allowed by a corner** — `Target` is attributed to the *receiver*, and only on
  incompletions. There is no defender-side coverage data.

The corner rating is therefore bounded no matter what we do. Coverage denial (v4.2) is a team
proxy standing in for an individual measurement, and it will stay one.

---

## C. The secondary's generic `DB` problem

48% of secondary players (1,397 of 2,889) come back from the API labelled only `DB`. They get
the even-thirds archetype weighting and a formula that is a copy of safety.

| # | Option | Needs | Status |
|---|---|---|---|
| C1 | **Resolve `DB` → CB/S using EA's position** for matched players | `ea_ratings.json` | ✅ 2026 only, but exact where it applies |
| C2 | Height/weight prior for the remainder | on disk | 🔬 Safeties are heavier; a simple classifier trained on players we *do* have labelled |
| C3 | Infer from behaviour — high INT+PBU and low tackles ⇒ corner | on disk | ⛔ **Circular.** It would assign the archetype using the same statistics the archetype then judges. |
| C4 | Leave unresolved; group in the UI only | — | ✅ Ships now, costs nothing, and is the current plan |

---

## D. The projection engine

Two distinct problems that pull in opposite directions (see `HOW_PROJECTIONS_WORK.md`):

- **P1, individual:** the raw acceleration feature produces artifact-driven penalties for
  interrupted careers — Mickey's raw acceleration is +120 against −13 healthy.
- **P2, systemic:** spread is compressed to 70–76% of reality, so decline is under-called at
  every level.

| # | Option | Addresses | Test | Status |
|---|---|---|---|---|
| D1 | Feed the **gap** `pct_accel − pct_accel_healthy` explicitly rather than both raw and healthy | P1 | Does Mickey's projection move toward his cohort baseline without the aggregate MAE worsening? | 🔬 |
| D2 | Suppress raw-path shape features entirely when `n_interrupted > 0` | P1 | Same, plus check the 658 interrupted careers as a group | 🔬 Blunter than D1 |
| D3 | Replace direction-blind `pct_sd` with a **monotonicity** measure | P1 | A steady climber and an oscillator with the same SD must score differently | 🔬 |
| D4 | **Career-context features** — own-program strength per season, transfer flag, destination-minus-origin strength | the user's explicit request | Does MAE improve for the ~4,300 players with a transfer in their history? | 🔬 Now buildable: `/ratings/*` for program strength, `transfers.json` for the move |
| D5 | **Model NFL departure explicitly** using draft data | P2, and §4d survivorship | Cohort curves currently condition on "stayed in college". With draft data the leavers can be modelled instead of silently dropped. | ✅ **Newly possible** — `/draft/picks`, 2010–2024 confirmed |
| D6 | Raise `VARIANCE_LAMBDA` toward 1.0 | P2 | Spread would match reality and decline rates would calibrate; MAE would get worse and interval coverage would need re-deriving | 🔬 A genuine trade, not a fix — the user's call |
| D7 | Separate the point estimate from the distribution: accurate headline, separately calibrated range and direction language | P2 | Both can be right at once | 🔬 More machinery; stops the trade-off being one dial |

**Sequencing note:** do not fix P1 and P2 with one lever. Anything that makes Mickey look better
by making the engine more optimistic makes the larger problem worse.

---

## E. Research findings

Detail in `RESEARCH_METHODS.md`; the options in brief.

| # | Option | Addresses | Status |
|---|---|---|---|
| E1 | **Residualize the recruiting-ROI metric** — expected peak OVR per *recruit* from his own composite, then aggregate the residual | Hit rate correlates **+0.27** with class recruiting strength, so today it measures recruiting, not development | ✅ The single highest-value fix on the research page |
| E2 | **Coaching-change event study** on script 13's residual | Residual persists (r = 0.607 at t+1) but persistence does not identify coaching | ✅ **Newly possible** — `/coaches` confirmed 2010–2024 with tenure and per-season SP+ |
| E3 | Add returning production and prior-year SP+ as covariates to script 13 | Separates "beat your talent" from "beat your talent this year" | 🔬 `/player/returning` confirmed 2016+ (zero in 2010) |
| E4 | **Empirical-Bayes shrinkage + intervals** on every ranked finding | Residual SD 9.82 across 2,310 team-seasons means the leaderboard top is substantially noise | ✅ One shared helper, serves every finding |
| E5 | Give hidden gems a denominator | Selection on outcome with no base rate | ✅ Trivial |
| E6 | **Validate ratings against NFL draft outcomes** | We have never had an independent, historical check | ✅ New finding, backtestable across 15 seasons |

---

## F. Data we still do not have

| What | Status |
|---|---|
| Coaching tenure | ✅ **Solved** — `/coaches`, 2010–2024, with per-season W/L, SRS and SP+ splits |
| NFL draft outcomes | ✅ **Solved** — `/draft/picks`, ~255/yr |
| Returning production | ✅ **Solved** — `/player/returning`, 2016+ |
| Line unit metrics | ✅ **Solved** — `/stats/season/advanced`, 2010+ |
| NIL | 🚧 No endpoint exists. Decide the target quantity — program-level spend is coarser than player valuations but sufficient for the roster-construction questions, and far more attainable. Survey public sources and check terms before proposing one. |
| Per-lineman blocking | ⛔ Does not exist |
| Missed tackles, coverage snaps, targets allowed | ⛔ Does not exist |

**Standing rule, unchanged:** NIL never enters an earned rating. Research and, if validated,
projections only — labelled through `projection_source`.
