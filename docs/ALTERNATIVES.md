# Alternatives — what we could compute instead

*Written 2026-08-12. **Revised the same day, after building everything marked recommended.***

A companion to `FORMULAS.md`, which describes what we compute today. Each option carries what it
needs, what it costs, and — the part that matters — **how we would know whether it worked**.

An option with no test attached is an opinion. Every entry below names the measurement that
would settle it, because this project's standing rule is that distribution shape is a hard gate
and a model that cannot show its backtest does not ship.

**Status key:** 🚀 shipped · ✅ recommended, not yet built · 🔬 worth an experiment ·
⛔ rejected, with reason · 🚧 blocked on data

---

## What happened when the recommendations were built

Every ✅ in the first draft of this document has now been implemented. Most worked. Two did not
survive contact with their own tests, and those are the interesting rows.

| Option | Predicted | Measured | Outcome |
|---|---|---|---|
| **B4 havoc share** | "elegant — measures share of the unit's disruption" | replacing each unit's havoc with one shared constant scored **better** (+0.0019) than the real denominator (+0.0011) | **Rejected on its own evidence.** The credit was re-weighting stats the composite already counted. Computed and published; scored at zero. |
| **A1 line-unit rating** | should beat the old OL average | +0.179 vs linemen drafted; mean rating 65.2 → 70.8 → 77.1 by picks. The withdrawn rating scored **−0.274** vs EA. | **Shipped** — but pooled bounds drifted 25 points across the archive on the first run, fixed with era buckets |
| **D5 model NFL departure** | fixes §4d survivorship | 22.1% of top-decile players depart against 0.7% in the bottom half; **MAE unchanged** (8.19 → 8.22) | **Shipped** for honesty, not accuracy |
| B3b opportunity denominator | the substantive defensive fix | +0.0085 mean Spearman vs EA; shuffled placebo **−0.0025** | **Shipped** — passes the placebo test |
| B1 solo/assist split | free, universal | aggregate-neutral by construction (0.988) | **Shipped** 2013+ |
| B2 fumble credit | "forced fumbles and recoveries" | `fumblesFUM` turned out to be fumbles **committed** | **Shipped, corrected** — recoveries only |
| B7 shrink small-sample ratios | fixes `instinct = 1.0` | 5,848 player-seasons had a ratio ≥ 1.0 against a ceiling of 0.3 | **Shipped** — the feature had been a constant for most of the pool |
| E1 residualize recruiting ROI | separates development from recruiting | correlation with class recruiting **+0.254 → −0.028** | **Shipped** — worked exactly as intended |
| E2 coaching event study | the decisive test for finding 13 | coach carry-over r = **+0.343** across 101 coaches | **Shipped** |
| E4 shrinkage + intervals | every ranked finding | applied to 2,310 team-seasons and 2,700 classes | **Shipped** |
| E5 gems base rate | trivial | — | **Shipped** |
| E6 draft validation | first real backtest | calibration monotone: 90+ → 66.4% drafted, under 70 → 1.8% | **Shipped** |
| C4 group CB/S/DB in the UI | costs nothing | 1,397 players had been invisible to every filter | **Shipped** — it was a live bug |

The havoc-share result is the one worth remembering: an idea that is obviously right, whose
mechanism is clearly stated, and which measurably does nothing. It ships as a published number
with zero weight rather than being deleted, because the measurement is real even though the
credit was not.

---

## A. Offensive line

The problem, from `FORMULAS.md` §2: 55% of the formula read keys that do not exist, so the live
rating was `0.25 + 0.30·recruiting + 0.10·experience + 0.05·award`, recruiting was 67% of the
only signal that varied, and agreement with EA was **−0.274**.

**The constraint that rules out most ideas:** there is no per-lineman blocking data anywhere in
the API — no pancakes, no sacks allowed, no pressures allowed. Verified by a full key scan; see
`API_INVENTORY.md`. Any option that needs individual blocking production is not merely
expensive, it is impossible.

| # | Option | Needs | Test | Status |
|---|---|---|---|---|
| A1 | **Withdraw the player rating; publish a line-*unit* rating** from `lineYards`, `stuffRate`, `powerSuccess`, `secondLevelYards`, sack rate allowed | `/stats/season/advanced` — **confirmed back to 2008**, all 2,295 FBS team-seasons | Validated against draft outcomes, which is stronger than the team-rushing check originally proposed: **+0.179** Spearman with linemen drafted off that season, and mean rating 65.2 / 70.8 / 77.1 for zero / one / two picks. | 🚀 v4.3 |
| A2 | Allocate the unit rating to individuals by snaps or starts | snap data (2013+) | — | ⛔ Invents per-player variance that does not exist. It would look like a player rating and be a team rating with noise on top. |
| A3 | **EA blocking composite for 2026**, labelled, alongside — never replacing — the withdrawn number | `ea_ratings.json`; 1,278 of 1,534 linemen matched (83%) | `passBlock` vs `runBlock` correlate 0.53, so they carry independent information. Judge against 2026 outcomes when the season is played. | 🔬 One season only; not backfillable; cannot be backtested by construction. EA's OL overall is already exported as a cross-check column. |
| A4 | Keep one OL overall, fix the inputs by joining team stats properly | `team_season_stats.json` (on disk) | — | ⛔ It would still be unit quality × recruiting wearing a player's name. |
| A5 | **Draft outcome as a validation target** | `/draft/picks` — 4,858 picks 2008–2026, 4,010 joining to our players (83.7%) | `scripts/validate_vs_draft.py`. Calibration is monotone across every band. | 🚀 v4.3 |

**The consequence, handled.** `avg_top()` in script 10 returned a hard-coded 50.0 for an empty
position, and OL is 40% of run offence — withdrawing OL ratings without renormalising would have
turned 40% of every team's run offence into a constant. `avg_top()` now returns `None` and the
weights renormalise, and the rule is universal rather than an OL special case: a team with no
rated kicker no longer gets a fabricated 50 for special teams either.

**A bug this shipped with for exactly one run.** Bounds pooled across all eighteen seasons made
the median line rating climb from 52 in 2008 to 77 in 2023. That is not improvement — median
line yards jump 2.885 → 3.095 between 2020 and 2021 and stuff rate drops 0.199 → 0.165 in the
same step. A 7% jump and a 17% drop between two consecutive seasons is a provider changing a
definition, not 130 teams simultaneously learning to block. Bounds are now bucketed into three
eras (2008–13, 2014–20, 2021+), each calibrated on its own p10/p90. Within an era they remain
fixed absolute constants, so the guarantee in `AUDIT_FINDINGS.md` §9 still holds.

The era breaks here are **not** the same as script 07's `ERA_ANCHORS` (2013, 2018), and should
not be forced to agree: EDGE's buckets track when defensive stats became available, these track
when the advanced-stats endpoint changed how it computes line play.

---

## B. Defense

The problem, from `FORMULAS.md` §3: tackles correlated **0.70–0.82** with the rating at every
position, and a tackle count is mostly snaps × opponent run rate × time on the field. A defence
that gets off the field denies its own players the statistic we reward them for.

### B1–B2, B7. The free wins

| # | Option | Needs | Result | Status |
|---|---|---|---|---|
| B1 | **Split solo from assisted**: `SOLO·1.25 + (TOT − SOLO)·0.65` | `defensiveSOLO` — on disk, **2013+ only** | Calibrated to be aggregate-neutral: solo tackles are 56.4% of the total, so 0.564×1.25 + 0.436×0.65 = 0.988. The average defender's credit does not move; only the mix does. Zero rows carry the field before 2013, so the classic era degrades to plain totals — the code asks the data rather than hardcoding a year. | 🚀 v4.3 |
| B2 | **Credit fumble RECOVERIES** | `fumblesREC` — 8,495 nonzero defensive game rows | **Correction to the original recommendation.** `fumblesFUM` is not a forced fumble: on a defensive row it is a fumble the player COMMITTED. Only 974 rows carry one, 84% in games where he also had a return, an interception or a recovery, and 455 also carry `fumblesLOST`. Crediting it would have paid a corner for coughing up an interception return. Forced fumbles are not published per player anywhere. | 🚀 v4.3, corrected |
| B7 | **Shrink small-sample ratios** toward the position mean | nothing | `(events + 12·prior) / (tackles + 12)`. Before: CB instinct p90 **2.000**, p99 5.000, max 11.0 — against a normalisation ceiling of 0.3, meaning 5,848 player-seasons clipped to a perfect 1.0 and the feature was a constant for most of the pool. After: p90 0.315, p99 0.546. Bounds re-derived. | 🚀 v4.3 |

### B3–B4. Giving tackles a denominator

| # | Denominator | Result | Status |
|---|---|---|---|
| B3b | **Defensive plays faced**, as a clipped index `clip(median_pg / this_defence_pg, 0.85, 1.20)` | **+0.0085** mean within-position Spearman vs EA across the six defensive groups. The same values **shuffled across teams score −0.0025** — below doing nothing at all. That is the signature of real information, and it is the same test the coverage credit passed in v4.2. | 🚀 v4.3 |
| B4 | **Havoc share** — the player's disruption ÷ his unit's havoc | **Failed.** Real denominator +0.0011; replacing every unit's havoc with one shared constant **+0.0019**. A denominator that performs worse than a constant is not a denominator — the credit was re-weighting tackles for loss, passes defensed and fumble recoveries, all of which the composite already counts. Computed, stored and displayed; `HAVOC_CREDIT = {}`. | ⛔ Published, not scored |

Deliberately gentle and clipped rather than a straight rate: dividing outright would make
snaps-faced the dominant term, and plays faced is not purely a defensive virtue — a fast-tempo
offence puts its own defence back on the field.

### B5, B6, B8. The rest

| # | Option | Needs | Status |
|---|---|---|---|
| B5 | **Leverage weighting** — weight sacks, INTs and PBUs by down, distance, field position, score | `/plays/stats` (per-play context confirmed); ~2,450-call harvest | 🔬 Real, but earn the harvest first |
| B6 | **Per-snap rates** where snap share exists | `snap_pct` — on disk, zero before 2013, ~2,500 players/yr after | 🔬 **The next phase.** Strictly better where present; needs an explicit "unknown" branch, never a zero |
| B8 | **Benchmark against `/wepa/players/*` and `/ppa/players/season`** | now harvested — 9,044 wepa rows | ✅ Not an input, an independent check. Both are **offence-only**, so they cannot validate defence |

### What is impossible, and should stop being wished for

- **Missed tackles** — not published anywhere.
- **Per-play tackle attribution** — `/plays/stats` defines a `Tackle` type but returns zero
  records of it across 2014, 2019 and 2024 samples.
- **QB hurry attribution per play** — same, zero records.
- **Targets allowed by a corner** — `Target` is attributed to the *receiver*, and only on
  incompletions. There is no defender-side coverage data.

The corner rating is therefore bounded no matter what we do. Coverage denial is a team proxy
standing in for an individual measurement, and it will stay one.

---

## C. The secondary's generic `DB` problem

48% of secondary players (1,397 of 2,889) come back from the API labelled only `DB`.

| # | Option | Needs | Status |
|---|---|---|---|
| C1 | **Resolve `DB` → CB/S using EA's position** for matched players | `ea_ratings.json` | 🔬 2026 only, but exact where it applies |
| C2 | Height/weight prior for the remainder | on disk | 🔬 Safeties are heavier; a simple classifier trained on players we *do* have labelled |
| C3 | Infer from behaviour — high INT+PBU and low tackles ⇒ corner | on disk | ⛔ **Circular.** It would assign the archetype using the same statistics the archetype then judges. |
| C4 | **Group CB/S/DB behind one filter, keep three badges** | UI only | 🚀 v4.3. `CONFIG.POSITIONS` omitted DB entirely, so 1,397 players were invisible to every position filter — a live bug, not a grouping preference. One `matchesPosition()` helper now serves every call site. |

---

## D. The projection engine

Two distinct problems that pull in opposite directions (see `HOW_PROJECTIONS_WORK.md`):

- **P1, individual:** the raw acceleration feature produces artifact-driven penalties for
  interrupted careers — Mickey's raw acceleration is +120 against −13 healthy.
- **P2, systemic:** spread is compressed to 70–77% of reality, so decline is under-called at
  every level.

| # | Option | Addresses | Test | Status |
|---|---|---|---|---|
| D1 | Feed the **gap** `pct_accel − pct_accel_healthy` explicitly rather than both raw and healthy | P1 | Does Mickey's projection move toward his cohort baseline without the aggregate MAE worsening? | 🔬 |
| D2 | Suppress raw-path shape features entirely when `n_interrupted > 0` | P1 | Same, plus check the 658 interrupted careers as a group | 🔬 Blunter than D1 |
| D3 | Replace direction-blind `pct_sd` with a **monotonicity** measure | P1 | A steady climber and an oscillator with the same SD must score differently | 🔬 |
| D4 | **Career-context features** — own-program strength per season, transfer flag, destination-minus-origin strength | the user's explicit request | Does MAE improve for the ~4,300 players with a transfer in their history? | 🔬 Now buildable: `/ratings/*` harvested, `transfers.json` on disk |
| D5 | **Model NFL departure explicitly** | P2, and §4d survivorship | **Measured:** 22.1% of top-production-decile players depart against 0.7% in the bottom half; 3.8% overall. **MAE unchanged** — 8.19 → 8.22 offence, 8.45 → 8.44 defence, both inside the noise. Shipped anyway: a disclosed limitation with a number beside it is a different thing from one without, and the model can now see how selected its own training population is. | 🚀 v4.3 |
| D6 | Raise `VARIANCE_LAMBDA` toward 1.0 | P2 | Spread would match reality and decline rates would calibrate; MAE would get worse and interval coverage would need re-deriving | 🔬 A genuine trade, not a fix |
| D7 | Separate the point estimate from the distribution | P2 | Both can be right at once | 🔬 More machinery; stops the trade-off being one dial |

| D8 | **Scale the interval width per position by `sqrt(1 − reliability)`** | P2, and the fact that one band per family claims equal precision for unequal measurements | Per-position coverage against the 80% target | ⛔ **Rejected, v4.5.** It fixed the two positions it was designed for and broke four others: CB 72.8% → 80.1% and DB 76.1% → 80.8%, but S 75.5% → 68.8%, LB 84.0% → 72.6%, TE 83.1% → 93.5%, QB 77.1% → 73.7%. Mean \|coverage − 80\| across positions went **3.2 → 5.9 points**. Reliability bounds what a rating can *know*; it does not describe how a projection of it *errs*, and those are different quantities however closely related they sound. |
| D9 | **Residual quantiles per (position, rating bucket)** instead of per family | the same problem, asked of the data directly | Same gate | 🚀 **v4.5.** Defence mean \|coverage − 80\| **4.2 → 2.3 points**; CB 72.8% → 80.4%, LB 84.0% → 79.0%, DL 84.6% → 82.7%. Offence unchanged at 1.7, which is the right outcome — it was already calibrated. Falls back to the family band when a cell holds fewer than 60 rows. |
| D10 | **Reliability-weighted blend of a season with the player's own career**, on the COMPOSITE rather than the shipped OVR | P1, and the v4.4 result that contradicted its own theory | Does the sign of the reliability/benefit correlation flip? | ✅ **Revived, v4.5 — recommended, not yet in the engine.** v4.4 measured the correlation at **−0.67** and called it inconclusive, but it was run on the OVR, which the playing-time tiers have already shrunk toward a recruiting prior — blending a number toward a prior it partly contains is not the experiment. On the unshrunk composite the sign flips to **+0.385** and the blend helps at every position, most where reliability is lowest: CB −0.0264 MAE, DB −0.0263, S −0.0228, against RB −0.0055 and QB −0.0058. |

**A trap worth recording.** The departure feature returned exactly 0.0% on its first run, for
every cohort cell. The cause: it was built from the same `train_mask` every other cohort
statistic uses, and that mask requires a next season — which a player who left for the NFL does
not have. It was measuring departure among the players who did not depart. Cohort statistics and
departure statistics need different denominators, and the resemblance between them is the trap.

**Sequencing note:** do not fix P1 and P2 with one lever. Anything that makes Mickey look better
by making the engine more optimistic makes the larger problem worse.

---

## E. Research findings

Detail in `RESEARCH_METHODS.md`; the options in brief.

| # | Option | Addresses | Result | Status |
|---|---|---|---|---|
| E1 | **Residualize the recruiting-ROI metric** — expected peak OVR per *recruit* from his own composite, then aggregate the residual | Hit rate correlated **+0.27** with class recruiting strength, so it measured recruiting, not development | Correlation with class recruiting falls from **+0.254 to −0.028**. Expected peak OVR = 54.60 × composite + 21.32 (n=37,462, R²=0.063); hit rate banded, 27.9% below 0.80 rising to 61.9% above 0.97. | 🚀 v4.3 |
| E2 | **Coaching-change event study** on script 13's residual | Residual persists (r = 0.607 at t+1) but persistence does not identify coaching | 303 changes with ≥2 rated seasons each side. Median step **−1.89**; only 40.6% improved. Step SD 9.15 against a residual SD of 9.82. **Coach carry-over r = +0.343** across 101 coaches with two stints. | 🚀 v4.3 |
| E3 | Add returning production and prior-year SP+ as covariates to script 13 | Separates "beat your talent" from "beat your talent this year" | `/player/returning` now harvested, 1,691 rows 2014+ | 🔬 |
| E4 | **Empirical-Bayes shrinkage + intervals** on every ranked finding | Residual SD 9.9 across 2,310 team-seasons means the leaderboard top is substantially noise | `utils/shrinkage.py`: `shrink_mean` for continuous, `shrink_rate` for proportions, both with 80% intervals matched to the projection bands. | 🚀 v4.3 |
| E5 | Give hidden gems a denominator | Selection on outcome with no base rate | Base rates for 1–2★, 3★ and 4–5★ now lead the finding | 🚀 v4.3 |
| E6 | **Validate ratings against NFL draft outcomes** | We had never had an independent, historical check | Drafted mean peak OVR 82.8 vs 62.8 undrafted. Calibration monotone across all six bands. Within-position agreement with draft order: offence 0.42–0.49, defence 0.13–0.25. | 🚀 v4.3 |

**The defence/offence gap in E6 is worth staring at.** Our offensive ratings order draft picks at
0.42–0.49; our defensive ratings manage 0.13–0.25. That is consistent with everything else known
about the defensive ratings and is the clearest single argument for the next phase.

---

## F. Data we still do not have

| What | Status |
|---|---|
| Coaching tenure | 🚀 **Harvested** — `/coaches`, 2,584 coach-seasons 2008–2026, with per-season W/L, SRS and SP+ splits |
| NFL draft outcomes | 🚀 **Harvested** — `/draft/picks`, 4,858 picks, 83.7% joining to our players |
| Returning production | 🚀 **Harvested** — `/player/returning`, 2014+ |
| Line unit metrics | 🚀 **Harvested** — `/stats/season/advanced`, 2008+ (the survey said 2010; the harvest found 2008) |
| Betting lines, pregame WP, CFP field | 🚀 **Harvested** — the playoff model's benchmarks and ground truth are on disk before the model exists |
| Per-player play counts | 🚀 **Harvested** — `/stats/player/success`, offence only, 2014+ |
| Opponent-adjusted EPA | 🚀 **Harvested** — `/wepa/players/*`, offence only, sparse |
| NIL | 🚧 No endpoint exists. Decide the target quantity first — program-level spend is coarser than player valuations but sufficient for the roster-construction questions, and far more attainable. Survey public sources and check terms before proposing one. |
| Per-lineman blocking | ⛔ Does not exist |
| Missed tackles, coverage snaps, targets allowed | ⛔ Does not exist |
| Coordinator-level coaching changes | ⛔ `/coaches` is head coaches only. The retired seed CSV had OC/DC rows; the API does not. |

**Standing rule, unchanged:** NIL never enters an earned rating. Research and, if validated,
projections only — labelled through `projection_source`.
