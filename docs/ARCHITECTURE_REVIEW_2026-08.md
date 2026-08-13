# Architecture review — the foundation, measured

*2026-08-13. Review and planning. Everything below was measured against the data on disk
during this review; the scripts that produced each number are named beside it.*

The brief was: the foundation is not solid enough to build playoff prediction and roster
analysis on top of, so fix the rating and therefore the projection, and start using the
seventeen supplemental datasets that have been harvested and left unread.

The review agrees with the premise and disagrees with the diagnosis. The foundation's
problem is not that the formulas are wrong. It is that **nobody had measured how much of a
rating is signal**, so every argument about improving it — including the roadmap's — has
been conducted without the one number that decides which improvements are even possible.

That number now exists. `scripts/validate_reliability.py`, new in this pass.

---

## 1. The finding that reorders everything

Build each player's season composite twice — odd weeks and even weeks — and correlate the
halves. Spearman-Brown corrects that to what a full season's self-agreement would be. The
square root of it is the **noise ceiling**: the highest correlation this measurement could
have with *any* perfectly measured truth, including EA, the NFL draft, and the player's
own next season.

| position | n | split-half | reliability | noise ceiling |
|---|---:|---:|---:|---:|
| QB | 1,697 | 0.779 | **0.876** | 0.936 |
| RB | 4,056 | 0.790 | **0.883** | 0.940 |
| WR | 5,883 | 0.725 | **0.840** | 0.917 |
| LB | 6,410 | 0.648 | 0.786 | 0.887 |
| TE | 1,594 | 0.600 | 0.750 | 0.866 |
| S | 2,590 | 0.520 | 0.684 | 0.827 |
| EDGE | 1,981 | 0.479 | 0.648 | 0.805 |
| DL | 6,187 | 0.457 | 0.627 | 0.792 |
| DB | 4,755 | 0.438 | 0.609 | 0.780 |
| CB | 2,073 | 0.420 | **0.591** | 0.769 |

*2016–2025, ≥3 games in each half, 37,226 player-seasons.*

A corner's first half-season and his second half-season agree at 0.42. Not with EA, not
with the draft — **with himself**. Whatever we compute from that, the ceiling on its
agreement with the truth is 0.77.

Now put the shipped rating's year-over-year persistence beside that ceiling:

| position | pairs | persistence | ceiling | share of ceiling reached |
|---|---:|---:|---:|---:|
| DB | 6,343 | 0.538 | 0.609 | **0.88** |
| CB | 2,892 | 0.491 | 0.591 | **0.83** |
| EDGE | 2,012 | 0.516 | 0.648 | 0.80 |
| DL | 6,649 | 0.473 | 0.627 | 0.75 |
| TE | 3,893 | 0.548 | 0.750 | 0.73 |
| LB | 7,671 | 0.550 | 0.786 | 0.70 |
| S | 3,411 | 0.473 | 0.684 | 0.69 |
| RB | 6,995 | 0.587 | 0.883 | 0.66 |
| WR | 10,814 | 0.551 | 0.840 | 0.66 |
| QB | 3,519 | 0.555 | 0.876 | **0.63** |

Read the last column as *how much of the achievable year-over-year signal we already
extract.* It inverts the roadmap:

- **Defensive backs are at 83–88% of their ceiling.** Their projections are not bad because
  the model is weak. They are bad because a corner's season is barely measurable, and the
  things that would measure it — targets allowed, coverage snaps, missed tackles — do not
  exist in any source (`API_INVENTORY.md`, verified by key scan). **No feature will fix
  this. Only a new measurement would, and there is not one to buy.**
- **Quarterbacks are at 63%.** QB production is measured cleanly (0.876) and predicted
  poorly (0.555). That gap is real change the model is not capturing — and it is the
  largest modelling headroom in the system.

So the queue is upside down. The defensive rating, which the roadmap makes the headline,
is close to done in the only sense available to it. Offensive projection, which the
roadmap treats as solved, is where the remaining accuracy lives.

---

## 2. Four things that were going to be built, tested first

Each of these was on the roadmap or implied by it. Each was measured this week. Three
failed, one is small and real. Every experiment is reproducible from the tables on disk.

### 2a. Per-game defensive denominator — **rejected**

The v4.3 opportunity index is one clipped number per team-season. `team_advanced_games.json`
(110 MB, harvested, never read) carries `defense_plays` for **35,422 team-games, complete
2008–2025**, and 78% of the variance in plays faced is *within* a team-season, invisible to
a season-level index. Obviously worth doing.

It is not. Scored the way v4.3 scored the season index — within-position Spearman against
EA, 2024 and 2025, with the denominator applied per game inside the sum the EDGE score
already builds:

| variant | mean Spearman | Δ vs no denominator |
|---|---:|---:|
| A — no denominator | 0.5339 | — |
| B — season index (**shipped**) | 0.5448 | **+0.0109** |
| C — per-game index (proposed) | 0.5417 | +0.0078 |
| P — placebo, plays shuffled across teams | 0.5355 | +0.0016 |

The finer-grained version is *worse* than the coarser one it would replace. Plays faced
varies game to game mostly through tempo and game script, which is noise about the player;
the season average is the part that carries information. Same shape as the havoc-share
result in v4.3 — and the same conclusion: keep B, publish C's failure.

### 2b. Fitted defensive weights — **rejected, and the reason matters**

`DEF_STAT_WEIGHTS` (sacks 7.0, hurries 2.5, tackles 0.3 …) is hand-set and has never been
fitted to anything. The draft is 18 years deep and was decided without seeing our numbers,
so: fit a logistic model on the identical inputs, train ≤2018, score 2019–2025 on whether
the player was drafted out of that season or the next two.

| position | AUC fitted | AUC shipped OVR | AUC raw edge_score |
|---|---:|---:|---:|
| EDGE | 0.816 | 0.850 | 0.851 |
| DL | 0.787 | 0.794 | 0.806 |
| LB | 0.825 | 0.839 | 0.844 |
| CB | 0.745 | 0.821 | 0.833 |
| S | 0.776 | 0.850 | 0.853 |
| DB | 0.763 | 0.818 | 0.826 |
| **mean** | **0.785** | **0.829** | **0.836** |

The hand-set weights beat the fit at every position. **The weights are not the problem; the
inputs are** — which is the same answer §1 gives, arrived at independently.

Two corrections this forces on the current story:

- The roadmap's headline argument — "our defensive ratings order draft picks at 0.13–0.25
  against 0.42–0.49 for offence" — is measuring rank agreement *among players who were
  already drafted*, the hardest and most selected slice. On the plain question *does this
  rating separate the drafted from the undrafted*, defence scores **AUC 0.83**. Both numbers
  are true and they support opposite conclusions. Publish both.
- `games` is the strongest single fitted feature at almost every position. Availability is
  not a confound to be scrubbed out of a production rating — it is most of what the draft
  itself rewards.

### 2c. Usage (snap share) — **the coverage is the opposite of what the roadmap says**

The roadmap makes snap share the next phase, on the grounds that it "matters most for
defence". Measured over **rated** player-seasons rather than over all stat rows:

| group | rated player-seasons carrying a nonzero `snap_pct` |
|---|---|
| QB | 94–99% every season 2013+ |
| RB / WR | 88–96% |
| TE | 81–93% |
| **all defence** | **15 rows out of 43,008 since 2016 — 0.03%** |
| K / P | 0% |

Snap share and PPA are offence-only. The dataset the roadmap earmarked for the defensive
fix does not reach defence at all, and its coverage of offence is far *better* than the
29.8% the roadmap records (that figure counts every stat row, most of which belong to
players nobody rates).

So the usage question is entirely an offensive question, and there it is answerable:

| features (same 11,342 pairs, train ≤2020, test 2021–24) | MAE | Spearman |
|---|---:|---:|
| this season's OVR alone | 8.851 | 0.512 |
| + snap share | 8.853 | 0.518 |
| + per-snap efficiency | 8.813 | 0.521 |
| + both | **8.800** | **0.524** |

Snap share alone buys nothing. **Per-snap efficiency** buys 0.04 MAE and +0.013 Spearman —
real, replicated at QB (−0.19 MAE) and WR (−0.14), absent at RB, negative at TE. That is a
worthwhile sub-rating and an honest one. It is not a foundation fix, and the roadmap's
framing of it as the headline should be retired.

### 2d. Reliability-weighted career blending — **inconclusive, do not ship yet**

If one defensive season is 0.59 reliable, blending it with the player's own prior seasons
should help most exactly where reliability is lowest. It does the opposite: the correlation
between a position's reliability and the blend's benefit is **−0.67**. Blending helps QB
(−0.23 MAE), RB (−0.14), WR (−0.12), DL (−0.35); it hurts DB (+0.10), S (+0.10).

The likely reason is that a noisy position's career prior is itself built from noisy
seasons, and that the OVR has already been shrunk once by the playing-time tiers. The idea
is not dead — it needs the blend fitted per position rather than assumed equal to
reliability — but it is not a result yet.

---

## 3. What works, and should not be touched

Stated because a review that only lists problems gets the priorities wrong.

- **The absolute-anchor design.** Fixed `EDGE_OVR_ANCHORS` and `COMPOSITE_OVR_ANCHORS`,
  era-bucketed, never rank-matched to a pool. This is the decision that makes 2012 and 2025
  comparable and it is worth more than any accuracy tweak proposed below.
- **The withdrawal of the OL player rating.** Refusing to publish a number is the strongest
  thing this project has done. The line-unit replacement validates at +0.179 against
  linemen drafted where its predecessor scored −0.274 against EA.
- **The placebo discipline.** Shuffled-control tests killed havoc share in v4.3 and killed
  2a and 2b in this pass. Very few hobby projects have this and it is why the negative
  results above are trustworthy.
- **The documentation set.** `FORMULAS.md` / `ALTERNATIVES.md` / `API_INVENTORY.md` are
  better than most production analytics teams maintain, and `methods.html` publishes them.
- **Test suite: 283 passing** (`CLAUDE.md` still says 41 — corrected).
- **The season contract.** `manifest.json` and `js/config.js` agree, enforced by
  `tests/test_export_contract.py`.
- **`avg_top()` returning `None`.** The rule that an absent measurement must never become an
  average one is applied universally, and it is the correct instinct throughout.

---

## 4. Defects found in code and docs

Ordered by how much they can mislead a reader or cost a run.

| # | Where | What | Severity |
|---|---|---|---|
| 1 | `06_compute_edge_scores.py:228-256` vs `FORMULAS.md` §0, `RATING_AND_PROJECTION_MODEL.md`, `js/methods.js:72` | The opponent multiplier is documented as symmetric `[0.55, 1.45]`. The code is **asymmetric: `1 + z·0.35` up to 1.70, `1 + z·0.12` down to 0.76**. Three documents and the public methods page state a formula the code does not implement — on the page whose premise is "as the code actually computes it". **Fixed in this pass.** | high (credibility) |
| 2 | `07_compute_player_ratings.py:1192` | `STAT_FALLBACK_TARGETS` maps the no-EDGE pool through `np.percentile(...)` — pool-relative scaling, the exact mechanism `AUDIT_FINDINGS.md` §9 forbids. Bounded (caps at 78) and cross-season pooled, so the damage is small, but the guarantee as written is not true of every path. | medium |
| 3 | `07_compute_player_ratings.py:1521` + `main()` | `rate_position()` loads and re-rates **every season** of a position to return one season, and `main()` loops seasons on the outside. A `--all-seasons` run therefore rebuilds the full cross-season frame 19×12 times and rewrites the 112 MB `ratings.json` 19 times. The v4.3 fix cached the *raw tables*; the quadratic work is the per-row feature loop above them, which is still there. | medium (run time) |
| 4 | `07_compute_player_ratings.py:78` | `classify_playtime_tier` falls back to `volume_score` when the position's canonical stat key is missing. `volume_score` is a different quantity on a different scale, so the tier thresholds (`QB starter ≥ 100 passingATT`) are being applied to a number that does not mean attempts. | medium |
| 5 | `data/computed/ratings.json` | 112 MB holding every engine in one file, rewritten whole on every season of every run. Should be split per engine (and probably per season) — `read_ratings()` already exists as the only correct way to read it, which is a workaround for the shape rather than a fix. | medium |
| 6 | `player_wepa.json` | 1,734 rows with `wepa_kind = "kicking"` and **`wepa` null in every one**. The harvest wrote the envelope of an empty response. | low |
| 7 | `venues.json` | 844 rows with `season = 0` and `team_id = null` — as harvested it cannot join to anything. | low |
| 8 | `cfp_participants.json` | 24 rows, 2024–2025 only. This is the playoff model's *ground truth* and it is two seasons deep. Any backtest claim must be framed around that, not around bracket accuracy. | low, but plan-shaping |
| 9 | frontend `data/` | `players_2025.json` is 13.7 MB and the grid needs a dozen fields; `trajectory_detail.json` 5.5 MB. Already in the roadmap's debt list; still true. | low |

---

## 5. Two structural conclusions

### 5a. The team rating restates SP+

Our team rating correlates 0.914 with Elo across 2,303 team-seasons; SP+ alone correlates
0.939. On the test that matters for the playoff model — **preseason** prediction of the next
season's games, everything lagged one year, held out on 2021–2025:

| predictor | Brier | log loss | AUC | accuracy |
|---|---:|---:|---:|---:|
| home field only | 0.2435 | 0.680 | 0.512 | 57.8% |
| prior-season SP+ | **0.2136** | 0.615 | 0.706 | 65.1% |
| our prior-season team rating | 0.2154 | 0.619 | 0.700 | 64.6% |
| ours + SP+ | 0.2137 | 0.615 | 0.706 | 65.0% |
| ours + SP+ + returning production | 0.2140 | 0.617 | **0.710** | **66.6%** |
| market closing spread *(knows injuries; a ceiling, not a competitor)* | 0.1816 | 0.537 | 0.795 | 72.0% |

Our rating adds **nothing** to SP+ (coefficient +0.0065 against SP+'s +0.0595; Brier
unchanged at the fourth decimal). That is not a failure — the team rating is 50% SP+ by
construction — but it means the playoff model cannot be sold as *our* team rating driving
it. It is honest to build the playoff model on prior-season SP+ plus our projected roster
strength, and to publish the ablation showing what each contributes.

### 5b. Two thirds of every roster is a recruiting grade in a production costume

`apply_multi_tier_treatment` blends the formula with `fallback_rating(stars, pos_avg)`, and
for the `bench` tier the rating *is* `pos_avg + STARS_OVR_DELTA[stars]` — a six-valued step
function. `FORMULAS.md` §7 says this plainly; the site does not. A backup's 54 and a
starter's 54 are different kinds of object and nothing in the data model distinguishes them.

This is the same category error as the withdrawn OL rating, differing only in degree, and
the fix is the one already proposed in the model doc as "Production and Talent as two
numbers". It is now cheap: every row already knows its tier.

---

## 6. The plan

Ordered so that each phase's gate is a measurement, and so that nothing downstream is built
on a number that has not passed one.

### Phase 0 — make the foundation legible (days, no accuracy claims)

1. **Ship reliability.** `scripts/validate_reliability.py` exists and exports
   `data/reliability.json`. Add it to the pre-ship checklist beside `validate_ratings.py`
   and `validate_vs_draft.py`. *Gate: none — it is a measurement.*
2. **Publish `rating_basis` on every rating row** — `production` / `blended` / `recruiting`
   / `withheld` — derived from the tier that already exists. The UI shows it as a chip.
   *Gate: `tests/test_export_contract.py` asserts every rated row carries one.*
3. **Per-position confidence from reliability.** A CB's interval must be wider than a QB's
   because his measurement is worse, not because his model residuals happened to be. Feed
   `noise_ceiling` into the published interval width.
4. **Fix the defects in §4**, items 1–4 first. Item 1 is done.

### Phase 1 — the rating, where reliability says there is room

5. **Offensive per-snap efficiency as a published sub-rating**, not folded into OVR.
   Coverage 88–99% of rated offensive players 2013+; worth 0.04 MAE and +0.013 Spearman on
   projections (§2c). Marked absent, never zero, before 2013.
   *Gate: distribution shape unchanged; the sub-rating must beat its own shuffled placebo.*
6. **Production vs Talent as two numbers** for the sub-threshold two thirds (§5b).
   *Gate: it is a labelling change — the gate is that no OVR moves.*
7. **Defence: stop adding machinery.** State the ceiling on the player page, widen the
   intervals, and publish both draft numbers (AUC 0.83 separation, 0.13–0.25 ordering).
   The only defensive idea left with headroom is a properly fitted multi-season blend
   (§2d), and it must clear a per-position fit before it ships.

### Phase 2 — the projection

8. **Separate the point estimate from the interval** (`ALTERNATIVES.md` D7). The compressed
   spread (P2) and the 80% coverage target are in direct tension on one dial, and §1 says
   part of the "missing" spread is measurement noise that *should not* be predicted.
9. **QB and WR first.** They sit at 63–66% of their ceiling — the largest headroom in the
   system. Candidate features: per-snap efficiency (5), transfer context (D4), returning
   production of the offence around him.
   *Gate: MAE on the same held-out split used in §2c, against 8.800.*

### Phase 3 — the playoff model (unblocked, with a caveat)

10. Build it on **prior-season SP+ + projected roster strength + returning production**,
    with our team rating included and ablated honestly (§5a). Benchmarks are on disk:
    38,396 games with closing spreads 2013+, 10,126 pregame win probabilities.
    *Gate: Brier ≤ 0.214 preseason on held-out seasons, calibration curve published, and
    the market's 0.182 shown beside it as the ceiling.*
11. **Say what the backtest can be.** `cfp_participants` is 24 rows over two seasons, so
    "how many playoff teams did we have in our top 12" is an anecdote for now. The
    defensible headline is game-level Brier and calibration over 2021–2025.

### Phase 4 — roster construction (still blocked, but less than it looks)

12. Transfer analysis does not need NIL and is designed already: two-part outcome
    (`P(rated at the new school)` then performance | playing), matched stayers, 18,885
    portal rows at 96.4% linked. NIL remains unsourced; program-level spend stays the
    target if a source is ever found.

---

## 7. What I am not recommending, and why

| Idea | Why not |
|---|---|
| Per-game defensive denominator | Measured worse than the season index it would replace (§2a) |
| Fitting the defensive weights | Measured worse than the hand-set weights, out of sample (§2b) |
| Snap share as the next phase | Does not exist for defence; ~0 for offensive projection (§2c) |
| More defensive feature engineering | DB/CB are at 83–88% of their measurement ceiling (§1) |
| Rebuilding the team rating | It restates SP+, and SP+ is free and better (§5a) |
| EA-derived per-player OL blocking | One season, not backfillable, unbacktestable by construction |
| Deleting `team_advanced_games.json` | Now read: it is what proved §2a. Keep, but slim to the columns used |

---

*Companion documents: `SUPPLEMENTAL_DATA.md` (what each harvested dataset is, whether
anything reads it, and the test each candidate use must pass), `ROADMAP.md` (revised to
match these measurements), `FORMULAS.md`, `ALTERNATIVES.md`.*
