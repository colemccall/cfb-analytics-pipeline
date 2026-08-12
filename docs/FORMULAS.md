# Every rating formula, as it is actually computed

*Written 2026-08-12. Descriptive, not aspirational — where the code does something other than
what it appears to, this records what it does.*

Companion documents: `ALTERNATIVES.md` (what we could do instead),
`HOW_PROJECTIONS_WORK.md` (the projection engine in plain language),
`RESEARCH_METHODS.md` (the research findings), `API_INVENTORY.md` (what data exists at all).

---

## 0. The shape shared by every position

Two stages, always:

```text
stage 1 (script 06)   per game:  stat_composite × opponent_multiplier
                      season:    Σ(per-game) / √(games_played)   = edge_score

stage 2 (script 07)   rating:    edge_score → OVR through fixed position anchors
```

- **`opponent_multiplier`** — the opponent's SP+ on the relevant side of the ball, normalised
  to `[0.55, 1.45]` per game. Production against a top-10 defence counts up to 1.45×; the same
  line against a weak one as little as 0.55×.
- **`√(games_played)`** — rewards sustained production without destroying a player who missed
  time. Not a mean (which would flatter a one-game wonder) and not a sum (which would make
  availability the whole rating).
- **Anchors are absolute, not a curve.** If nobody reaches the top anchor in a season, nobody
  gets a 99. This is what makes 2012 and 2025 comparable, and it is the single decision in the
  system most worth protecting.

**Three positions never reach stage 1 at all** — OL, K and P have no `edge_score` rows,
because there is no per-play production to build one from. They go through a separate
composite → `COMPOSITE_OVR_ANCHORS` path described in §2 and §5.

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

## 2. Offensive line — read this one carefully

### What the code says

```text
composite_OL = 0.30·N(team_rush_ypa)
             + 0.25·N(team_sack_rate_inv)
             + 0.30·N(recruit_composite)
             + 0.10·N(experience)
             + 0.05·N(award_tier)

N(x) = clip((x − lo) / (hi − lo), 0, 1)
   team_rush_ypa      (3.0, 6.0)      team_sack_rate_inv (0.5, 0.98)
   experience         (1.0, 5.0)      award_tier         (0.0, 3.0)

OVR = interp(composite, [(0,30) (0.25,45) (0.325,55) (0.40,65)
                         (0.50,72) (0.625,80) (0.65,88)])
```

### What the code does

`team_rush_ypa` and `team_sack_rate` are read with `_stat_float(stats, ...)` from the player's
own `season_aggregate` payload. **Those keys are never written there.** Verified: 0 of 280 OL
payloads in 2025 contain `team_rush_ypa`.

So `_stat_float` returns `0.0` for both, and:

- `N(team_rush_ypa)` = `clip((0 − 3)/3)` = **0** — contributes nothing, ever.
- `team_sack_rate_inv` = `1.0 − min(0.0, 1.0)` = `1.0`, so `N(1.0)` = `clip((1 − 0.5)/0.48)`
  = **1.0** — contributes a flat **0.25 to every lineman in the country**.

**The live formula is:**

```text
composite_OL = 0.25 + 0.30·N(recruiting) + 0.10·N(experience) + 0.05·N(award_tier)
```

Consequences, all measured:

- Recruiting is **67% of the only signal that varies**, which is why OL correlates r = 0.877
  with recruiting composite where every other position is under 9%.
- Maximum attainable composite is `0.25 + 0.30 + 0.10 + 0.05` = **0.65**, which the anchor table
  maps to exactly **88**. The documented "88 cap" is not a policy — it is arithmetic.
- **59 of 293** rated linemen in 2025 (20%) land on exactly **80.0**, the value at composite
  0.625, reached by any 4th-year lineman whose recruiting normalises to the top of its range.
- Within-position agreement with EA CFB 27 is **−0.274**. Negative.

### How class age contributes

Through `experience`, at weight **0.10** — 22% of the live signal. `experience` is
`player_seasons.year` normalised on `(1, 5)`. That field is documented elsewhere in this repo
as **not a class year**: constant across a career for 84% of players with three or more
seasons, and an outright calendar year for 114,612 of 269,552 rows. **A lineman's experience
term does not increase as he ages.** It is a fixed per-player constant.

### Coverage

Only ~9% of rostered linemen are rated at all (280 of 2,952 in 2025), because a lineman only
receives a stats payload if he happens to record a defensive stat — a tackle after an
interception, say. The rated linemen are close to a random sample, not the best ones.

**There is no per-lineman blocking data in the API.** See `API_INVENTORY.md`. The disposition
of this rating is in `ALTERNATIVES.md` §A.

---

## 3. Defense: EDGE, DL, LB, CB, S, DB

### The six inputs

Everything is built from `defensiveTOT`, `defensiveSACKS`, `defensiveTFL`, `defensiveQB HUR`,
`defensivePD`, `interceptionsINT`. Three more sit in the payload **unused**:
`defensiveSOLO` (all 402,156 rows), `fumblesFUM` and `fumblesREC` (42,328 rows).

### Front seven

```text
EDGE  pass_rush  = sacks×5.0 + hurries×1.5 + TFL×2.0
      disruption = (sacks + TFL) / max(TOT, 1)
      run_stop   = TFL×2.5 + (TOT − sacks)×0.3
DL    pass_rush  = sacks×5.0 + hurries×1.5 + TFL×1.0
      run_stop   = TFL×2.5 + (TOT − sacks)×0.4
LB    tackling   = TOT×0.5 + TFL×2.0
      coverage   = INT×3.0 + PBU×1.5
      instinct   = (INT + PBU + TFL) / max(TOT, 1)
```

Blend: EDGE `0.50 edge + 0.25 pass_rush + 0.12 disruption + 0.08 run_stop + 0.05 recruiting`;
DL `0.40 / 0.25 / 0.10 / 0.18 / 0.07`; LB `0.40 edge + 0.25 tackling + 0.15 coverage +
0.10 pass_rush + 0.10 recruiting`.

### Secondary — the overall *is* the three archetypes

Since v4.2 a defensive back's score is not a stat composite but three sub-scores on one 0–10
axis, weighted by position:

```text
ball_hawk   = INT×12.0 + PBU×3.5 + defTD×8.0
run_support = TOT×0.6 + TFL×4.0 + sacks×6.0 + hurries×1.5
coverage    = playing-time share × team pass-denial credit    (no box-score input at all)

scaled_k = clip(Σ(per-game k) / √games / ARCHETYPE_SCALE[k] × 10, 0, 20)
ARCHETYPE_SCALE = {ball_hawk: 12.9, coverage: 8.5, run_support: 14.8}

score = Σ weight[pos][k] × scaled_k
        CB 0.40 coverage / 0.40 ball_hawk / 0.20 run_support
        S  0.20 / 0.30 / 0.50            DB 0.33 / 0.33 / 0.33
```

`ARCHETYPE_SCALE` values are each archetype's 90th percentile, frozen like the anchors. They
**must be re-measured whenever an input changes** — stale constants once left coverage topping
out at 7.1 while run support reached 20. Last checked after v4.2: p90s of 9.9 / 8.4 / 10.0.

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

Tackles correlate **0.70–0.82** with the final OVR at every position, and at safety they are
the strongest single input. A tackle count is mostly snaps played × how often the opponent runs
at you × how long your defence is on the field — so a defence that gets off the field denies
its own players the statistic we reward them for. This is the central known weakness; options
are in `ALTERNATIVES.md` §B.

### Era

`ERA_ANCHORS`: modern 2018+, transition 2013–2017, classic 2008–2012. Classic-era defensive
thresholds are 75% of modern, compensating for hurries and pass breakups not existing before
2015. Pre-2016 DL and EDGE ratings should be read as recruiting-caliber estimates.

---

## 4. Playing-time tiers

Before any of the above, a player is bucketed by stat volume — starter (full formula), role
player (capped 78), reserve (capped 68, blended with recruiting), bench (capped 60,
recruiting only). Thresholds are per position, e.g. LB starter ≥ 20 tackles, CB ≥ 10.

---

## 5. Kickers and punters

```text
K   0.50 fg_pct + 0.25 fg_long + 0.15 xp_pct + 0.10 volume
P   0.55 avg_yards + 0.30 inside_20_pct + 0.15 volume
→ COMPOSITE_OVR_ANCHORS, ceiling 90
```

v4.2 pulled specialists down deliberately: EA rates 5 kickers at 85+ and exactly 1 punter,
against our pre-v4.2 17 and 38. The tell was a punter outranking the receivers on his own team
page. **Note:** script 07's distribution validator still expects K/P `mean 55–70` and the
shipped distribution means ~50, so specialists warn on every run — the bounds are stale, not
the ratings. Logged in `ROADMAP.md`.

---

## 6. Team ratings (script 10)

```text
team_rating = 0.50 SP+ + 0.30 our player ratings + 0.20 team stats
              (renormalised when a signal is absent — which is how 2026 works
               with neither SP+ nor stats)

pass_off = 0.45 avg_top(QB,2) + 0.35 avg_top(WR+TE,5) + 0.20 avg_top(OL,5)
run_off  = 0.40 avg_top(RB,3) + 0.40 avg_top(OL,5) + 0.10 avg_top(QB,2)
                                                    + 0.10 avg_top(WR+TE,5)
```

**`avg_top()` returns a hard-coded 50.0 when a position has no rated players.** That matters
for any change that removes a position group: OL is 40% of run offence, so withdrawing OL
ratings without renormalising would make 40% of every team's run offence an identical constant.

---

## 7. What every rating shares, and cannot escape

- It measures **counted, opponent-adjusted box-score production per game**. Not talent, not
  ability, not value.
- It cannot see **snaps** for two thirds of players, **blocking** for anyone, **coverage**
  except through a team proxy, or **missed tackles** at all.
- Two thirds of any roster has no meaningful production, so for them the number is mostly a
  recruiting grade in a production-shaped costume — which is exactly the OL problem in §2,
  differing only in degree.
