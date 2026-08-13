# The supplemental datasets — what we hold, what reads it, what it could do

*2026-08-13. Every row measured against the files on disk during the architecture review.*

Script 09 harvested seventeen datasets in v4.3 on the principle that the next question
should not begin with a week of "can we even get that". A year later the fair question is
the opposite one: **which of them is anything actually reading?**

This file answers that, and gives every unread dataset either a use with a test attached or
an explicit reason it stays unread. An option with no test attached is an opinion — the
standing rule from `ALTERNATIVES.md`, applied here.

**Status key:** 🚀 in use · ✅ recommended, test named · 🔬 worth an experiment ·
⛔ tested and rejected · 💤 held, no use proposed · 🐛 broken as harvested

---

## What reads what, today

| Dataset | Rows | Seasons | Read by | Status |
|---|---:|---|---|---|
| `team_advanced_season` | 2,295 | 2008–2025 | scripts 06 (opportunity index, havoc), 10 (line unit) | 🚀 |
| `coaches` | 2,584 | 2008–2026 | `utils/coaching.py`, script 10 | 🚀 |
| `draft_picks` | 4,858 | 2008–2026 | `utils/draft.py`, `validate_vs_draft.py` | 🚀 |
| `team_advanced_games` | 35,422 | 2008–2025 | **nothing** — but it is what disproved the per-game denominator | ⛔ |
| `team_havoc_games` | 22,876 | 2008–2025 | nothing | 💤 |
| `betting_lines` | 43,489 (38,396 with a spread) | 2013–2026 | nothing | ✅ |
| `pregame_wp` | 10,126 | 2014–2026 | nothing | ✅ |
| `returning_production` | 1,691 | 2014–2026 | nothing | ✅ |
| `player_success` | 32,080 | 2014–2025 | nothing | 🔬 |
| `player_wepa` | 9,044 (7,310 usable) | 2014–2025 | nothing | 🔬 |
| `team_talent` | 2,278 | 2015–2025 | nothing | 🔬 |
| `team_ratings_external` (Elo) | 10,409 | 2008–2026 | nothing | 🔬 |
| `team_records` | 6,773 | 2008–2025 | nothing | ✅ |
| `team_ats` | 1,493 | 2019–2025 | nothing | 💤 |
| `game_weather` | 24,001 | 2008–2025 | nothing | 🔬 |
| `cfp_participants` | 24 | 2024–2025 | nothing | ✅ (as ground truth) |
| `venues` | 844 | — | nothing | 🐛 |

Three of seventeen are consumed. That is the gap this file is about — but note the shape of
it: the unread datasets are overwhelmingly **team-and-game level**, which is the playoff
model's raw material, not the player rating's. The player-level supplements
(`player_success`, `player_wepa`) are thin and offence-only.

---

## Dataset by dataset

### 🚀 In use

**`team_advanced_season`** — the load-bearing one. Both the defensive opportunity index
(script 06) and the entire line-unit rating (script 10 via `utils/line_unit.py`) come from
here. If script 09 has not run, both silently degrade to no-ops, which is why script 06
prints which of them are active.

**`coaches`** — 2,584 coach-seasons with per-season W/L, SRS and SP+ splits. Powers the
coaching event study (carry-over r = +0.343 across 101 coaches). Head coaches only; the
retired seed CSV had coordinators and the API does not.

**`draft_picks`** — 4,858 picks, 4,010 joining to our players (83.7%). The only external,
historical, backtestable check the ratings have.

---

### ⛔ Tested and rejected

**`team_advanced_games`** (110 MB, 35,422 team-games, complete 2008–2025)

Carries `defense_plays`, `defense_ppa`, `defense_successRate`, `defense_stuffRate`,
`lineYards` and standard/passing-down splits *per game*. The obvious use is a per-game
version of the defensive opportunity index, and it looked compelling: **78% of the variance
in defensive plays faced is within a team-season**, which a season-level index cannot see
at all, and the median team's easiest and hardest game differ by 0.311 of index against a
clip band only 0.35 wide.

Tested exactly as v4.3 tested the season index — within-position Spearman against EA, 2024
and 2025, denominator applied per game inside the sum:

| variant | mean Spearman | Δ |
|---|---:|---:|
| no denominator | 0.5339 | — |
| season index (shipped) | 0.5448 | **+0.0109** |
| per-game index | 0.5417 | +0.0078 |
| placebo (plays shuffled across teams) | 0.5355 | +0.0016 |

The finer measurement is worse than the coarser one. Game-to-game play count is mostly
tempo and game script — noise about the player — while the season average is the part that
carries information about his opportunity.

**Verdict:** the file earned its 110 MB by settling this and should now be slimmed to the
columns a future consumer names, or dropped to season level. Do not delete the finding.

---

### ✅ Recommended, with the test named

**`betting_lines`** — 38,396 games carrying a spread, 2013–2026, joined on `game_cfb_id`.

Two distinct uses, and they must not be confused:

1. **Benchmark for the playoff model.** The closing spread is what a market that knows
   injuries, weather and lineups believes. Measured this week: a preseason model on
   prior-season SP+ scores Brier **0.2136** on held-out 2021–2025; the closing spread scores
   **0.1816**. That gap is the honest headroom, and publishing it beside our number is the
   difference between a demo and a defensible claim.
2. **A validation target for team ratings** — but only in the lagged, preseason form.
   Scoring our same-season team rating against the same season's games is circular, because
   the rating is built from that season's SP+ and that season's player ratings.

*Test before shipping:* out-of-sample Brier and a reliability curve, with the market shown
as a ceiling rather than a competitor.

**`pregame_wp`** — 10,126 rows of the provider's own pregame win probability, 2014+.
The second benchmark, and a cheaper one than the spread because it is already a probability.
*Test:* our Brier against theirs on identical games.

**`returning_production`** — 1,691 team-seasons, `percentPPA` and `usage` returning, 2014+.

The covariate script 13's residual is missing: it separates "beats the roster they
recruited" from "beat it this specific year with everyone back". Two cautions, both
measured:

- Added to a preseason game model it improves AUC (0.706 → 0.710) and accuracy (65.0% →
  66.6%) while leaving Brier flat — it sharpens the ordering without improving calibration.
- Its raw correlation with the *change* in SP+ is **−0.136**, which reads backwards until
  you see the confound: good teams lose more players to the NFL, so high returning
  production is partly a marker of having been bad. It must be used conditional on current
  level, never as a standalone "returning production is good" claim.

*Test:* does adding it to script 13's regression reduce residual SD below 9.82, and does the
residual's year-over-year persistence (0.607) fall — which would mean it was partly
capturing roster continuity all along?

**`team_records`** — 6,773 team-seasons including `expectedWins`, home/away/neutral splits
and conference records. The Monte Carlo layer of the playoff model needs exactly this shape,
and `expectedWins` is a ready-made sanity check on any simulated win total.
*Test:* simulated win totals must track `expectedWins` at r > 0.9 or the simulation is
wrong before the bracket is ever built.

**`cfp_participants`** — 24 rows, 2024 and 2025 only, with seed, bid type, committee rank
and elimination round. This is the playoff model's **ground truth**, and it is two seasons
deep. That constrains the claim, not the build: game-level Brier over 2021–2025 is the
defensible headline, and "how many actual playoff teams were in our top 12" is an anecdote
until there are more seasons of the 12-team format.

---

### 🔬 Worth an experiment

**`player_success`** — 32,080 player-seasons, 2014–2025, joins to us at **65.7%**.

Per-player **play counts** and success rates, for passing and rushing only (there is no
receiving equivalent). Coverage after joining: ~400 QB and ~700 RB per season, thin
everywhere else.

What it already told us, which is the point: **our offensive rating correlates 0.84 (QB)
and 0.87 (RB) with a player's raw play count, and only 0.52 and 0.30 with his success
rate.** The rating is substantially a volume measure. That is not automatically wrong —
volume is opportunity and coaches give opportunity to good players, and `games` is the
strongest predictor of being drafted — but it is a fact the site should state rather than a
critique to defend against.

*Test if built:* publish success rate as a labelled efficiency column beside the rating; it
must not enter OVR without beating a shuffled placebo the way the coverage credit did.

**`player_wepa`** — 9,044 rows, joins at **99.9%**, but only 7,310 are usable: every one of
the 1,734 `kicking` rows has a null `wepa` (see 🐛 below). Usable coverage is QB passing
(2,322) and RB rushing (4,988). Opponent-adjusted EPA computed by someone else.

*Use:* an independent check on the offensive rating, not an input. Two models that share no
code agreeing is worth more than either agreeing with itself.
*Measured already:* Spearman(our OVR, wepa) = 0.673 QB, 0.431 RB.

**`team_talent`** — 2,278 team-seasons of the 247 composite talent number, 2015–2025.
Script 13 builds its own talent proxy by aggregating `recruiting.json`. Using the published
composite instead would remove one hand-rolled step from the finding most exposed to the
"you are just restating recruiting" critique.
*Test:* does substituting it change the residual ranking? If the top and bottom twenty are
stable, the finding is robust to the talent proxy and can say so.

**`team_ratings_external`** (Elo) — 10,409 team-seasons, 2008–2026, and the only external
team rating we hold that is *not* SP+.
*Measured:* our team rating agrees with Elo at 0.914; SP+ agrees at 0.939. So Elo mostly
confirms that our team rating is a re-expression of SP+ (see the review's §5a).
*Use:* an ablation column, not an input.

**`game_weather`** — 24,001 games, 2008–2025, with temperature, wind, precipitation and an
indoor flag.
*Use:* a game-level covariate for the playoff model, and a genuine confound control for
kicker ratings — `FORMULAS.md` §5 already discloses that field-goal percentage is confounded
by distance and by which kicks a coach attempts; wind is a third confound and it is on disk.
*Test:* does adding wind and precipitation to the game model improve Brier out of sample? If
not, drop it — it is a plausible story, and plausible stories are what the placebo tests are
for.

---

### 💤 Held, no use proposed

**`team_havoc_games`** — 22,876 team-games. Superseded for the denominator question by
`team_advanced_games` (which covers ~1,550–3,300 team-games a season against this file's
~500–640 before 2016), and havoc share itself was measured at zero in v4.3.

**`team_ats`** — 1,493 team-seasons, 2019–2025, against-the-spread records. Interesting for
a betting-adjacent question this project does not ask.

---

### 🐛 Broken as harvested

**`player_wepa`, kicking rows** — 1,734 rows written with `wepa` null in every one. The
harvest stored the envelope of an empty response. Either the endpoint has no kicking wepa or
the parameter is wrong; either way the rows should not be on disk claiming to be data.

**`venues`** — 844 rows with `season = 0` and `team_id = null`. As harvested it joins to
nothing. Venue elevation and dome status are real covariates for a game model, so this is
worth a re-harvest that resolves the team, not a deletion.

---

## The order I would wire them in

1. `returning_production` into script 13 — smallest change, sharpest question, and it makes
   an existing published finding more defensible rather than adding a new one.
2. `betting_lines` + `pregame_wp` as a benchmark harness — before any playoff model exists,
   so the target is fixed before the result is seen.
3. `team_records` as the Monte Carlo sanity check.
4. `team_talent` as a robustness check on script 13.
5. `player_success` / `player_wepa` as published cross-checks on the offensive rating.
6. `game_weather` only if the game model exists and asks for it.

Nothing on this list changes a player's OVR. That is deliberate: after the reliability
measurement (`ARCHITECTURE_REVIEW_2026-08.md` §1), the case for adding inputs to the
defensive rating is weak, and the case for adding *context* to the team and game models is
strong.
