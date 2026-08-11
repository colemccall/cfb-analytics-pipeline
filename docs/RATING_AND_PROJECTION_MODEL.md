# How players are rated and projected — and where it breaks

*Written 2026-08-11. Originally an analysis-only document; **§9 records what has since been
acted on.** Every number here was computed from the data in this repo.*

> ## Update — v3.3 shipped the position-family split
>
> Three of the failures below are now addressed. Projections are split by position family,
> **OL is no longer projected at all**, and offensive skill gained the opportunity model.
> See §9. The EA CFB 27 options in §6 remain unimplemented.

---

## 1. Two different questions, one number

The platform answers two questions with the same 30–99 scale, and most of the trouble
comes from that:

| | Question | Evidence | Where it lives |
|---|---|---|---|
| **Rating** | What did this player *do*? | On-field production, opponent-adjusted | `engine="edge"`, scripts 06–07 |
| **Projection** | What *will* he do? | Career shape + what similar players did | `engine="projected"`, scripts 15–16 |

A rating is a measurement. A projection is a forecast. They share a scale so they can be
compared, which is useful, but it hides that they need different evidence. **A rating only
needs production. A projection needs talent** — and talent is exactly what we never measure
directly.

That single sentence explains almost every failure below.

---

## 2. How a rating is built today

```
per game:   stat_composite × opponent_multiplier
season:     Σ(per-game) / √(games_played)          = EDGE
rating:     EDGE → OVR through fixed position anchors
```

- **`stat_composite`** — a position-specific weighted sum of that game's box score. QB =
  `passYds×1.0 + passTD×25 + rushYds×0.7 + rushTD×20 − INT×20`, and so on per position.
- **`opponent_multiplier`** — the opponent's SP+ on the relevant side of the ball,
  normalised to `[0.55, 1.45]` per game. Production against a top-10 defence counts up to
  1.45×; the same line against a weak one counts 0.55×.
- **`√(games_played)`** — rewards sustained production without destroying players who
  missed time.
- **Anchors** — fixed piecewise-linear maps from EDGE to OVR, per position, per era bucket
  (classic / transition / modern). **Absolute, not a curve**: if nobody reaches the 99
  threshold in a season, nobody rates 99.

The anchor design is the strongest decision in the system and should not be touched. It is
what makes 2012 and 2025 comparable at all.

### What the rating therefore *is*

**A measure of counted, opponent-adjusted box-score production per game.** Not talent, not
ability, not value. Every failure below follows from asking that number to do a job it was
never built for.

---

## 3. How a projection is built today

Rebuilt in v3.2. Two stages.

**Stage 1 — script 15, the career-curve model.** For each player, every season becomes an
EDGE *percentile within its own (season, position group)*, forming a career curve. The
model reads that curve's shape — recency-weighted slope, acceleration, distance from peak,
consistency, availability, opponent strength faced — plus cohort development curves (what
players at the same position, class year and production decile historically did next).
Output is variance-inflated 50% toward the realised distribution and published with an 80%
interval.

**Stage 2 — script 16, the source chain.** Every player on the upcoming roster gets a
number from the first source that can produce one:

| Source | 2026 count | Share | What it knows |
|---|---:|---:|---|
| `engine_d` career curve | 3,820 | 33% | Real production history |
| `recruiting` grade | 5,155 | 44% | High-school scouting, nothing since |
| `carry` forward | 1,970 | 17% | One prior rating, moved along a cohort curve |
| `ea_cfb27` | 638 | 6% | Someone else's opinion |

Holdout accuracy (2023–24, never trained on): **MAE 8.24 vs 9.32** for assuming no change.
80% intervals cover 79.8%.

**That headline is honest but flattering**, because it is measured on the population that
has a career curve to read. Read on.

---

## 4. Where it actually breaks

### 4a. Two thirds of the roster has no production to project from

**7,739 of the 11,583 projected 2026 players — 67% — played two games or fewer in 2025.**
Only a third go through the method that carries the accuracy claim. The rest get a
recruiting grade from years ago, or a rating carried forward from a season they spent on
the bench.

The holdout error confirms this is where the money is:

| Prior season | n | Carry-forward MAE | Mean move |
|---|---:|---:|---|
| ≤2 games | 3,055 | **10.02** | 52.7 → 60.1 |
| 3–7 games | 2,627 | **10.25** | 58.9 → 63.5 |
| 8+ games | 3,957 | 9.48 | 71.5 → 71.1 |

Established starters are close to stable and easy. The hard, high-error population is the
one we know least about — and it is the majority.

### 4b. Backup → starter is a coin flip we pretend to know

Of players who went from ≤2 games to 8+ the next season (**n = 3,622**), the correlation
between our prior rating and their next-season rating is **+0.096**. Essentially zero. For
players who started both seasons it is **+0.476**.

Their average rating jumps **+19.3 points**.

So for the single most interesting group in college football — the guy about to get his
shot — our input carries almost no information, and we publish a projected number anyway.
Recruiting composite is no better (**+0.080**). Neither of our two available signals knows
anything.

### 4c. The OL rating is a recruiting rating wearing a costume

This is the worst of it.

- **Only 280 of 2,952 rostered OL got a 2025 rating — 9% coverage.**
- Of the ratings that exist, **r = +0.877 with recruiting composite — 77% of the variance**.
  For every other position that number is under 9%.
- Its correlation with EA CFB 27's independent OL assessment is **−0.293**. *Negative.*

We already document that OL inputs are team proxies (team rush YPA, sack rate allowed) and
cap them at 88. The measurement above says something stronger: **the OL number is not
mostly a production rating at all.** It is recruiting, lightly perturbed by which team a
player happens to block for, and it disagrees with the one independent source we have.

2026 makes it concrete: of 2,090 projected OL, **1,607 come from recruiting grade and 3
from the career-curve model.**

### 4d. The top of the scale is optimistic, and we know why

Cohort curves are built only from players who *have* a next season. The best leave for the
NFL. So the historical record overstates elite decline, and after variance inflation we
project **60% of 90+ players to decline where reality is 86%**. Currently disclosed in the
UI rather than corrected.

### 4e. Production and opportunity are entangled

A rating rises when a player produces more, and production requires snaps. We have no snap
counts, so "got better" and "played more" are the same event to us. This inflates every
backup-to-starter jump and quietly penalises good players on crowded depth charts.

---

## 5. What EA CFB 27 actually gives us

`data/raw/ea_ratings.json` — 9,013 players, 138 teams, **7,352 matched (81.6%)** to our
player IDs, **54 attributes each**:

| Group | n | Examples |
|---|---:|---|
| Athletic | 8 | speed, acceleration, agility, strength, jumping, stamina, injury |
| **Blocking** | **8** | passBlock, passBlockPower/Finesse, runBlock, runBlockPower/Finesse, leadBlock, impactBlocking |
| Pass rush | 6 | powerMoves, finesseMoves, blockShedding, pursuit, tackle, hitPower |
| Receiving | 6 | catching, catchInTraffic, route running (short/med/deep), release |
| Carrying | 7 | breakTackle, jukeMove, spinMove, stiffArm, trucking, vision |
| Passing | 7 | throwPower, accuracy (short/mid/deep), throwOnTheRun, playAction |
| Coverage | 3 | manCoverage, zoneCoverage, press |
| Mental | 2 | awareness, playRecognition |

### Is it just recruiting rankings recycled?

**No.** EA's overall correlates with the 247Sports composite at only **r = 0.15–0.36**
across positions. It carries information that neither our production ratings nor recruiting
rankings contain.

### Does it agree with us where we both have signal?

Yes — which is the encouraging part:

| Position | corr(ours, EA) | | Position | corr(ours, EA) |
|---|---:|---|---|---:|
| QB | **+0.770** | | LB | +0.658 |
| WR | +0.751 | | EDGE | +0.629 |
| RB | +0.721 | | CB | +0.617 |
| S | +0.701 | | K | +0.535 |
| TE | +0.677 | | DL | +0.496 |
| | | | **OL** | **−0.293** |

Two independent systems agreeing at 0.5–0.77 is mutual validation. **The one position where
they disagree outright is the one where we already know our inputs are proxies.**

### The tell: it rates talent, we rate production

| Population | corr(ours, EA) | Our mean | EA mean |
|---|---:|---:|---:|
| Starters (8+ games) | +0.619 | 71.0 | 78.6 |
| Bench (≤2 games) | **+0.305** | **54.2** | **72.1** |

On starters we broadly agree. On the bench we diverge by **18 points**, because we are
measuring two different things: we say "produced nothing", EA says "good player who didn't
start". **Both are correct. Only one of them is useful for projecting what happens when he
takes the job** — and it isn't ours.

That is precisely the 67% of the roster from §4a.

---

## 6. Options, with their costs

Ranked by value per unit of risk. None of this is built.

### Option 1 — EA attributes as the OL rating input *(highest value, lowest risk)*

OL is the only position where no individual production data exists anywhere. EA publishes
**eight per-lineman blocking grades**. Replacing team-proxy OL inputs with a blocking
composite would turn a rating that is 77% recruiting into one grounded in a per-player
assessment, and could lift the 9% coverage rate toward EA's roster coverage.

*Costs:* our OL ratings would become dependent on a commercial product that ships annually
and could change or disappear; the 88 cap would need re-derivation; historical seasons have
no EA data, so OL becomes non-comparable across eras unless we keep both and label them.

### Option 2 — a talent prior for the production-blind majority

For the 67% with no meaningful production, EA's overall is a far better starting point than
a stale recruiting grade. Rather than replacing the rating, use it as the **prior in the
projection**, with weight decaying as real production accumulates:

```text
projected = w · talent_prior(EA, recruiting) + (1 − w) · production_signal
w → 1 when a player has never played, → 0 once he has two full seasons
```

This is the honest shape of the problem: a player with no snaps *is* mostly a scouting
opinion, and pretending otherwise is what produces a 35.9-rated punter that EA has at 77.

*Costs:* introduces a third-party opinion into our headline projection. It must be
surfaced in `projection_source` (the machinery already exists) and cannot be allowed to
leak into *earned* ratings.

### Option 3 — separate the two scales instead of blending them

Publish **Production (earned)** and **Talent (scouted)** as distinct numbers, and let the
projection be an explicit function of both. Removes the category error at the root: a
backup would read `Production 41 · Talent 78` instead of a single misleading 54.

*Costs:* the largest UI change of the three, and two numbers ask more of the reader. But it
is the only option that stops overloading one scale with two meanings.

### Option 4 — fix opportunity blindness (independent of EA)

Snap counts or usage share would separate "improved" from "played more". The CFB Data API
exposes usage data that script 01 already fetches (`fetch_player_usage`) but the rating
does not consume. **This is the cheapest real accuracy win available and needs no third
party.**

### Option 5 — correct top-end survivorship

Model NFL departure explicitly (draft/departure data would be needed) so cohort curves stop
conditioning on "stayed in college". Fixes §4d properly rather than disclosing it.

---

## 7. The validation problem — read this before building anything

**We have exactly one season of EA data (CFB 27, 2026).** That is a hard constraint:

- We **cannot backtest** any EA-based feature. There is no 2019 EA snapshot to check
  whether EA's talent grade predicted 2020 production.
- EA CFB 27 ships *before* the 2026 season, so using it for 2026 is not leakage — it is
  legitimately available information. But "not leaking" is not the same as "known to help".
- Any EA-based rating change would be **unvalidated by construction** until a second
  season exists (CFB 28, ~mid-2027, giving one year-over-year pair).

The standing project rule is that a model which cannot show its backtest does not ship.
That rule should apply here too, and it points at a specific sequencing:

1. **Option 4 first.** Usage data is historical, so it can be backtested properly, and it
   attacks §4e directly.
2. **Option 1 next**, scoped to OL only. It is defensible without a backtest because the
   argument is not "EA predicts better" but "we currently have no individual OL input at
   all, and 77%-recruiting is worse than a per-player blocking grade." Ship it labelled,
   keep the old value alongside for a season, compare when 2026 is played.
3. **Option 2 as an experiment**, not a default: compute it, expose it under
   `projection_source`, and let 2026's actual results be the backtest we currently lack.
4. **Option 3 only if 1 and 2 prove the two signals really are distinct** — which the
   bench-vs-starter divergence already suggests, but one season cannot confirm.

---

## 8. Summary

The rating engine is sound and its anchor design should be protected. The projection engine
is sound *on the third of players who have a career to read*, which is exactly the third
where projection is easiest.

The real problem is not the model. It is that **we only measure production, projection
needs talent, and two thirds of any roster has no production to measure.** EA CFB 27 is the
first genuine talent signal available to this project — independent of recruiting
(r ≈ 0.15–0.36), agreeing with us where we are strong (0.5–0.77), and disagreeing exactly
where we already know we are weak.

The two cheapest honest wins:

1. **Usage data** for opportunity-vs-improvement (backtestable today, no third party).
2. **EA blocking attributes for OL**, where we currently have nothing individual at all.

Both should be labelled, kept alongside the existing values for a season, and judged
against what actually happens in 2026.

---

## 9. What v3.3 changed (2026-08-11)

Three of the failures above are now addressed. The EA options in §6 are not.

### Projections split by position family

One model over all positions was averaging good inputs with bad ones. Now:

| Family | Model | Holdout (naive → model) | Confidence |
|---|---|---|---|
| Offensive skill (QB/RB/WR/TE) | Career curve + cohort + **opportunity** | 9.11 → **8.23** | high |
| Defense (EDGE/DL/LB/CB/S/DB) | Career curve + cohort | 9.41 → **8.28** | **low**, stated in the UI |
| Specialists (K/P) | Carried forward | — | **low** |
| **OL** | **Not projected** | — | — |

Both models beat naive carry-forward, and each carries its own calibration and
interval quantiles — their error distributions are different shapes, so sharing them
mis-covered both.

### OL is no longer projected — 2,902 player-seasons dropped

§4c argued the OL number is a recruiting ranking in costume. It is now excluded from the
projection engine entirely rather than carried forward. Rosters show *"not projected"*
with the reason on hover instead of a number nobody should trust. Team ratings absorb the
absence uniformly (every team loses the same OL contribution), so relative ordering holds.

**This does not fix the OL rating itself** — earned OL ratings still exist for played
seasons and are still 77% recruiting. §6 Option 1 is the fix for that and remains open.

### Opportunity, for offensive skill

The missing idea: what a player did last season only tells you what he will do next once
you know whether he will get the ball. Three new feature families — his share of his
position room's production, his depth-chart rank on **next** season's roster (computed
from who is actually returning), and how much production is departing **ahead of him**.

Measured on 2023–24:

| Production ahead of him that departs | n | Yards | OVR |
|---|---:|---|---:|
| Nothing (<2%) | 4,283 | 1,080 → 1,022 | −2.6 |
| Some (2–15%) | 1,196 | 891 → 822 | −2.5 |
| A lot (15–35%) | 1,329 | 743 → 752 | −1.4 |
| **The job is wide open (>35%)** | **1,561** | **599 → 820** | **+1.7** |

A 280-yard swing driven purely by opportunity, entirely invisible to a career curve.
Opportunity features improve *yards* prediction by ~9% and *OVR* by ~1.8% — the gap is
itself informative: OVR is per-game and volume-normalised by construction, so it
deliberately strips out most of what opportunity drives.

### A breakout now requires a path to the ball

Regression toward the mean makes any regressor optimistic about players near the rating
floor. Without a gate the breakout list filled with fourth-stringers: one 58-yard WR sat
**third** on his depth chart behind players who were **all returning** and still scored
+18.9 against his cohort.

A breakout label now additionally requires one of: top-2 on the new depth chart, ≥25% of
the work ahead departing, or ≥300 yards of his own. **67 calls were demoted to steady.**
The top calls now all carry a real opportunity story — 90%, 82%, 97% of the production
ahead of them leaving.

### Also fixed

Season-aggregate stat rows were missing or empty for 176 offensive skill players who do
have game-level rows. Left unfilled they counted as zero production, corrupting their whole
position room's shares and vacancy. Now backfilled by summing game rows.

### Still open

- Everything in §6 — EA blocking attributes for OL, a talent prior for the
  production-blind majority, separating Production from Talent as two scales.
- §4d top-end survivorship.
- §4e opportunity blindness for **defense**, which has no touches to count. Defensive
  ratings need reworking before their projections mean much; they ship marked low
  confidence in the meantime.

---

## 10. Rating calibration, v4.2 (2026-08-11)

§9 rebuilt *projections*. This pass rebuilds the *ratings* they project from, after a
position-by-position review. Seven complaints came in; all seven reproduced against the data.

### The instrument that made it checkable

Every claim below is measured against **EA Sports CFB 27** (9,013 players, on disk since
the v3.2 pass), used as an independent scouting consensus covering the same 136 FBS teams.

EA is a **reference, not a target**. It never supplies a number. It answers one question —
*are we too generous, too stingy, is the ceiling in the right place* — and our own EDGE
distribution supplies the scale. Rank-matching to EA every season would have reintroduced
exactly the pool-relative scaling that AUDIT_FINDINGS §9 exists to forbid.

The reason to trust it as a reference is that it independently reproduced the one position
judged correct. TE was called "amazing" with no knowledge of EA's numbers; EA rates 17 tight
ends at 85+ and 3 at 90+, and so did we. **TE was therefore left completely untouched**, as
was OL.

### What was actually wrong

| Position | EA 85+ / 90+ | Ours, before | Diagnosis |
|---|---|---|---|
| QB | 27 / 9 | 38 / 5 | ceiling flat, else fine |
| RB | 70 / 15 | 24 / 7 | ceiling flat, else fine |
| WR | 83 / 15 | 45 / 9 | **middle far too low** |
| TE | 17 / 3 | 17 / 4 | correct — untouched |
| EDGE | 32 / 12 | 18 / 7, max **99** | thin at the top, tip too high |
| DL | 33 / 7 | **45 / 14** | too generous |
| LB | 45 / 10 | 54 / 16 | mildly generous |
| CB+S+DB | **113 / 24** | **53 / 15** | less than half what it should be |
| K | 5 / 0 | **17 / 7** | too generous |
| P | **1 / 0** | **38 / 9** | 38× too generous |

### Coverage denial — why the secondary was broken

A defensive back's best games leave no trace. Quarterbacks stop throwing at a corner who
covers, so the counting stats every position's score is built from measure, for this one
position group, *the opposite of what we want*: volume accrues to the DBs who get picked on.
Caleb Downs — a player no credible top-five safety list omits — rated **22nd among safeties**.

The machinery to know which defenses were good already existed — `def_context_modifier` —
but it is **multiplicative**, and 1.1 × a suppressed composite is still suppressed. That is
the whole bug in one line. Credit has to be **additive** to survive the suppression it is
correcting.

So a DB who is one of his secondary's five regulars is credited for how few passing yards his
defense allowed, whether or not the ball came near him — the same reasoning OL already runs
on, and carrying the same humility.

Two details were each wrong once before they were right:

- **The denial signal is z-scored across teams over their season means, not across games.**
  Per-game standardization measures game-to-game noise, nobody clears it, and almost no
  credit is paid.
- **Participation is rank within the secondary, not share of its tackles.** Share re-imports
  the suppression: the avoided corner tackles less, so he would be credited less. On a share
  rule a covered corner scored 0.68 of a full-timer. `tests/test_coverage_credit.py` locks
  this down — it is the test that caught it.

Tuned against EA (Spearman of our DB score vs EA's OVR, 2025, n=954):

| Coverage credit | Spearman |
|---|---|
| none | 0.6386 |
| ×3 | 0.6570 |
| **×4 (peak, shipped at ×3.5)** | **0.6576** |
| ×8 | 0.6521 |
| ×4, **randomly chosen** defenses credited | **0.6293** |

The placebo is the load-bearing row: crediting the same magnitude to random defenses scores
*below not crediting at all*, so the gain is the denial signal rather than the extra points.

### Defensive-back archetypes

"Defensive back" is three jobs wearing one label, and a single composite made them
compete on an axis they do not share. Each player-season now carries three sub-scores on
one 0-10 axis:

| Sub-rating | Built from | Note |
|---|---|---|
| **Ball hawk** | interceptions, pass breakups, defensive TDs | |
| **Lockdown** | playing time x how little his defense allowed per pass | no box-score input at all |
| **Run support** | tackles, TFLs, sacks | the only place tackles count as *production* |

Tackles do double duty deliberately: production in run support, and evidence of playing
time for the coverage credit — which is what lets a corner nobody throws at still register
as a full-time player.

The overall is the weighted sum of the three, by position:

| | coverage | ball hawk | run support |
|---|---|---|---|
| CB | 0.40 | 0.40 | 0.20 |
| S | 0.20 | 0.30 | 0.50 |
| DB | 0.33 | 0.33 | 0.33 |

This costs a little accuracy and buys explainability: Spearman against EA goes 0.660 ->
0.655 (2025) and 0.555 -> 0.540 (2024) versus the flat stat composite. Accepted so that a
defensive back's overall is literally the three numbers printed beside it. Reverting is
one line.

Two ways of combining them were rejected on measurement:

- **Best skill plus partial credit for the rest** scored 0.643 — taking a max discards the
  information that a player is good at two things.
- The first scale constants were carried over from a prototype with a different denial
  signal, leaving coverage topping out at 7.1 on a 0-10 axis while run support reached 20.
  Coverage could not win a comparison it existed to win, and **25 of 2,026 defensive backs
  typed as coverage players**. The constants are now each archetype's own 90th percentile,
  and must be re-measured whenever the inputs change.

### Receivers, and why the middle was too low

A team rotates three to five receivers through real snaps. The anchors priced the WR3
nationally as a reserve, which is a category error about the position rather than a
mis-estimate of the player. The 72 and 77 anchors are now the last man in a 3.5-deep rotation
and the rank-286 receiver. The same fact was wrong in the projection gate, where a WR sitting
third was treated as buried; `PATH_TOP_DEPTH_BY_POS` now reads WR 4, RB 3, TE 2, QB 1.

### Specialists

The tell was a punter outranking the receivers on his own team page. K and P now top out near
90, and an average specialist is an average player. Their impact range is genuinely narrower
than a skill player's, which EA concludes independently (its punters top out at 85, with one
above it nationally).

While recalibrating: **`fg_long` is 25% of the kicker composite and had been reading
`kickingLNG`, a key that does not exist.** It returned 0.0 for every kicker ever rated, and
the old anchors were fitted on top of that hole.

### Ceilings

Set from our own history, in opposite directions because the complaints were opposite:

- **offensive skill** — the *weakest* season's best player is a 96, so the best player in the
  country always reads like one;
- **defense** — the *typical* season's best is a 96, so a monster year can still exceed it and
  only a historic one nears 99. A 99 was going to a very good season rather than a
  generational one.

The single best season on record maps to 99 either way.

### Still open

Unchanged from §9, minus the secondary: EA blocking attributes for OL, a talent prior for the
production-blind majority, separating Production from Talent as two scales, and §4d top-end
survivorship. Defensive **front-seven** ratings remain counting-stat driven with no equivalent
of the coverage correction, because there is no analogous team proxy for "he was doubled".

---

## 11. Interrupted seasons, and a class year that was never a class year (2026-08-11)

Two reported projections, one root cause: a career curve cannot tell a season a player
*missed* from a season a player *declined*.

**Whit Weeks** (LSU LB) played 12 games in 2024 at the 98th percentile among linebackers and
6 games in 2025 at the 69th. The model projected him up — correctly — but computed
`vs_cohort` against a baseline anchored on his injured rating, produced +21.0, and labelled
him a **breakout**. 2024 was the breakout. 2026 is a return to it.

**Jaden Mickey** played 3 games at Notre Dame in 2024 and a career-best 11-game 2025 at Boise
State. The lost season dragged `pct_mean` down and `pct_sd` up until his best year read as an
outlier, and he projected **down 9.6** off it.

### Detecting an interruption without reading the future

The hard part is separating *hurt* from *backup*: a true freshman playing 4 games is a
reserve, a starter playing 4 games is injured. Availability is measured against **his team's
games** — the most any of its rated players appeared in — because raw game counts are not
comparable across 12-game, 13-game and 2020 seasons.

A first attempt required an absolute 75% prior availability to count as "established". It
caught Weeks and **missed Mickey**, whose prior best was 9/13 = 0.69: he was a rotation corner
at Notre Dame, never a full-time starter. The test is therefore relative to *his own* prior
best, not to an absolute idea of a starter:

```
interrupted  ⟺  avail < 0.60  ∧  prior_max ≥ 0.50  ∧  avail ≤ 0.60 × prior_max
```

Prior seasons only. A test asserts that truncating a career cannot change an earlier verdict —
otherwise the model would be trained on information it will not have at prediction time.
About 5% of player-seasons qualify.

### Both readings are supplied, not one replacing the other

Career shape is computed twice — raw, and over healthy seasons only — and both go to the
model. The *gap between them* is the signal that a season was interrupted, and the model
learns how much to trust each. Nine features: `last_interrupted`, `n_interrupted`,
`avail_last`, `avail_prior_max`, `pct_last_healthy`, `pct_slope_healthy`, `pct_mean_healthy`,
`pct_sd_healthy`, `pct_peak_healthy`, plus `pct_accel_healthy`.

Acceleration mattered more than the rest. It is a second difference, so one interrupted season
poisons it twice: Mickey's raw path 11 → 57 → 16\* → 91 gives **+116**, an unsustainable-looking
leap that is entirely the 3-game season sitting in the middle. Over the seasons he played it is
**−12**.

The cohort lookup also buckets on the healthy percentile. Bucketing Weeks on his 6-game season
filed him among replacement-level linebackers and handed him their development curve.

### `bounceback` is its own label

Returning to a level already posted is a different and better-supported claim than breaking new
ground. A projection is relabelled when the last season was interrupted, the projection rises by
at least 3 OVR, and it lands at or below the healthy peak plus that margin. Exceeding the healthy
peak by more is still a breakout. 250 of 5,645 projections qualify.

### The class year was never a class year

`player_seasons.year` is a **static player attribute**. Of 29,722 players with three or more
seasons and a plausible stored value, it is constant across the entire career for **84%** and
erratic for the rest — not one increments correctly. It also holds an outright calendar year
for 114,612 of 269,552 rows, almost all before 2017.

Cohort curves are the explainable backbone of every projection, and a cell keyed
`(position, class_year)` was not measuring what juniors do next — it was mixing one player's
freshman, sophomore and junior seasons under whichever label he happened to carry. Mickey
played four seasons and was a "junior" in all of them.

Derived instead, in order of trust: `season − recruit_year + 1` where recruiting has him (52%
of rows), else `season − first_observed_season + 1` as a **floor** (a career starting before
2008 looks younger than it was). Where both apply they agree exactly 72% of the time and
within one year 90%. 53,348 of 64,024 rows changed. Usable cohort cells rose from 255 to 334.

### Measured

| | naive | before | after |
|---|---|---|---|
| Offence (n=2,094) | 9.09 | 8.19 | **8.19** |
| Defence (n=3,985) | 9.67 | 8.50 | **8.39** |

Interval coverage 78.5% → 79.9% on defence. `decline` fell from 1,580 to 1,417; most of that
was players being charged for a season they missed.

### Still open

**Mickey is only half fixed** — −9.6 to −9.4. The mechanical defects are gone (his lost season
no longer poisons slope, mean, SD or acceleration, and his class year is right), but the model
still regresses him, and defensibly: excluding the interrupted year his career average is the
53rd percentile with a single 91st-percentile season, the senior cohort at that level loses 4.5
(n=28), and his three closest historical shapes averaged −7.1.

The remaining lever is that **standard deviation is direction-blind**. `pct_sd_healthy` scores
Mickey's monotonic 11 → 57 → 91 climb as "inconsistent" identically to a player oscillating
between the same values. A monotonicity measure alongside SD would separate steady development
from volatility. That is a general improvement, not a fit to one player — which is why it is
listed here rather than applied as a per-player adjustment.

`player_seasons.year` is still exported to the frontend by script 12, so a player card can
read "Junior" for a fourth-year senior. Only script 15 derives it today.
