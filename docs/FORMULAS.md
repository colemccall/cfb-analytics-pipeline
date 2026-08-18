# Every rating formula, as it is actually computed

*Rating version **v4.5**, 2026-08-13.*

What changed in v4.5:

| | change |
|---|---|
| **Tiers** | **DB was missing from `PLAYTIME_TIERS` entirely**, so all 23,353 DB player-seasons classified as starters and were rated on the full formula with no recruiting anchor. Fixed; 7,138 DB ratings moved and agreement with EA rose 0.6132 → **0.6497** (§4) |
| **Tiers** | a defender's zero tackles read as one, because the tier lookup used the floored `volume_score`. The bench tier was unreachable at CB, DL and EDGE (§4) |
| **Every row** | carries `rating_basis` — `production` / `blended` / `recruiting` / `withheld` (§4) |
| **No-EDGE fallback** | percentile-of-the-pool scaling replaced by fixed `STAT_FALLBACK_ANCHORS` (§1) |
| **Opponent multiplier** | documented asymmetrically, as the code has always computed it (§0) |

*Rating version v4.3, 2026-08-12.*

"As actually computed", not as intended — the distinction earns its place here. The offensive
line rating in the previous version of this document was described correctly and computed
correctly from inputs that did not exist, and nothing in the code, the tests or the site said
so for a year.

What changed in v4.3, in one place:

| | change |
|---|---|
| **OL** | player rating **withdrawn**; a line-**unit** rating replaces it (§2) |
| **Defence** | solo/assist split, fumble recoveries, an opportunity denominator, small-sample shrinkage (§3) |
| **Defence** | havoc share built, measured, and **not scored** — it failed its own ablation (§3) |
| **Secondary** | `ARCHETYPE_SCALE` re-measured: ball hawk 12.9 -> 13.4 |
| **K/P** | distribution gate re-derived; it had been warning on every run since v4.2 (§5) |
| **Team** | `avg_top()` returns `None` instead of a fabricated 50, and renormalises (§6) |

Companion documents: `ALTERNATIVES.md` (what we could compute instead, with the test that would
settle each), `HOW_PROJECTIONS_WORK.md`, `RESEARCH_METHODS.md`, `API_INVENTORY.md` (generated).

## 0. The shape shared by every position

Two stages, always:

```text
stage 1 (script 06)   per game:  stat_composite × opponent_multiplier
                      season:    Σ(per-game) / √(games_played)   = edge_score

stage 2 (script 07)   rating:    edge_score → OVR through fixed position anchors
```

- **`opponent_multiplier`** — the opponent's SP+ on the relevant side of the ball, as a z-score
  clipped to ±2, applied **asymmetrically**:

  ```text
  z ≥ 0 (hard opponent):  1 + z × 0.35     up to 1.70
  z < 0 (weak opponent):  1 + z × 0.12     down to 0.76
  ```

  Beating a top defence is rewarded roughly three times as hard as beating a weak one is
  punished. **Corrected 2026-08-13:** this document, `RATING_AND_PROJECTION_MODEL.md` and the
  site's methods page all described a symmetric `[0.55, 1.45]`. The code
  (`06_compute_edge_scores.py:228-256`) has always done the above; only the description was
  wrong, and it was wrong in the document whose stated premise is "as actually computed".
- **`√(games_played)`** — rewards sustained production without destroying a player who missed
  time. Not a mean (which would flatter a one-game wonder) and not a sum (which would make
  availability the whole rating).
- **Anchors are absolute, not a curve.** If nobody reaches the top anchor in a season, nobody
  gets a 99. This is what makes 2012 and 2025 comparable, and it is the single decision in the
  system most worth protecting.

**Three positions never reach stage 1 at all** — OL, K and P have no `edge_score` rows,
because there is no per-play production to build one from. They go through a separate
composite → `COMPOSITE_OVR_ANCHORS` path described in §2 and §5.

**A player at an EDGE position with no EDGE score** — injured, pre-2016, or below the
`stats_measured` threshold — falls back to a stat-only composite mapped through
`STAT_FALLBACK_ANCHORS`, capped at 78 because elite production cannot be confirmed without
the opponent adjustment.

Those anchors are **fixed constants as of v4.5**. Until then the fallback mapped through
`np.percentile(pool, [0, 10, 50, 75, 90, 99, 100])` — pool-relative scaling, the exact
mechanism `AUDIT_FINDINGS.md` §9 forbids, surviving on one path after being removed from
every other. The bottom of the no-EDGE pool became 30 and the top 78 in every season
regardless of how good either actually was. The x-coordinates are now that same pooled
2008–2026 distribution frozen at those seven percentiles, so today's output is unchanged to
within interpolation error (3,468 rows moved by ≤0.02 OVR, none by more) and a future crop
that is genuinely worse maps lower instead of being re-stretched to fill the band.

---

## 1. Offensive skill: QB, RB, WR, TE

### Stage 1 — the per-game composite (script 06)

```text
QB   passYds×1.0 + passTD×25 + rushYds×0.7 + rushTD×20 − INT×20
RB   rushYds×1.0 + rushTD×20 + recYds×0.7 + recTD×15
WR   recYds×1.0 + recTD×20 + rushYds×0.7
TE   as WR
```

### Stage 2 — supplementary features and the blend (script 07)

```text
QB   comp_pct, yards_per_att, td_int_ratio, volume_score
RB   yards_per_carry, yards_total, rec_versatility, volume_score
WR   yards_per_rec, yards_total, td_score = TD×8 + yds×0.01, rec_volume
```

Each is normalised to `[0,1]` against **fixed absolute bounds** (`FEATURE_BOUNDS`), then blended:

| Position | edge_score | supplementary | recruiting |
|---|---:|---|---:|
| QB | 0.45 | comp_pct .15, yards_per_att .20, td_int .17 | 0.03 |
| RB | 0.42 | yards_per_carry .20, yards_total .18, versatility .17 | 0.03 |
| WR/TE | 0.38 | td_score .22, yards_per_rec .20, yards_total .12, volume .05 | 0.03 |

### Stage 3 — anchors

`EDGE_OVR_ANCHORS`, piecewise linear, per position and era bucket. QB, for example:

```text
edge_score   0 → 30    445 → 65    1100 → 85    1676.9 → 96    2385 → 99
```

Anchor x-values are the edge_score posted by the Nth-best player at that position in a typical
season. **The scales are not comparable between positions** — a QB accumulates ~1,600 where a
corner accumulates ~20 — which is why boards show percentile-within-position, never raw EDGE.

---

## 2. Offensive line — the player rating is withdrawn

**Status: no lineman carries an earned rating from v4.3.** What follows is first the record of
why, because the failure is instructive, and then what replaced it.

### The rating that was removed

```text
composite_OL = 0.30·N(team_rush_ypa)  + 0.25·N(team_sack_rate_inv)
             + 0.30·N(recruit_composite) + 0.10·N(experience) + 0.05·N(award_tier)
```

`team_rush_ypa` and `team_sack_rate` were read with `_stat_float(stats, ...)` from the player's
own `season_aggregate` payload. **Those keys are never written there.** Verified: 0 of 280 OL
payloads in 2025 contain `team_rush_ypa`. So:

- `N(team_rush_ypa)` = `clip((0 − 3)/3)` = **0** — contributed nothing, ever.
- `team_sack_rate_inv` = `1.0 − min(0.0, 1.0)` = `1.0`, so `N(1.0)` = **1.0** — a flat **0.25 for
  every lineman in the country**.

The live formula was therefore
`0.25 + 0.30·N(recruiting) + 0.10·N(experience) + 0.05·N(award_tier)`, and:

- Recruiting was **67% of the only signal that varied** — OL correlated r = **0.877** with the
  recruiting composite where every other position is under 9%.
- Maximum attainable composite was `0.65`, which the anchor table mapped to exactly **88**. The
  documented "88 cap" was not policy. It was arithmetic.
- **59 of 293** rated linemen in 2025 (20%) landed on exactly **80.0**.
- Within-position agreement with EA CFB 27 was **−0.274**. Negative.
- `experience` was 22% of the live signal and came from `player_seasons.year`, which is **not a
  class year** — constant across a career for 84% of players with three or more seasons. A
  lineman's experience term did not increase as he aged.
- Only ~9% of rostered linemen were rated at all (280 of 2,952), because a lineman receives a
  stats payload only if he happens to record a defensive stat. The rated ones were close to a
  random sample, not the best ones.

**There is no per-lineman blocking data in the API** — no pancakes, no sacks allowed, no
pressures allowed. Verified by a full key scan; see `API_INVENTORY.md`.

### What ships instead: the line as a unit

`utils/line_unit.py`, computed in script 10, attached to the team-season.

```text
composite = 0.30·N(line_yards)              + 0.25·N(sack_rate_allowed, inverted)
          + 0.20·N(stuff_rate,   inverted)  + 0.15·N(power_success)
          + 0.10·N(second_level_yards)

rating    = interp(composite, [(0,30) (0.15,42) (0.30,52) (0.45,62)
                               (0.55,70) (0.70,80) (0.85,89) (1.00,95)])
```

Sources: the four run metrics from `/stats/season/advanced` (2008–2025, all 2,295 FBS
team-seasons); sack rate from `/stats/season` `sacksOpponent / (passAttempts + sacksOpponent)`,
which is dropbacks rather than attempts — a sack is a pass play that ended in a sack, and putting
it only in the numerator understates the rate for exactly the lines that allow the most.

**Missing inputs renormalise the remaining weights. They never contribute a zero.** That is the
specific failure that killed the old rating, and `tests/test_line_unit.py` asserts it.

**Bounds are per era**, and that is a fix rather than a flourish:

| bucket | seasons | why |
|---|---|---|
| classic | 2008–2013 | |
| transition | 2014–2020 | `power_success` steps 0.664 → 0.717 and `second_level_yards` 1.038 → 1.112 at 2014 |
| modern | 2021–  | `line_yards` steps 2.885 → 3.095 and `stuff_rate` 0.199 → 0.165 at 2021 |

Pooled bounds produced a median line rating of 52 in 2008 rising to 77 in 2023. A 7% jump in
line yards and a 17% drop in stuff rate between two consecutive seasons is the provider changing
a definition, not 130 teams simultaneously learning to block. Within an era the bounds are still
fixed absolute constants, so the `AUDIT_FINDINGS.md` §9 guarantee holds. After bucketing, season
medians run 61–73 with no trend.

Note these era breaks are **not** script 07's `ERA_ANCHORS` (2013, 2018). Different phenomena:
those track when defensive stats became available, these track a change in how the
advanced-stats endpoint computes line play.

### What it is validated against

The only external check possible: `scripts/validate_vs_draft.py`.

- Spearman(line rating, linemen drafted off that season) = **+0.179**
- Mean line rating by picks: **65.2** with none, **70.8** with one, **77.1** with two

Weak, and real, and pointing the right way — against a withdrawn rating that scored −0.274.

### Consequences elsewhere

- `avg_top()` in script 10 returned a hard-coded **50.0** for a position with nobody rated. OL is
  40% of run offence, so withdrawing the ratings without changing this would have made 40% of
  every team's run offence a constant. It now returns `None` and the weights renormalise —
  universally, so a team with no rated kicker no longer gets a fabricated 50 either.
- Script 07 emits OL rows with `overall_rating = None`, `rating_status = "not_rated"` and a
  reason. A missing row would make a lineman vanish from his own roster; a withheld one keeps him
  there and says why.
- Script 15 never trained on or predicted OL, and still does not.
- An unplayed season gets **no** line rating. These are measurements of games that have not
  happened.

---

## 3. Defense: EDGE, DL, LB, CB, S, DB

### The inputs

`defensiveTOT`, `defensiveSOLO`, `defensiveSACKS`, `defensiveTFL`, `defensiveQB HUR`,
`defensivePD`, `interceptionsINT`, `fumblesREC`. Plus two team-level signals from
`/stats/season/advanced`: defensive plays faced, and unit havoc.

`defensiveSOLO` and `fumblesREC` were unused until v4.3. One key remains deliberately unused:
**`fumblesFUM` is not a forced fumble.** On a defensive row it is a fumble the player COMMITTED
— 974 rows carry one, 84% of them in a game where he also had a return, an interception or a
recovery, and 455 also carry `fumblesLOST`. Crediting it would pay a corner for coughing up an
interception return. Forced fumbles are not published per player anywhere in the API.

### The tackle credit (v4.3)

```text
2013+   :  SOLO x 1.25 + (TOT - SOLO) x 0.65   x position_weight
pre-2013:  TOT                                 x position_weight
```

A solo tackle is a play the defender made; an assist is a play he was near. Calibrated to be
aggregate-neutral — solo tackles are 56.4% of all recorded tackles, so
`0.564 x 1.25 + 0.436 x 0.65 = 0.988` and the average defender's credit does not move, only the
mix. `defensiveSOLO` does not exist before 2013 (zero rows), so the split degrades to plain
totals there; the code asks the data rather than hardcoding a year, so a season that stops
publishing the field cannot silently be read as all-assists.

Within 2013+ a zero **is** meaningful: 8,359 of 8,590 games have some players with solos and
some without, and the SOLO=0 rows average 1.65 tackles against 3.88 for the rest.

### The opportunity index (v4.3)

```text
index = clip(median_defensive_plays_per_game / this_defence_plays_per_game, 0.85, 1.20)
composite x index
```

The direct answer to "a tackle count is mostly opportunity". Above 1.0 means the unit faced
fewer plays than typical, so each counting stat represents more per snap. Deliberately gentle
and clipped: dividing outright would make snaps-faced the dominant term, and plays faced is not
purely a defensive virtue — a fast-tempo offence puts its own defence back on the field. Source
is `/stats/season/advanced` `defense.plays`, confirmed 2008+.

It earns its place on the placebo test: **+0.0085** mean within-position Spearman against EA
across the six defensive groups, while the same values **shuffled across teams score -0.0025**,
below doing nothing at all.

### Havoc share — computed, published, and NOT scored

`HAVOC_CREDIT = {}`. The player's havoc events (TFL + PBU + fumble recoveries; sacks are a
subset of TFL and are not added twice) over his unit's season havoc, from
`defense.havoc.frontSeven` / `.db`.

It failed its own ablation. Replacing every unit's havoc with **one shared constant** scored
+0.0019 against the real denominator's +0.0011 — a denominator that performs worse than a
constant is not a denominator. The credit was re-weighting tackles for loss and passes defensed,
which the composite already counts. The share is stored on `player_edge` and exported for
display, because "this player accounted for 18% of his unit's disruption" is a real fact about
him; it is simply not part of his number.

### Front seven

```text
EDGE  pass_rush  = sacks×5.0 + hurries×1.5 + TFL×2.0
      disruption = shrunk_rate(sacks + TFL, TOT, prior 0.288)
      run_stop   = TFL×2.5 + (TOT − sacks)×0.3
DL    pass_rush  = sacks×5.0 + hurries×1.5 + TFL×1.0
      run_stop   = TFL×2.5 + (TOT − sacks)×0.4
      disruption = shrunk_rate(sacks + TFL, TOT, prior 0.228)
LB    tackling   = TOT×0.5 + TFL×2.0
      coverage   = INT×3.0 + PBU×1.5
      instinct   = shrunk_rate(INT + PBU + TFL, TOT, prior 0.122)

shrunk_rate(events, tackles, prior) = (events + 12 × prior) / (tackles + 12)
```

### Small-sample shrinkage (v4.3)

Every defensive "rate" divides an event count by tackles, and a rate over one tackle is not a
rate. `instinct = (INT + PBU) / max(TOT, 1)` gave a player with one tackle and one breakup a
perfect **1.0**, and 30% of rated defenders have five or fewer tackles — so this was not an edge
case. **5,848 player-seasons posted a ratio of 1.0 or better against a normalisation ceiling of
0.3**, meaning nine corners in ten clipped to maximum and the feature was a constant rather than
a measurement.

Mixing in a prior worth 12 tackles at the position's pooled rate fixes it. Before to after:

| feature | p90 | p99 | max |
|---|---|---|---|
| CB instinct | 2.000 -> 0.315 | 5.000 -> 0.546 | 11.0 -> 1.01 |
| S instinct | 1.000 -> 0.220 | 4.000 -> 0.398 | |
| LB instinct | 1.000 -> 0.192 | 2.000 -> 0.343 | |
| EDGE disruption | 0.500 -> 0.402 | 1.000 -> 0.569 | |
| DL disruption | 0.444 -> 0.330 | 1.000 -> 0.491 | |

`FEATURE_BOUNDS` were re-derived to match: `disruption_rate` (0.10, 0.55),
`instinct_score` (0.04, 0.45). Leaving the old ones would have kept the feature saturated.

Blend: EDGE `0.50 edge + 0.25 pass_rush + 0.12 disruption + 0.08 run_stop + 0.05 recruiting`;
DL `0.40 / 0.25 / 0.10 / 0.18 / 0.07`; LB `0.40 edge + 0.25 tackling + 0.15 coverage +
0.10 pass_rush + 0.10 recruiting`.

### Secondary — the overall *is* the three archetypes

Since v4.2 a defensive back's score is not a stat composite but three sub-scores on one 0–10
axis, weighted by position:

```text
ball_hawk   = INT×12.0 + PBU×3.5 + defTD×8.0 + fumble_recoveries×6.0
run_support = tackle_credit(0.6) + TFL×4.0 + sacks×6.0 + hurries×1.5
coverage    = playing-time share × team pass-denial credit    (no box-score input at all)

scaled_k = clip(Σ(per-game k) / √games / ARCHETYPE_SCALE[k] × 10, 0, 20)
ARCHETYPE_SCALE = {ball_hawk: 13.4, coverage: 8.5, run_support: 14.8}

score = Σ weight[pos][k] × scaled_k
        CB 0.40 coverage / 0.40 ball_hawk / 0.20 run_support
        S  0.20 / 0.30 / 0.50            DB 0.33 / 0.33 / 0.33
```

`ARCHETYPE_SCALE` values are each archetype's 90th percentile, frozen like the anchors. They
**must be re-measured whenever an input changes** — stale constants once left coverage topping
out at 7.1 while run support reached 20.

Re-measured for v4.3 after fumble recoveries entered ball hawk and the tackle changes entered
run support, over 2,026 rated defensive backs in 2025: **ball hawk 12.9 -> 13.4**, coverage 8.5
unchanged, **run support 14.8 unchanged** — which is independent confirmation that the tackle
changes really were aggregate-neutral. Coverage sits below the other two by construction: a
defensive back on a porous pass defence earns no credit at all and the zeros drag its p90 down.

### Coverage denial (CB/S/DB only)

A corner's best games leave no trace, because quarterbacks stop throwing at him. The credit is
**additive**, not multiplicative — `def_context_modifier` already knew which defences were
good, but 1.1 × a suppressed number is still suppressed.

```text
per game, per offence:   shortfall = YPA_in_this_game − YPA_that_offence_averages_elsewhere
season, per defence:     Σ(shortfall × attempts) / Σ(attempts)      ← attempt-weighted
credit band:             percentile of the season shortfalls, clipped to [0, 1]
```

Measured against the offence actually faced, so a soft schedule no longer reads as good
coverage. Credit only, never penalty.

### What the defensive rating therefore measures

Tackles correlated **0.70–0.82** with the final OVR at every position before v4.3, and at safety
they were the strongest single input. A tackle count is mostly snaps played × how often the
opponent runs at you × how long your defence is on the field — so a defence that gets off the
field denies its own players the statistic we reward them for.

v4.3 attacks that from three directions (the solo split, the opportunity index, shrinkage). The
effect on within-position agreement with EA CFB 27 in 2025:

| position | v4.2 | v4.3 |
|---|---|---|
| EDGE | 0.5699 | **0.5822** |
| DL | 0.4766 | **0.4849** |
| LB | 0.6380 | **0.6400** |
| CB | 0.5654 | **0.5711** |
| S | 0.7219 | 0.7202 |
| DB | 0.5901 | **0.6132** |

Real, small, and in the same direction at five of six positions. It does not make the central
weakness go away: a tackle is still mostly opportunity, and the things that would settle it —
missed tackles, per-play tackle attribution, coverage snaps, targets allowed — do not exist in
any source. Independent confirmation of the remaining gap: our defensive ratings order NFL draft
picks at Spearman 0.13–0.25, against 0.42–0.49 for offence. Options are in `ALTERNATIVES.md`
§B; per-snap rates are the next phase.

### Era

`ERA_ANCHORS`: modern 2018+, transition 2013–2017, classic 2008–2012. Classic-era defensive
thresholds are 75% of modern, compensating for hurries and pass breakups not existing before
2015. Pre-2016 DL and EDGE ratings should be read as recruiting-caliber estimates.

---

## 4. Playing-time tiers, and what a rating is built from

Before any of the above, a player is bucketed by stat volume, and the tier decides how much
of the formula survives:

```text
starter   100% formula
role      75% formula + 25% recruiting anchor
reserve   40% formula + 60% recruiting anchor
bench     recruiting anchor only  =  position_avg + STARS_OVR_DELTA[stars]
```

Thresholds are per position — LB starter ≥ 20 tackles, CB ≥ 10, DB ≥ 15.

### `rating_basis` — the tier, published (v4.5)

Every rating row now carries what its number is **built from**, derived from the tier rather
than newly computed:

| basis | meaning | rows |
|---|---|---:|
| `production` | the formula ran on real production | 70,718 |
| `blended` | formula and recruiting mixed | 4,456 |
| `recruiting` | the number **is** `position_avg + stars_delta`, a six-valued step function | 29,427 |
| `withheld` | OL — `rating_status: "not_rated"` | 47,958 |

§7 has always said two thirds of a roster has no production to measure. Nothing on the site
said which two thirds, so a backup's 54 and a starter's 54 were published as the same kind of
object. This is the same category error as the withdrawn OL rating, differing only in degree,
and the fix is the same shape: the number stays, its nature stops being hidden.

### Two defects fixed in v4.5

**DB was not in `PLAYTIME_TIERS` at all.** The lookup returned `None`, which the code read as
"no thresholds, treat as starter" — so every one of **23,353 DB player-seasons** was rated on
the full production formula with no recruiting anchor, however little he played. In 2025 the
tier split was 919 starters and nothing else, against CB's 245 / 70 / 123. DB thresholds now
sit between CB's and S's, because DB is the API's own generic label for both and its tackle
distribution sits between them (2025 p50 14 against CB 13 and S 14).

Measured consequence: **7,138 DB ratings moved, mean −5.19**, and within-position agreement
with EA CFB 27 rose from **0.6132 to 0.6497** — the largest single-position gain in any recent
pass, from a missing table entry rather than from tuning. `test_every_rated_position_has_tiers`
is what stops the next position group inheriting it.

**A zero tackle read as one.** Defensive features divide by tackles, so `volume_score` is
`max(TOT, 1)`; the tier lookup read that floored value and the reserve threshold at CB, DL and
EDGE is exactly 1. The bench tier was therefore unreachable at those positions — 0 bench rows
at all three in 2025. Tiers now read an unfloored `tier_volume`.

**Held constant on purpose:** pre-2016 defenders keep the floored behaviour. Tackles are not
published before 2016, so their zero is unknown rather than real, and un-flooring it would
move them from `reserve` to `bench` — which discards the CLASSIC interceptions-and-recruiting
rating entirely. That interaction is a real defect (the tier blend already discards or dilutes
CLASSIC for every pre-2016 defender, worth a mean +9.0 to +16.5 across 3,900 ratings if
fixed), and it is a rating change with no external check available for that era. It belongs to
its own pass, not to a labelling one.

---

## 5. Kickers and punters

```text
K   0.50 fg_pct + 0.25 fg_long + 0.15 xp_pct + 0.10 volume
P   0.55 avg_yards + 0.30 inside_20_pct + 0.15 volume
both -> COMPOSITE_OVR_ANCHORS, ceiling 90
```

Specialists occupy a narrow band by design: their impact range is genuinely smaller than a skill
player's, and the tell that the old calibration was wrong was a punter outranking the receivers
on his own team page. v4.2 pulled them down — 17 kickers and 38 punters at 85+ became 4 and 2,
against EA CFB 27's 5 and 1.

### The gate, re-derived in v4.3

The distribution gate for K and P was inherited from the distribution it was supposed to judge:
mean 55-70, p90 65-79, p99 70-79. After v4.2 deliberately lowered specialists, the gate warned
on **every single run**, which is the same as not having a gate.

It was deliberately not fixed in the same change that shipped the ratings it judges — that is
how goalposts move. v4.3 is that separate change:

| | old | new | 2025 K | 2025 P |
|---|---|---|---|---|
| mean | 55-70 | **46-64** | 51.1 | 59.5 |
| p90 | 65-79 | **70-82** | 74.6 | 78.3 |
| p99 | 70-79 | **78-90** | 85.2 | 85.3 |

Derived from the stated design — a specialist's band is narrower than a skill player's and the
ceiling is ~88-90 — rather than from the shipped output. The old failure still fails: 24% of
punters at 85+ puts p90 near 88, outside the new ceiling of 82.

**Known limitation:** field goal percentage is heavily confounded by attempt distance and by
which kicks a coach chooses to attempt, and we have neither. A kicker on a bad team attempts
longer field goals and rates worse for it.

---

## 6. Team ratings (script 10)

```text
team_rating = 0.50 SP+ + 0.30 our player ratings + 0.20 team stats
              (renormalised across whichever signals exist)

pass_off = 0.45 avg_top(QB,2) + 0.35 avg_top(WR+TE,5) + 0.20 line_unit
run_off  = 0.40 avg_top(RB,3) + 0.40 line_unit + 0.10 QB + 0.10 WR/TE
pass_def = 0.45 avg_top(DB,5) + 0.30 avg_top(LB,4) + 0.25 avg_top(DL+EDGE,4)
run_def  = 0.40 avg_top(DL+EDGE,4) + 0.35 avg_top(LB,4) + 0.25 avg_top(DB,5)
special  = avg_top(K+P, 2)
```

### The trap that v4.3 removed

`avg_top()` returned a hard-coded **50.0** for a position with nobody rated. That is not a
default, it is a trap: an empty position silently became an average one. With the OL player
rating withdrawn it would have made 40% of every team's run offence an identical constant — the
rating would have stopped varying with the thing it claimed to measure, and nothing would have
errored.

`avg_top()` now returns `None` and `blend()` renormalises across whatever is present. The rule is
universal rather than an OL special case: a team with no rated kicker no longer gets a fabricated
50 for special teams either. It is the same rule the headline blend already applied when a whole
signal was missing, finally applied one level down.

The OL term is **not** simply deleted. It is the line-unit rating from §2, which is a more
honest input than the average of five recruiting ranks ever was. An unplayed season has no line
rating, so for 2026 the term renormalises out.

---

## 7. What every rating shares, and cannot escape

- It measures **counted, opponent-adjusted box-score production per game**. Not talent, not
  ability, not value.
- It cannot see **snaps** for two thirds of players, **blocking** for anyone, **coverage**
  except through a team proxy, or **missed tackles** at all.
- Two thirds of any roster has no meaningful production, so for them the number is mostly a
  recruiting grade in a production-shaped costume — which is exactly the OL problem in §2,
  differing only in degree.
