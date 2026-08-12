# Design review — what the numbers are actually built from

*2026-08-12. Evaluation and planning only; nothing here has been implemented.*

> **⚠ Superseded the same day by a systematic API survey. Read the successors instead:**
> `FORMULAS.md`, `ALTERNATIVES.md`, `HOW_PROJECTIONS_WORK.md`, `RESEARCH_METHODS.md`,
> `API_INVENTORY.md`.
>
> Three conclusions below are **wrong**, and wrong in the same direction — they assume data
> does not exist because we were not already fetching it:
>
> - "The decisive test is a coaching-change event study … **blocked**" — it is not.
>   `/coaches` covers 2010–2024 with full tenure and per-season SP+, matching our schools 100%.
> - "There is no individual OL signal, so only a team proxy is possible" — still true per
>   *player*, but `/stats/season/advanced` carries `lineYards`, `stuffRate` and `powerSuccess`
>   back to 2010, so a real line-**unit** rating is possible.
> - Survivorship at the top of the scale was described as something to disclose. `/draft/picks`
>   joins to our players at 94.5%, so NFL departure can be modelled instead.
>
> Kept because the measurements in it — the OL arithmetic, the tackle correlations, the residual
> persistence, the recruiting-ROI confound — are sound and are what prompted the survey. The
> lesson recorded: **do not infer an absence from our own ignorance.**

Four threads: the offensive line and defense, the projection's treatment of interrupted
careers, the research findings, and how to approach NIL / transfers / the playoff model.

Every number below was measured against the current data during this review. Where a claim is
an opinion rather than a measurement, it says so.

The recurring theme, stated once: **several of our numbers are honest computations of the wrong
quantity.** They are not bugs. They are metrics whose definition does not match the label on
the page, and no amount of tuning fixes that — only redefinition does.

---

## 1. Offensive line and defense

### 1a. The OL rating is a recruiting rank wearing a costume

**What it computes today** (`POSITION_WEIGHTS["OL"]`, script 07):

| Input | Weight | Varies per player? |
|---|---:|---|
| `team_rush_ypa` | 0.30 | **No** — team value |
| `team_sack_rate_inv` | 0.25 | **No** — team value |
| `recruit_composite` | 0.30 | Yes |
| `experience` (class year) | 0.10 | Yes |
| `award_tier` | 0.05 | Yes, but ~0 for almost everyone |

55% of the formula is a **team constant**. Every lineman on a roster receives the identical
value for it. So *within a team*, the OL ordering is recruiting composite, nudged by class year.
That is the whole mechanism.

**Measured consequences:**

- r = **0.877** with recruiting composite (77% of variance). Every other position is under 9%.
- Within-position Spearman against EA CFB 27: **−0.274**. Negative. We disagree with the only
  independent assessment available, in the wrong direction.
- 2025 distribution: p90 = p99 = max = **80.0**. The composite saturates; the top tenth of
  linemen share one number. Zero players at 85+.
- OL has **no EDGE rows at all** — script 06 never computes one, because there is no individual
  production to compute it from. There never was an individual signal here.

**The honest description:** this is a team run-game quality index multiplied by a recruiting
rank. Calling it a player rating is the error, and the 88 cap is a bandage over it.

**What is available:**

| Option | What it buys | What it costs |
|---|---|---|
| **A. EA blocking composite** | 9 per-lineman grades. 1,534 EA linemen, **1,278 matched (83%)**. `passBlock` vs `runBlock` correlate only **0.53**, so they are two real signals, not one duplicated. EA spread p10 70 → p90 84 against our saturated 80; EA has 71 linemen at 85+ and 24 at 90+ where we have 0. | One season only (CFB 27). No history → not backtestable, and OL becomes non-comparable across eras unless both are kept and labelled. Commercial dependency that could change or vanish. |
| **B. Better team line stats** | We fetch none of the standard line metrics. SP+ and the advanced team endpoints carry line yards, stuff rate, power success, and standard/passing-down splits — far better than raw rush YPA and a single sack rate. | Still a team number. Improves the 55%, does nothing for the 45%. Does not make it a player rating. |
| **C. Individual charting** | The real answer. | Does not exist in any source we can access. |
| **D. Stop publishing a single OL overall** | Publish a **unit** rating (team, honest about what it is) beside **pedigree** (recruiting). Two labelled numbers instead of one misleading one. | The largest UI change; asks more of the reader. Admits publicly that we cannot rate a lineman. |

**My recommendation, for discussion:** D as the frame, B to make the unit half defensible, A as a
clearly labelled experiment carried alongside — not replacing — the existing number for one
season, then judged against 2026. D is the only option that stops the category error, and it is
the same "Production vs Talent" split the model doc already proposes for everyone else. OL is
simply where the error is most extreme, which makes it the natural place to prove the pattern.

### 1b. Defense measures tackle volume, and tackle volume is close to the opposite of quality

**What it computes today.** Six counters from the box score: `defensiveTOT`, `defensiveSACKS`,
`defensiveTFL`, `defensiveQB HUR`, `defensivePD`, `interceptionsINT`. Each position combines
them with hand-set multipliers (e.g. CB: `coverage = INT×4 + PBU×2`, `tackling = TOT×0.3 +
TFL×1.0`).

**Measured, 2025 — correlation of each input with the final OVR:**

| Position | n | tackles | sacks+TFL | INT+PBU |
|---|---:|---:|---:|---:|
| EDGE | 318 | 0.70 | **0.83** | 0.47 |
| DL | 1,238 | 0.70 | **0.78** | 0.35 |
| LB | 1,230 | **0.78** | 0.76 | 0.60 |
| CB | 436 | 0.74 | 0.50 | **0.77** |
| S | 543 | **0.82** | 0.64 | 0.74 |
| DB | 914 | 0.73 | 0.62 | **0.78** |

Tackles correlate 0.70–0.82 with the rating at **every** position, and at safety they are the
single strongest input. That is the problem in one row, because a tackle count is mostly:

1. **snaps played** — which we do not divide by;
2. **how often the opponent runs at you** — a scheme and game-script fact;
3. **how often your defense is on the field** — a *bad* defense generates more tackles.

A defense that gets off the field on third down denies its own players the counting stat we
reward them for. The coverage credit (v4.2) patches this for the secondary specifically and
measurably helped, but the underlying denominator problem is untouched at every position.

**Other structural issues found:**

- **`DB` is 1,397 of 2,889 secondary players (48%)** and it is not our normalization failing —
  the API itself returns the generic string `"DB"` for them. The code comment says the group
  "should never appear post-v2 schema", which is wrong and should not be trusted by the next
  reader. Half the secondary therefore gets the even-thirds archetype weighting and a formula
  that is a copy-paste of safety.
- **Rate features divide by tackles floored at 1.** `instinct_score = (INT + PBU) / max(TOT,1)`
  means one tackle and one breakup scores a perfect 1.0. The playtime tiers cap the damage but
  do not remove it.
- **Signals sitting on disk, unused:** `defensiveSOLO` (solo vs assisted — a real quality
  distinction, present for every defender), `fumblesFUM` / `fumblesREC` (forced and recovered,
  present for 986), `snap_pct` / `snap_pct_pass` / `snap_pct_rush`, and `ppa` (nonzero for only
  12.6% of defenders, so thin).

**Options, roughly in order of value per unit of risk:**

1. **Per-snap instead of per-game.** Snap share is the missing denominator and it is already
   harvested. Constraint measured earlier: **zero coverage before 2013**, then ~2,500 players a
   season — starters and rotation only. So this is a modern-era, rotation-player improvement
   with an explicit "unknown" branch elsewhere, not a universal fix.
2. **Split solo from assisted.** Free, universal, and separates "made the play" from "arrived".
3. **Havoc framing.** `(TFL + INT + PBU + FF) / opportunity` is the standard opponent-neutral
   disruption measure, and every term already exists.
4. **Resolve `DB` → CB/S** using EA's position for matched players and a height/weight prior for
   the rest. Do **not** resolve it from box-score behaviour: that would assign the archetype
   using the same statistics the archetype then judges, which is circular.
5. **State the ceiling honestly.** Nobody publishes targets or coverage snaps. A corner rating
   built without them is bounded, and no rework changes that.

---

## 2. Jaden Mickey — why an 85 senior is projected to 74

His actual record, and the model's own reasoning:

| Season | Team | Games | EDGE %ile | OVR |
|---|---|---:|---:|---:|
| 2022 | Notre Dame | 7 | 10.3 | 50.2 |
| 2023 | Notre Dame | 9 | 58.2 | 72.9 |
| 2024 | Notre Dame | **3** (interrupted) | 15.6 | 63.5 |
| 2025 | **Boise State** | 11 | **92.8** | **85.2** |
| 2026 | Boise State | — | — | **projected 73.7** |

Drivers the model reports: `pct_accel` **−3.65**, `ovr` +2.34, `cohort_next` +1.66,
`n_seasons` +0.97. Cohort baseline: seniors at this level lose 4.5 (n=31); we then project
**7.0 below** even that.

**The mechanism.** Acceleration is a second difference over the percentile path.

- Raw path `10.3 → 58.2 → 15.6 → 92.8`: differences +47.9, −42.6, +77.2 → acceleration ≈ **+120**.
- Healthy-only path `10.3 → 58.2 → 92.8`: differences +47.9, +34.6 → acceleration ≈ **−13**.

The interrupted season digs a false trough, and climbing out of it registers as a violent
upward spike. The model has learned — correctly, in general — that violent spikes regress. It
then applies that lesson to a spike that is an artifact of a missed season. Both `pct_accel`
and `pct_accel_healthy` are fed to the model precisely so it can learn which to trust; for this
player it leaned on the raw one.

**Two further contributors:**

- **`pct_sd` is direction-blind.** A monotonic 10 → 58 → 93 climb scores as "inconsistent"
  identically to a player oscillating between those values. The model doc already identifies
  this; it remains unfixed and it is biting here.
- **The model does not know he transferred.** `FEATURE_COLS` has no transfer term. A player who
  moved from Notre Dame to Boise State and immediately produced at the 93rd percentile is,
  to the model, indistinguishable from one who did it in place.

**The tell that this specific projection is over-confident:** the 80% interval is
**[57.6, 83.6]**, which *excludes his current 85.2*. The model is asserting better than 4-to-1
odds that he cannot repeat his own last season. Across all 5,883 projections only 0.6% make
that claim, but 29.6% of players at 90+ do.

### The important caveat: in aggregate, the interruption handling works

| Career shape | n | mean projected Δ | projected down |
|---|---:|---:|---:|
| Clean | 5,225 | **+3.55** | 29.3% |
| Interrupted somewhere | 658 | **+6.06** | 20.5% |

Interrupted players are treated *more* generously on average. Mickey is a tail case, not the
central tendency — so the fix must be surgical, or it will make an already-optimistic model
worse. Which brings up the opposite and larger problem:

### The systemic error runs the other way — the model under-predicts decline

From the model's own holdout metrics, its predicted spread is **76% (offense) and 70%
(defense)** of the realised spread. Compressing spread mechanically produces too few large
moves in *both* directions.

Corroborating, comparing projected 2026 behaviour against what actually happened historically
at each rating level:

| Current OVR | Model says "down" | Actually declined | Model mean Δ | Actual mean Δ |
|---|---:|---:|---:|---:|
| <60 | 4.7% | 17.1% | +9.9 | +8.7 |
| 60–70 | 21.7% | 42.8% | +3.2 | +1.4 |
| 70–80 | 45.8% | 50.5% | +0.6 | −2.7 |
| 80–85 | 49.2% | 58.7% | +0.1 | −3.7 |
| 85–90 | 56.9% | 68.4% | −0.7 | −4.9 |
| 90+ | 66.2% | 81.0% | −2.5 | −7.8 |

*(Caveat: not a like-for-like population — the 2026 projection set is the returning roster,
the historical set is every player with a following season. Treat the direction as the finding
and the magnitudes as needing a matched re-measurement.)*

The direction is consistent across all six bands. v3.2 fixed "everyone declines" and appears to
have overshot into "almost nobody does".

**So there are two distinct problems and they pull in opposite directions.** Do not fix them
with one lever:

- **P1, individual:** artifact-driven penalties for interrupted careers.
- **P2, systemic:** insufficient spread, so decline is under-called everywhere.

**Options for P1** (any of these is small; the question is which is principled):

1. Feed the **gap** `pct_accel − pct_accel_healthy` as an explicit feature instead of hoping the
   model infers it from both. The gap *is* the interruption signal.
2. Suppress raw-path shape features when `n_interrupted > 0`, using healthy-path only.
3. Replace `pct_sd` with a **monotonicity** measure, so steady climbing stops reading as volatile.
4. Add a **transfer feature** (moved / stayed, and destination-vs-origin program strength).

**For P2**, the lever is the variance calibration, not the features — but note it is already
tuned to hit 80% interval coverage (80.6% / 78.8%, both close to target). Widening spread to fix
the decline rate would break coverage. That tension is the real design question, and it suggests
the point estimate and the interval want separate treatment.

**What would decide it:** re-run the historical base-rate table restricted to a matched
population (players on the following season's roster), then check whether the model's projected
decline rate matches within each band. That is a measurement, not an opinion, and it is cheap.

---

## 3. The research findings

All four are honest computations. Two of them are labelled as answering a question they cannot
answer.

### 3a. "Who beats the roster they recruited" (script 13)

**Computes:** `SP+ ≈ β₁·talent + β₂·is_p5 + c` by least squares; residual = actual − predicted.
R² = 0.474, residual SD = **9.82** SP+ points.

**Claims:** the residual "identifies programs beating or trailing the roster they recruited",
presented as a coaching and development signal.

**Measured:** the residual persists — r = **0.607** year over year, decaying to **0.280** at
three years. So it is emphatically *not* noise; something durable is there.

**But persistence does not identify coaching.** Everything that is stable about a program and
missing from a two-variable model lands in that residual: scheme, strength and conditioning,
walk-on and JUCO pipelines, portal usage, and — most importantly — **systematic error in the
talent proxy itself**. If the 247 composite understates what a program's roster is actually
worth, that understatement is persistent by construction and indistinguishable from coaching.
Boise State is the obvious case and the one the user cares about.

**What would identify it:** a coaching-change event study — does the residual step when the head
coach changes, and does it travel with the coach? That is the decisive test, and it is
**blocked**: the coaching table is 22 seeded rows. Getting real coaching-tenure data is
therefore not a side quest; it is the prerequisite for the claim the page already makes.

**Cheaper improvements available now:** add returning production and previous-year SP+ as
covariates (separating "beat your talent" from "beat your talent *this year specifically*"),
report the residual with an interval, and shrink small samples toward zero before ranking.

### 3b. "What recruiting classes actually became" (script 14)

**Computes:** hit rate = share of a class reaching peak OVR ≥ 75.

**Claims:** "separating programs that recruit well from programs that develop well."

**Measured:** correlation between class average recruiting composite and hit rate is **+0.266**
(among rated recruits) and **+0.328** (over all recruits).

| Class strength | n | hit rate (rated) | hit rate (all) |
|---|---:|---:|---:|
| Weak third | 757 | 28.8% | 16.7% |
| Middle | 780 | 29.5% | 17.8% |
| Strong third | 757 | **39.0%** | **25.6%** |

Better classes have higher hit rates. So the metric **tracks recruiting**, which means it cannot
do the job stated on the page — it is closer to a restatement of the star ratings than a
separation from them. (I expected the opposite confound, that weak programs would look good
because their recruits play sooner. The data says no.)

**The fix is the same trick script 13 already uses, applied one level down:** compute each
*recruit's* expected peak OVR from his own composite, then aggregate the **residual** per class.
A program that turns 3-stars into 80s scores well; a program that turns 5-stars into 80s does
not. That is the development question, and it is a small change to a script that already has
every input.

Two further issues: peak OVR is computed across all seasons, so recent classes are **censored**
(the `maturing` flag exists — good, keep it and make the UI respect it), and a hit rate over all
recruits mixes "developed poorly" with "transferred out", which are different stories.

### 3c. "Who beats their cohort next season" (breakout)

This is the projection surfaced as research, so it inherits everything in §2 — including the
Mickey failure mode, on the page where such a call is most visible. The `vs_cohort` framing is
right and should be kept: comparing to what similar players did is the honest claim, and it is
what stops the label collapsing into "was bad last year".

### 3d. "The recruits nobody wanted" (hidden gems)

**Computes:** ≤2★ recruits who reached OVR ≥ 70. Selection on the outcome, with **no
denominator** — we never show how many 2★ recruits there were, so the reader cannot tell
whether this is remarkable or arithmetic. Same fix as 3b: express it against expectation, and
show the base rate beside the list.

### Cross-cutting, and the one I would fix first

**None of these findings carries uncertainty, and all of them are presented as rankings.** With
a residual SD of 9.82 and 2,310 team-seasons, the top of the leaderboard is substantially noise
— that is the multiple-comparisons trap, and a portfolio piece that ranks 2,310 things without
shrinkage will eventually be embarrassed by one of them. Empirical-Bayes shrinkage plus a
displayed interval would apply to every finding on the page and would cost one shared helper.

---

## 4. NIL, transfer impact, and the playoff model

### 4a. NIL — decide what we are measuring before hunting for a source

`nil_valuations.json` is empty; On3 blocks scraping. Before choosing a scraper, the definitional
question matters more:

- A **valuation** (what a player's brand is estimated to be worth) is a model output from
  someone else, with its own unknown biases.
- **Actual compensation** is the quantity the roster-construction questions need, and it is
  largely private.
- **Program-level spend** is coarser, more attainable, and — importantly — **sufficient for the
  questions we actually want to ask**: does spend predict the performance residual, does it
  predict portal hit rate, does it change roster composition.

**Planning position:** treat program-level spend as the primary target and player-level
valuation as optional colour. That reframing unblocks the roster-construction thread without
resolving the hardest data problem, and it avoids importing a third party's model into ours.

**Hard rule to carry forward:** NIL must never enter an *earned* rating. It can enter research
and, if validated, projections — labelled via `projection_source`, the machinery for which
already exists.

I should confirm what is currently obtainable and permitted before recommending a specific
source; I have not verified Spotrac's coverage or terms, and I would rather check than guess.

### 4b. Transfer impact — the outcome is the hard part, not the data

**Data is in good shape:** 18,885 portal rows, **96.4% linked** to a player, 2021–2026, with
`from_team_id`, `to_team_id` and `portal_date`.

**Measured, and this is the finding that should shape the design:**

- 4,288 transfers have a rating on **both** sides of the move.
- **4,482 were rated before and never rated after** — they did not play enough at the new school
  to be rated at all.

So the single most common outcome of a transfer, in our data, is *disappearing*. Any analysis
restricted to players with a rating on both sides throws away half the population **and
conditions on the outcome of interest**. "Did the transfer work?" must treat not playing as a
result, not as missing data.

**Second, the naive comparison is pure mean reversion:**

| Prior OVR | n | mean Δ after transfer |
|---|---:|---:|
| <60 | 1,573 | **+10.6** |
| 60–70 | 1,116 | +2.2 |
| 70–80 | 1,037 | −5.2 |
| 80+ | 562 | **−7.5** |

Aggregate "+2.2, 54.9% improved" is meaningless — it is the same regression to the mean that
would appear for players who never moved. **The comparison must be against matched stayers** at
the same prior level, position, and class year.

**Design sketch to react to:** outcome = a two-part model, `P(rated at the new school)` and then
performance conditional on playing; treatment = transferred vs matched stayer; report the
destination-vs-origin program-strength gradient (the G5 → P4 question the research page already
lists as unanswered). State plainly that this is observational and that selection into the
portal is not random — we can adjust for what we observe, not for why he left.

### 4c. Playoff model — the sequencing is right; the open questions are upstream

The design already recorded (game-level win probability → Monte Carlo the schedule → conference
champions → bracket, backtested strictly out-of-sample on 2024 and 2025, scripts **17–19**) is
sound and I would not change its shape. What needs deciding *before* any code:

1. **Target variable.** Win/loss directly, or margin then mapped to a win probability? Margin
   carries more information per game and is the conventional choice, but it needs a variance
   model to become a probability.
2. **What "the backtest" means.** Predicting 2024 must train only on ≤2023 — including the
   *ratings* and the *anchors*, which are themselves fitted on history. Anchors calibrated using
   2024–25 data leak into a 2024 backtest. This is the subtle one and it is worth an explicit
   decision.
3. **Metric hierarchy.** Brier and log loss for calibration, a reliability curve for honesty,
   and "how many actual playoff teams landed in our top 12" for the headline. Agree the ordering
   now so the result is not chosen after seeing it.
4. **Preseason vs in-season.** A preseason-only model is a much harder problem and a much better
   story. Mixing in-season updating makes the numbers look better and the claim weaker.
5. **Known blind spots to state in the UI:** no injuries, coaching too thin to use, and — new
   from this review — the projected roster strength that feeds it inherits §2's compressed
   spread.

---

## What I would sequence first, and why

1. **The matched re-measurement of projection decline rates (§2).** Cheap, decisive, and it
   determines whether P1 or P2 is the real problem. Everything else in the projection is guesswork
   until it is done.
2. **Residualize the recruiting-ROI metric (§3b).** Small change, existing inputs, and it turns a
   metric that restates recruiting into one that answers the development question the platform is
   actually about.
3. **Shrinkage and intervals on every research finding (§3).** One shared helper, applies
   everywhere, and it is the difference between a demo and something defensible.
4. **Per-snap defensive rates and the solo/assist split (§1b).** The biggest honest accuracy win
   available without a third party, bounded by usage coverage starting in 2013.
5. **Decide the OL framing (§1a)** before building anything for it — D-then-B-then-A, or
   something else, but the decision precedes the code.

Threads 4a–4c stay parked until the above lands, with one exception: confirming what NIL data is
actually obtainable is research, not implementation, and can happen in parallel.
