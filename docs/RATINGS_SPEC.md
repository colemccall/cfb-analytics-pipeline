# Ratings spec — what the system is, as of v4.25.1 (2026-11-06)

The one document that says what these ratings are, how every number is made, what has been measured about it, what is
decided by the owner and what by the data, and what is still wrong. It is written for the owner and for a reader who wants
to check a claim. Everything here is either copied from a validator's output or from a Build Log entry, or generated from
the code (the constants table in §12 is written by `scripts/spec_constants.py`, and `tests/test_ratings_spec.py` fails if it
drifts from the code). The site carries the same text as the Methods entry `system-spec` (About › Methods); registry prose and
this file change together or not at all (root `CLAUDE.md` §2).

It describes the system; it does not replace the longer documents it points to: `FORMULAS.md` (each formula as the code
computes it), `ALTERNATIVES.md` (what was tried and rejected), `RATING_AND_PROJECTION_MODEL.md` (the history),
`DATA_FLOW.md` (the path), and the Build Log (`menu.html#/changes`, what each pass changed and cost).

---

## 0. Purpose and principles

**Build our own ratings; do not match anyone else's.** No SP+, poll, betting line or video-game rating is an input to any
number. SP+ is published beside the team rating as a reference column and nothing reads it for the rating's shape; EA CFB 27
was removed from the project in v4.7 after it was found to have supplied a number for 273 players. An outside number is a
reference or a baseline, never a target: the committee's seed and the closing spread appear in §8 only as the things we are
measured against.

1. **A number that cannot show its evidence does not ship.** Every change to a rating, projection or finding states the defect
   with a number, runs a placebo that breaks the selection and not only the values, compares itself with the dumb baseline,
   states what it costs, and publishes the result when it fails. Rejected options stay in `ALTERNATIVES.md` and in the
   `alternatives` of the Methods entry.
2. **Anchors are absolute.** The score-to-rating map is fixed once; if nobody reaches the top of the scale in a season,
   nobody rates 99 that season. That is what makes a 2012 number comparable with a 2025 one. Percentile scaling of any
   position is a bug.
3. **Missing means unknown, never zero.** A withheld number (an offensive lineman) is not a missing one and does not look
   like one. A feature at 30% coverage has an explicit unknown branch.
4. **Provenance is structural.** A projected number renders as projected because the season says so, not because a caller
   remembered. A rating says what it is built from (`rating_basis`).
5. **Owner settings are labelled as the owner's.** They are the FCS weight, the headline's three weights and the
   quarterback score's weights. The data did not set them, and the constants table (§12) says so in its "Set by" column.
6. **2024 and 2025, the seasons and their playoffs, are the best markers of the current state of football.** §8 tests the
   system against them with only what was known the day the field was picked.
7. **No preseason or last-season number enters a rating of a season in progress.** A live rating is the games played so
   far and nothing else.

## 1. The system

Two sibling repositories. `cfb-analytics-pipeline` is Python and writes local JSON (no database; `utils/store.py` is the whole
persistence layer). `cfb-analytics-app` is a static site (GitHub Pages, no build step, no framework); `js/dataLoader.js` is
the only code that fetches. Both are under git; nothing is pushed without the owner's say, because pushing the app publishes it.

```
CollegeFootballData API -> .cache -> data/raw -> 06 EDGE -> 07 player ratings -> 10 team ratings -> 11 -> 15 -> 16 -> 10 (projected)
  -> 13 -> 14 -> data/computed -> 12 export -> cfb-analytics-app/data -> js/dataLoader.js -> menu.html
a season in play:  17 (to date) and 17b (once per completed week) run 06 -> 07 -> 10 in a private copy of the data, offline
```

All of 06 runs before all of 07 (interleaving leaves stale pool averages). Recomputes run offline: the children get a dead proxy,
so a cache miss cannot reach the API and any attempted request fails the run. A season in play is refreshed with a few requests
(`01b`), not a re-harvest. The site explains itself through four registries (`METHODS`, `FINDINGS`, `CHANGES`, `GUIDE`), and every
substantive change ships with an entry in the same commit.

## 2. Player ratings

**The score.** Each game's box score is turned into a position composite, multiplied by an opponent adjustment, summed over the
season and divided by the square root of games played. That is the EDGE score: opponent-adjusted production per root game.

- *Offense.* Per-position composites of yards, touchdowns and turnovers. The quarterback's is the owner's judgement: 1.05 per passing
  yard, 0.6 per rushing yard, 25 per passing touchdown, 18 per rushing touchdown, −20 per interception, and half of the score
  restated at the median 28 attempts (floor 10) so a passer who does more with each throw is not beaten by volume. The blend was
  chosen on held-out data (R² 0.3983), not on a player.
- *Defense.* Hand-weighted counters. A tackle is split into solo (×1.25) and assist (×0.65); sacks and tackles for loss count as
  the halves the API records (v4.24); a defender's production is scaled by an opportunity index (what his defense faced), clipped to 0.85 to 1.2
  and applied only where a team has 300 or more defensive plays on file. Cornerbacks, safeties and defensive backs are rated on three jobs, ball
  hawk, run support and coverage, with coverage credited from what his defense allowed through the air (cornerback 4.2, defensive back 3.7, safety
  3.0), because a covered receiver produces no statistic; the jobs and their weights are on the card.
- *Kickers and punters* are rated from their composites through their own anchors.

**The opponent adjustment.** Every game is scaled by the strength of the side the player faced, from our own results: an
offense rating and a defense rating per team, solved from the season's scored games with a conference ridge (team 3.0,
conference 0.5), cross-conference games weighted 1.5 and the postseason 1.0. A non-FBS opponent counts at a flat 0.85 (the
owner's setting; the data say 0.88 raw and 0.95 after FBS adjustment). No SP+ and no player rating goes in, so there is no loop.

**The score to a rating.** A fixed piecewise-linear map per position, 30 to 99, the anchors frozen (§12). They were set once, as the
score of the Nth-best player of a median 2021-2025 season, with the external video-game consensus consulted only to ask whether
the ceiling was in the right place. Offensive skill positions put the weakest season's best player at 96 so the country's best
always reads like one; defensive positions put the typical season's best at 96 so only a historic one nears 99. Pre-2016 EDGE and
DL, which have no sacks or tackles in the data, are rated from interceptions and recruiting and capped at 88 and 85.

**What a rating is built from.** A starter (a count of the position's own volume stat above a frozen threshold) gets a
production rating; a role player or reserve is blended with his recruiting grade; a bench player is a recruiting grade, the position
average shifted by stars. Every row carries `rating_basis` (production, blended, recruiting, withheld) and the site chips it, so a
backup's 54 is not read as a starter's 54. A finished season applies a games confidence of max(0.25, games/8).

**One or two games.** A player with one or two productive games is rated from his recruiting grade plus what he did, weighted by how few
games it was and how busy he was (weights 0.115 to 0.209), capped at 72, basis blended. Since v4.27 two games in a season being played are rated like three: his
per-game level through the three-game line of the part-season curve, basis production. One game, and two games in a finished season, stay on the rule above.

**Offensive linemen carry no rating.** No individual blocking data exists in any public source, and the number that used to be
there (a recruiting rank plus a constant) correlated 0.877 with the recruiting composite and −0.274 with the video-game rating.
It was withdrawn in v4.3 and is replaced by a line-unit rating on the team page, validated against draft outcomes.

## 3. Ratings for a season in progress, and week by week

**The level he has played at, believed by the games he has played** (v4.24). EDGE_level = EDGE_raw × √(12 / n), with n the games *he*
has played (what a 12-game season at his level scores); the rating is a + b × the anchored level, with (a, b) fitted for each
position family and each count of games (b from 0.30 to 0.91, smaller the fewer the games and the noisier the position; eight or more
games use the 8-9 line). Nothing in it reads how many games his team has played, so a game he sat out changes neither his score
nor his curve. Fitted on 86,717 player-cuts at weeks 4, 6, 8 and 10 of 2021-2025 so that the rating predicts how he plays afterwards. The
cost, stated: a player whose level rates 85 or more plays on average 3.6 points lower afterwards, so the curve lowers the top of a short
sample, and a mid-season rating for a high performer sits below what a finished season gives.

Earlier rules, kept as history: v4.20 rescaled by √(team games scheduled / completed) and by his share of his team's games, so a
player lost rating for every game he missed; the v4.24 measurement found that availability term predicted nothing about how he plays.

**Week by week** (`17b`). The live chain is run again as the season stood after each completed week (a week is complete when 90% of its
FBS-involved games are scored) in a private copy of the data in which nothing later can be seen; each team and player gets that
week's running number and the one game of that week graded on the same scale. A snapshot is a regular-season object: §8 records the
defect found in v4.25.1, when the postseason's scores were still visible to every week of a finished season, and its fix.

## 4. Team ratings

**The headline** = 1/6 roster + 1/6 team stats + 2/3 results (the owner's setting; each reading carries at least a sixth, results most).

- *Roster*: a slot score. Each position group's score is 0.85 × the mean of its starters + 0.15 × the mean of its depth; groups are
  weighted QB .139, RB .091, WR .087, TE .029, OL (the line unit as one number) .101, DL+EDGE .131, LB .144, DB .252, K .012, P .015, fitted
  half by non-negative least squares and half per slot. A missing player is skipped and the weights renormalise.
- *Stats*: yards, third down, havoc and a turnover-margin adjustment, from the team's per-game box scores, adjusted for the schedule it
  faced (1.57 rating points per point-per-game of schedule, found when the stats half turned out to be schedule-blind: a Power-4 team faced
  defenses 5.6 points a game tougher than a Group-of-6 team's).
- *Results*: the points a team scored and prevented against the opponents it played, per game above an average FBS team, solved with the same
  solver as the player adjustment and put through absolute anchors.

**Earned** is the order the site ranks teams by (v4.27): every game worth its margin up to 7 plus 12 for a win or minus 12 for a loss, solved over all games
against the opponents played, no rating part, in every season. **Power** says how good a team is: a *rating part* (0.98 × the team rating's distance from the mean) plus a *results part* (the ridge-shrunk
average of margin, capped at ±28 with ±8 for a win or loss, over what the rating part expected, home edge included). While a season is
being played the rating part is 0, so power is the games alone (v4.22). The conference level is measured and switched off.

**How the rating and the ranking differ, and why.** The headline rating says how good a team's players and stats are and how it has scored and
allowed. The ranking (power) says what the team has *done*: it credits a win, and it credits a margin up to 28, so a 40-point win and a 7-point win
differ, but not without limit. A team can rate above where it ranks (its players and stats look better than its scoreboard) or the reverse. The
Compare screen shows five readings side by side and marks any that sit 20 or more places from power.

**What each reading adds.** Against the next season's points strength, roster 0.623, stats 0.686, results 0.731; the shipped blend 0.724. On a season
in progress, roster and stats add 0.000 to prediction beyond the points so far plus last season: they are explanation in the headline, not prediction.

## 5. Projections (Engine D)

A career becomes a curve of percentiles; the curve becomes about thirty shape numbers; a cohort baseline says what similar players did next; a model
predicts; its spread is stretched halfway back toward reality. Offensive skill players also get an opportunity model (depth-chart rank on next
season's roster, production departing ahead of him). Linemen are excluded. Two hard gates are on the file itself (`trajectory.json`, `_meta`):
each position family beats its own naive carry-forward, and the 80% intervals cover close to 80%. Current: offense 8.11 against 9.02, interval
coverage 78.6% (n = 2,124); defense 8.49 against 9.63, 77.0% (n = 4,259); held out on 2023 and 2024. Defensive projections are marked low confidence
and say why. Players who leave for the NFL are absent from the history the curves are built on, so decline at the very top is overstated.

## 6. The app

One page, `menu.html`, with seven sections (Home, My Team, Season, Teams, Players, Recruiting, About) and a second bar for the pages of the section you
are in. A team has eight pages (Overview, Roster, Depth Chart, Ratings, Schedule, Recruiting, Transfers, History); a player has a card (Overview, Games,
Stats, Career) with the projection's reasoning and similar players; Season holds Rankings (by method and by week), Standings, The Season, Playoff and
Compare. A 2026 player carries three ratings that are never merged: last season, the preseason projection, earned to date. The classic site (a sidebar and a
page per topic) was retired in v4.25; every old address is a stub that forwards. The frontend rules are enforced by tooling: no colour literal outside a token
block, nothing below 12px, `@media` blocks at the end of a stylesheet, badge text from the luminance guard.

## 7. Validation: what has been measured, and against what

Every check below can be re-run (§12 lists the commands). None of them uses an outside rating as a target. An outside source shows up only as a
baseline (the closing spread, the committee's seed) or as an outcome a rating should anticipate (the NFL draft).

**Distributions (2025, `validate_ratings.py`).** 11,321 player-seasons rated. Counts at 85 or more and at 90 or more, by position:

| Position | 85+ | 90+ |
|---|---|---|
| QB | 40 | 6 |
| RB | 24 | 7 |
| WR | 80 | 11 |
| TE | 17 | 3 |
| EDGE | 33 | 15 |
| DL | 42 | 11 |
| LB | 58 | 15 |
| CB | 19 | 6 |
| S | 30 | 8 |
| DB | 67 | 22 |

The anchors are absolute, so these counts move from season to season. A season with fewer great players has fewer 90s.

**Reliability (2016 to 2025, `validate_reliability.py`).** Each player's season is built twice, from his odd weeks and from his even weeks, and the
two halves are correlated. That gives the most a season-to-season correlation could be. Persistence is the year-over-year correlation actually
achieved, and "share" is persistence as a share of that ceiling, for the shipped rating:

| Position | Player-seasons | Reliability of the shipped rating | Persistence | Share |
|---|---|---|---|---|
| QB | 1,838 | 0.917 | 0.556 | 0.607 |
| RB | 4,385 | 0.930 | 0.591 | 0.635 |
| WR | 6,310 | 0.908 | 0.555 | 0.611 |
| TE | 1,700 | 0.827 | 0.550 | 0.665 |
| EDGE | 2,127 | 0.704 | 0.537 | 0.763 |
| DL | 6,650 | 0.693 | 0.487 | 0.703 |
| LB | 6,904 | 0.848 | 0.560 | 0.660 |
| CB | 2,264 | 0.697 | 0.496 | 0.712 |
| S | 2,838 | 0.808 | 0.482 | 0.597 |
| DB | 5,093 | 0.722 | 0.525 | 0.728 |

Defense is nearer its ceiling than offense, and its ceiling is lower: the measurement allows less. That is why the backlog order puts offensive
signal ahead of a new defensive rating (`ARCHITECTURE_REVIEW_2026-08.md` §6).

**The draft (`validate_vs_draft.py`).** The share of players drafted falls in every rating band from the highest down: 64.1%, 30.0%, 12.6%, 4.8%, 3.3%,
1.8%. The line-unit rating, which replaced the withdrawn individual lineman rating, correlates +0.174 (Spearman) with how its linemen are drafted.

**Projections.** The two hard gates are on the file itself (§5). Offense: MAE 8.11 against 9.02 for carrying last season forward, with the 80% intervals
covering 78.6% (n 2,124). Defense: 8.49 against 9.63, covering 77.0% (n 4,259). Held out on 2023 and 2024.

**Team readings.** Each reading's correlation with the next season's points strength: roster 0.623, stats 0.686, results 0.731, and the shipped
headline 0.724. Within a season in progress, roster and stats add 0.000 to the prediction once the points so far and last season are known.

**A week-N snapshot sees nothing later (`validate_snapshot_invariance.py`).** The validator makes three checks:
- (a) every row of a week-N snapshot was cut at week N, with its team's game count;
- (b) scrambling every game after week N moves no earned rating;
- (c) scrambling a finished season's postseason moves no team reading, added in v4.25.1, because (b) could not see the postseason (§8).

**The seam between the repositories (`tests/test_export_contract.py`).** The contract suite has caught a NaN token, a season split-brain and 71 MB of
orphaned exports. One of its tests fails by design (§10.1, item 3).

**What is not validated.** There is no individual offensive-line rating to check. The pre-2016 defensive rating has no outside check. The live
ratings of a season in progress are checked against how players play the rest of the season, not against the season-end rating (§10.1, item 2).

## 8. The pre-playoff markers, and the postseason question

**The question.** 2024 and 2025 are the two seasons that look most like football now: the 12-team playoff, the portal and NIL. What did each reading
say the day the field was picked, and how did the playoff go? `scripts/validate_playoff_markers.py` reads each season's week-15 snapshot from the
weekly replay. Week 15 is the conference championships, and the committee picks the next day; Army–Navy (week 16) comes after the pick and is
excluded. The snapshot sees no game after week 15 and no postseason total.

The snapshot is scored three ways:
- selection: how many of the committee's 12 were in the reading's top 12;
- the playoff: did the higher-rated team win each of the 22 playoff games;
- the bowls: the same question, plus the margin error against the closing spread.

Each is set beside baselines that use no rating of ours (the record, last season's rating, the committee's seed, the closing spread) and a placebo
that deals the ratings to the wrong teams. The full tables and every game are in `PLAYOFF_MARKERS_2024_2025.md`, which the script writes.

### 8.1 What the test found first: two defects in the replays it reads

**The first results were too good to be true, and they weren't.** On the replays as they stood, the headline picked 21 of the 22 playoff games and
results 21, against 16 for the closing spread and 9 for the committee's seed. A rating built from box scores does not beat the betting market by five
games in 22.

**Defect 1: the postseason was in every week.** Postseason games are numbered weeks 1 to 5 again. The replay blanked "later" games by week number,
so every bowl and playoff score stayed in every snapshot of a finished season. Script 10 solves the results reading, and the schedule map the stats
half is adjusted by, from every scored game it is given.

The proof is a placebo: `validate_snapshot_invariance.py` check (c), new in this release, scrambles the 2025 postseason scores and asks whether any
week's team reading moves.

| 2025, before the fix | Headline | Power | Stats | Results |
|---|---|---|---|---|
| Week 5: teams moved, of 136 (largest move) | 136 (6.10) | 132 (0.52) | 136 (2.77) | 135 (9.16) |
| Week 15: teams moved, of 136 (largest move) | 135 (4.22) | 131 (0.28) | 133 (1.77) | 136 (6.31) |

For example, Miami went from 90.72 to 84.75 and Mississippi State from 80.87 to 86.97 on noise alone. The old replays also showed a bowl as a team's
week-1 game: 46 team-weeks in 2024 and 82 in 2025 (now 0).

The fix, in `scripts/17b_weekly_progression.py`:
- every game whose type is not "regular" is blanked, as well as every game after week N;
- a finished season (one with a scored postseason game) never reads its season totals, which include the postseason;
- the game shown for a week is a regular-season game.

After the fix, check (c) moves 0 of 136 teams at 2025 weeks 5 and 15, and 0 of 134 at 2024 weeks 5 and 15.

**Defect 2: every 2025 snapshot looked like a played season.** Four 2025 games between two lower-division teams are stored as regular-season week 1
and dated 2025-12-13. A week-N snapshot never blanks a week-1 score, so the season's last scored game always looked like its last game, and
`season_in_progress` said 2025 was over at every week. Two things followed:
- power carried its rating part (0.98 × the play rating's distance from the mean) instead of ranking on games alone, as v4.22 decided;
- the part-season shrink of the stats reading was off.

2024 had neither. Holding the record fixed, power correlated with the headline at 0.85 to 0.89 in 2025 weeks 1 to 5, against 0.36 to 0.63 in 2024.

The fix counts only games with an FBS team, with a minimum of 5 still to come (a postponed finale or two is fewer; a conference-championship week holds
10 to 12):
- no finished season's verdict changes, from 2008 to 2025;
- 2026 is in progress at every week either way;
- 2024's replay keeps every verdict it had (rebuilt, it is identical field for field: 0 of 35,971 differ);
- 2025 weeks 1 to 14 now read as in progress, like 2024's;
- week 15 reads as played in both seasons under either rule.

**What moved when both seasons were rebuilt** (offline, 0 requests; the old files are kept in `_snapshots/v4.25-pre-replay-fix/`):

Each cell gives the teams that moved, the mean move and the largest move (in brackets), from the old replay to the rebuilt one, with both fixes applied.

| Season and reading | Week 1 | Week 5 | Week 10 | Week 15 |
|---|---|---|---|---|
| 2024 headline | 131 of 134, 1.63 (8.60) | 127, 0.74 (4.30) | 128, 0.70 (3.70) | 126, 0.62 (3.40) |
| 2024 results | 133 of 133, 2.42 (12.90) | 127, 1.04 (5.00) | 130, 0.93 (4.50) | 131, 0.79 (3.90) |
| 2024 stats | 131, 1.27 (3.60) | 126, 0.85 (5.90) | 127, 0.72 (4.60) | 128, 0.69 (4.40) |
| 2024 power | 0 | 0 | 0 | 102, 0.13 (0.53) |
| 2025 headline | 132 of 136, 2.31 (13.20) | 132, 1.13 (5.70) | 126, 0.64 (3.40) | 126, 0.45 (2.30) |
| 2025 results | 135, 2.52 (15.90) | 131, 1.20 (5.00) | 126, 0.79 (3.30) | 129, 0.59 (2.50) |
| 2025 stats | 136, 8.98 (26.40) | 135, 4.51 (16.20) | 134, 1.77 (7.50) | 131, 0.52 (3.90) |
| 2025 power | 135, 4.33 (13.70) | 135, 2.49 (7.51) | 136, 1.97 (4.58) | 98, 0.09 (0.33) |

**How the 2025 moves split between the two fixes.** The second fix alone moved 2025 power by a mean of 3.77 points at week 1 (largest 11.37) and 1.72 at week 14, as
its rating part came off. It moved the stats half by 6.68 at week 1 (largest 20.10) and 0.30 at week 13, as the part-season shrink came back. It moved nothing
at week 15, and nothing at all in 2024.

**The roster reading** moved only at week 15 (123 and 120 teams, mean 0.35), through the player ratings.

**Player ratings** did not move at weeks 1 to 14 in either season. At week 15, 3,795 of 7,655 (2024) and 3,773 of 7,917 (2025) moved, by a mean of 0.23 and 0.22
points and a mean signed −0.01. The old week-15 snapshot had read the season totals, which include the postseason; the rebuilt one reads the per-game rows through
week 15. The largest moves were 40.6 points and 8.0. The 40.6 is Joshua Eaton (2024 DB). His season totals hold 24 tackles, but the per-game file holds one game of 3, so
read from the totals he rated 76.3 on production, and read from his one game he falls to the low-game rule at 35.7. That is a gap in the per-game file, not his play
(§10.1, item 10).

### 8.2 The markers, on the clean replays

Every figure below is from `PLAYOFF_MARKERS_2024_2025.md`, which also lists every game and every miss. "Leaky" is the same test on the replays before the fix.

**Selection.** The committee's 12 found in each reading's top 12, given as 2024 / 2025. "Under the format" applies the committee's own rule with the reading as the
ranking: its five best conference champions get in, then its seven best other teams.

| Reading | Top 12 | Under the format | Champion's rank | Order with the committee (Spearman) | Leaky, top 12 |
|---|---|---|---|---|---|
| headline | 8 / 8 | 10 / 8 | 5 / 1 | +0.59 / +0.73 | 8 / 8 |
| power | 8 / 8 | 9 / 9 | 3 / 2 | +0.71 / +0.84 | 8 / 8 |
| results | 7 / 8 | 10 / 8 | 6 / 1 | +0.58 / +0.73 | 8 / 8 |
| stats | 7 / 8 | 10 / 8 | 5 / 1 | +0.57 / +0.65 | 8 / 8 |
| roster | 4 / 8 | 7 / 8 | 21 / 3 | +0.75 / +0.37 | 4 / 9 |
| record (baseline) | 11 / 11 | — | 11 / 1 | — | 11 / 11 |
| last season (baseline) | 6 / 7 | 8 / 7 | 5 / 6 | — | 6 / 7 |

A random 12 holds 1.1 of the committee's 12. Ties share the better rank, so the record's top 12 holds 15 and 16 teams and the stats top 12 in 2025 holds 13, which
flatters both. The record is left out of the format test for the same reason.

**The 22 playoff games: did the higher-rated team win?**

| Reading | Correct (95% interval) | P against ratings dealt to the wrong teams | Leaky |
|---|---|---|---|
| headline | 17 of 22 (57–90%) | 0.013 | 21 of 22 |
| results | 16 of 22 (52–87%) | 0.033 | 21 of 22 |
| power | 15 of 22 (47–84%) | 0.085 | 15 of 22 |
| stats | 15 of 22 (47–84%) | 0.069 | 18 of 22 |
| roster | 13 of 22 (39–77%) | 0.236 | 12 of 21 |
| last season (baseline) | 17 of 22 (57–90%) | | 17 of 22 |
| closing spread (baseline) | 16 of 22 (52–87%) | | 16 of 22 |
| committee seed (baseline) | 9 of 22 (23–61%) | | 9 of 22 |
| record (baseline) | 7 of 18 (20–61%) | | 7 of 18 |

**The bowls** (68 to 70 games, a weaker test: opt-outs and the portal change rosters after the snapshot):

| Reading | Correct | Leaky |
|---|---|---|
| stats | 43 of 70 | 45 of 69 |
| headline | 39 of 70 | 45 of 70 |
| power | 38 of 70 | 38 of 70 |
| results | 38 of 69 | 46 of 69 |
| roster | 28 of 68 | 33 of 69 |
| closing spread (baseline) | 43 of 70 | 43 of 70 |
| last season (baseline) | 33 of 68 | |
| record (baseline) | 30 of 55 | |

**Margins.** On neutral sites, our power difference misses the final margin by an RMSE of 15.7 points in the playoff (n 14) and 14.2 in the bowls (n 68).
The closing spread misses by 14.6 and 12.7. Ours is the closer of the two in 9 of the 14 playoff games and 25 of the 68 bowls.

### 8.3 What the markers say

1. **The leak was real, and it was confined to the readings that read the postseason.** On the 22 playoff games:
   - the headline fell from 21 to 17;
   - results fell from 21 to 16;
   - stats fell from 18 to 15;
   - power, which reads only the regular season, stayed at 15, and no baseline moved.

   The bowls went the same way (the headline fell from 45 to 39 of 70).
2. **On the clean replays, no reading of ours beats the simple baselines at picking playoff games.** The headline's 17 of 22 ties last season's rating (17) and is
   one game better than the closing spread (16). The intervals overlap everything above the committee's seed (9) and the record (7 of 18). Against ratings dealt to
   the wrong teams, the headline is unlikely to be luck (P = 0.013) and power less clearly so (0.085).
3. **The committee picks by record, and by its rule.** The record holds 11 of the 12 in both seasons, on a top 12 widened by ties. Our readings hold 7 or 8 in a
   plain top 12, and 8 to 10 under the format's rule, because the rule admits conference champions whatever anyone rates them. Five such champions were in no
   reading's top 12: Boise State, Arizona State and Clemson in 2024, and Tulane and James Madison in 2025. The at-large misses are real disagreements: in 2025
   every reading put Notre Dame in its field, and the committee did not select it.
4. **Power orders the twelve most like the committee** (Spearman +0.71 and +0.84, against +0.59 and +0.73 for the headline). Neither champion was a surprise to the
   readings: Ohio State was 5th on the 2024 headline and 3rd on power, and Indiana was 1st on the 2025 headline, results and stats.
5. **The market is the better margin forecaster**, as it should be: 14.6 and 12.7 points of RMSE against our 15.7 and 14.2.
6. **What it can and cannot say.** Two seasons cannot tell the readings apart: headline, results, power and stats differ by one or two games of 22. The marker shows
   that the readings are not broken, since each beats the placebo and the seed. It does not show that one reading is best. Whether it becomes a gate is the
   owner's question 3.

### 8.4 Should the postseason count in a rating at all? Evidence only; nothing changed

Today a finished season counts its bowls and playoff games in three places:
- the opponent solve behind both the player adjustment and the results reading, at weight 1.0 (`POST_WEIGHT`);
- a player's games;
- the season stat totals behind the stats half.

Power uses the regular season only, and a week-N snapshot sees no postseason at all (v4.25.1). Three measurements bear on the question.

1. **Does counting it predict the next season better?** Each season's strengths are solved from its regular season alone, or with the postseason at weight 0.5, 1 or 2,
   and used to predict every regular-season game of the following season. That covers seven season pairs, 2016→2017 to 2024→2025, with 2020 left out
   (`validate_opponent_strength.py` part B, re-run for this spec).

   | Postseason weight | 0 | 0.5 | 1 (today) | 2 |
   |---|---|---|---|---|
   | Next-season winners picked | 0.669 | 0.670 | 0.671 | 0.674 |
   | Margin MAE | 14.17 | 14.14 | 14.13 | 14.14 |

   The gain is small and steady: half a point of accuracy from none to double weight. v4.17 measured the same shape on its data (66.3%, 66.3%, 66.5%, 66.6%).
2. **How much does it move a finished season?** 2024 and 2025 were solved with and without their postseason, with the same solver and settings:

   | | 2024 | 2025 |
   |---|---|---|
   | Mean change in points strength (points a game) | 0.98 | 0.76 |
   | Teams moving five or more places by strength | 37 | 22 |
   | Mean change for the twelve selected teams | 1.74 | 1.26 |
   | Mean change for every other team | 0.91 | 0.72 |

   The champions show the effect. **Ohio State was 6th on its 2024 regular season and 1st once its four playoff wins count** (+5.16 points a game). Indiana was 1st either way in 2025 (+2.89).
3. **Counting it makes a finished season untestable on its own playoff.** A reading that already holds the playoff games cannot be scored on them.
   §8.2 shows how far that inflated this test before the replays were fixed.

**What this does not settle.** Bowls are played after opt-outs and portal departures, so a bowl result says less about a team's season than a regular-season
game does. The measurement above does not separate bowls from playoff games, and the roster and stats readings (a third of the headline) were not re-measured
without the postseason. The choice is the owner's, between three options:
- keep it, as today;
- count the regular season only;
- count the playoff but not the bowls.

## 9. Change index, v4.16 to v4.25.1

Each line is one Build Log entry (`menu.html#/changes`, About › Build Log). The full record lives there: what was done, what was tried and
rejected, what was measured, which gates ran and what was not fixed. This index gives the order of events; it does not replace that record.

| Version | Date | What changed |
|---|---|---|
| v4.16 | 2026-10-29 | Team rating built from every position group with depth (the slot score); a calibrated in-season pace correction; the menu for mouse and controller |
| v4.17 | 2026-10-29 | Our own opponent strength replaces SP+ in the player ratings; a shutout's 0 stops being read as a missing score |
| v4.18 | 2026-10-29 | The stats half is adjusted for the schedule faced; part-season readings shrink toward the league mean |
| v4.19 | 2026-10-29 | Players with one or two games get a small, capped, blended rating; a written account of why Kansas State and Nebraska rate high |
| v4.20 | 2026-10-29 | Live player ratings get the opponent adjustment back; FCS games count at 0.85; quarterbacks scored on efficiency as well as volume; roster scored by starters |
| v4.21 | 2026-10-29 | The headline becomes 1/6 roster, 1/6 stats, 2/3 results; quarterbacks tilt slightly toward passing; Rankings gains All FBS and Compare |
| v4.22 | 2026-10-30 | Live power ranked on games alone; the week-by-week progression with each game graded |
| v4.23 | 2026-10-30 | A Games tab on the player card; a team's units by week; Rankings by method and by week |
| v4.24 | 2026-11-02 | Week 9 added with 12 requests; an audit of both repositories; weekly snapshots stopped reading full-season totals; a part-season rating becomes the level a player has played at, believed by his own games |
| v4.25 | 2026-11-02 | The classic site retired; what it had that the menu lacked came across; every old address forwards |
| v4.27 | 2026-11-09 | The earned order beside power, the default wherever a rank is shown; two-game players of a season in progress rated on their own play |
| v4.25.1 | 2026-11-06 | This spec; the Build Log audit (`tools/registry-check.mjs`); the pre-playoff marker test; two replay defects fixed (the postseason in every week, and a 2025 snapshot that looked finished) and the 2024 and 2025 replays rebuilt |

## 10. Outstanding issues, and the player cases

### 10.1 What is still wrong or unproven, most important first

1. **The marker test covers two seasons.** That is 24 selected teams and 22 playoff games. Every figure in §8 carries its interval, and the intervals
   are wide: a reading that wins by two games has not been shown to be better. The test only gets stronger as seasons are added, one a year.
2. **In a season in progress, the top of the defensive line and edge reads low.** The owner raised this; it was reviewed in v4.25 and left unchanged.
   The anchors are not the cause: a full 2025 season puts 15 EDGE and 11 DL at 90 or more. The cause is the part-season curve. At 6 games the DL/EDGE line
   turns a level of about 99 into 85.5 (Colin Simmons). That is because DL/EDGE production is the noisiest per game of any family: split-half
   reliability of the shipped rating is 0.693 to 0.704, against 0.908 to 0.930 for QB, RB and WR. If this is fixed, the fix is a better-measured curve
   for that family, not new anchors.
3. **One contract test fails by design.** Recruiting-basis rows are 4.7% of a season's ratings, against a 5% floor in
   `tests/test_export_contract.py::test_recruiting_basis_rows_exist_and_are_not_rare`. It is left failing so the drop stays visible. The floor is a gate, and
   a gate is not moved in the release that ships the output it judges.
4. **Two settings of the opponent solve have a measured better value that is not adopted.** These are the cross-conference weight and the conference level.
   Part A of `validate_opponent_strength.py` finds 0.709 at team ridge 3.0 with no conference level and cross weight 1.0, against about 0.703 shipped.
   The gain is inside a season's noise, and the change would move every player rating. It waits for a release of its own, with the full recompute and the projection gates.
5. **The Methods entry `projection` quotes stale accuracy.** It says MAE 8.22 against 9.10 and coverage 80.5%. The file now reports 8.11 against 9.02 and 78.6%
   for offense (§5). The entry is prose, so it drifted from the `_meta` in `trajectory.json`; it should read the file, as the guide does.
6. **Missing values still become defaults in places**: a 40.0 composite, stars to 0, PPA to 0, a missing team stat to 0.0 (v4.24 audit item S5). None is fixed;
   each needs its own before and after.
7. **The pre-2016 defensive rating is largely inoperative.** A pre-2016 defender is tiered on a tackle count that does not exist for him, and there is no
   external check for that era.
8. **Four 2025 lower-division games are stored as regular-season week 1, dated in December.** v4.25.1 stopped them from fooling the
   season-in-progress test, but the rows themselves are still mislabelled in `games.json`.
9. **Week 16 is not in the replays.** That is Army–Navy, plus Montana State–Idaho in 2024. The team box-score fetch stops at regular week 15.
10. **Some players' season totals hold games their per-game rows lack.** Of defenders with 10 or more season tackles, the per-game rows hold under half of them for
   26 in 2024 (1.0%, 12 with no game row at all) and 5 in 2025 (0.2%). A week-N snapshot reads only the per-game rows, so these players rate lower there
   (Joshua Eaton, §8.1). Not traced to a cause and not fixed.
11. **Still open from the backlog** (root `CLAUDE.md` §6):
    - the empty `research_cache`;
    - the slim/detail export split;
    - the class year;
    - the 110 MB `team_advanced_games.json`;
    - the two histories (the markdown changelogs and the Build Log);
    - the playoff model, of which this spec's §8 is the first step, not the model.

### 10.2 Player cases, before and after

The owner asked for player cases shown as before and after. These are the players raised in sessions. The figures are 2026 earned ratings to date,
through week 9, with the rule that moved each one. A case checks that a rule behaves as described; no weight was tuned to any of them (root `CLAUDE.md` §3).

| Player | Position, team | Before | After | What moved it |
|---|---|---|---|---|
| Jamarrion Harkless | DL, Kentucky | 69.7 | 77.2 | v4.24, second step: in a season in progress a player with three or more games skips the full-season volume gate and the playing-time tier, which had rated him for how little he played |
| Alex Godavitarne | TE, UConn | 35.7 at two games | 71.1 at his third game; 76.6 now | a change of basis, not one game's play: at three games he left the low-game rule (built on an unrecruited player's prior of 40, listed as unfixed in v4.24) for a production rating |
| Sire Gaines | RB, Boise State | 80.1 | 80.6 | the same second step; his game grades average 82.4, and the curve keeps a five-game sample a little below that |
| Dante Moore | QB, Oregon | 72.9 | 82.4 | v4.24, first step: under the v4.20 rule he fell from 85.6 to 72.9 over four games he did not play; the availability term is gone |
| Colin Simmons | EDGE, Texas | — | 85.5 | no rule moved him: six games at a level of about 99 is the top of the DL/EDGE curve at six games (§10.1 item 2) |
| Kenny Minchey | QB, Kentucky | — | 85.3 | no rule moved him; a reference point at the top of the position |
| Canaan Moore | K, Boise State | — | 78.5 | no rule moved him; kickers are rated from their composite through their own anchors |

Headline rank and power rank for 2026 through week 9:

| Team | Headline rank | Power rank |
|---|---|---|
| Boise State | 45 | 12 |
| Nebraska | 23 | 29 |
| Kentucky | 22 | 19 |
| Kansas State | 38 | 77 |
| LSU | 1 | 2 |
| Liberty | 58 | 13 |

The gaps are the difference §4 describes. Kansas State's players and stats rate well and its scoreboard does not. Liberty's scoreboard is better than its roster.

## 11. Decisions log

What has been decided, by whom, and where the record is. A decision here is reversed by a written case, not by a refactor.

| Decision | By | Record |
|---|---|---|
| Anchors are absolute; a position mapped by season percentile is a bug | design, settled | `AUDIT_FINDINGS.md` §9; root `CLAUDE.md` §3 |
| Era buckets, not pooled bounds, for the line rating | design, settled | `test_line_rating_does_not_drift_across_eras` |
| Never tune weights to fix one player | owner | root `CLAUDE.md` §3 |
| Missing means unknown; `avg_top()` returns None, never 50 | design, settled | `PIPELINE_INVARIANTS.md` |
| Offensive linemen carry no individual rating | measurement (0.877 with recruiting) | v4.3; Methods `ol` |
| An outside rating is a reference, never a target; EA removed | owner | v4.7; Methods `ea-removed` |
| NIL never enters an earned rating | owner | root `CLAUDE.md` §3 |
| FCS opponents count at a flat 0.85 | owner (the data say 0.88 raw, 0.95 FBS-adjusted) | v4.20 |
| The quarterback composite's weights and the efficiency tilt | owner (weights); held-out data (the 0.5 blend) | v4.20, v4.21 |
| The headline is 1/6 roster, 1/6 stats, 2/3 results | owner | v4.21 |
| No preseason or last-season input in a live rating; live power is games alone | owner | v4.22 |
| A part-season rating is scaled by his own games, not his team's, and is not shrunk to a prior | owner (scale, not shrink); measurement (the curve) | v4.24 |
| The DL/EDGE in-season ceiling reviewed and left unchanged | owner ("review, but don't change") | v4.25 |
| The classic site retired | owner | v4.25 |
| The postseason is not part of a week-N snapshot | design, measured (§8) | v4.25.1 |
| Whether the marker test becomes a gate | **open: the owner's question 3 below** | |

## 12. Constants, and the commands that check this document

This table is generated by `python scripts/spec_constants.py --update-spec`, and `tests/test_ratings_spec.py` fails when it differs from the code.

"Set by" takes one of three values:
- `owner`: the owner's judgement, labelled as such wherever it is shown;
- `data`: fitted or chosen by a measurement a validator reproduces;
- `design`: a structural choice.

"Frozen" means the constant is on root `CLAUDE.md` §3's list.

<!-- constants:start -->
| Constant | Value | Where | Set by | Frozen |
|---|---|---|---|---|
| OPP_FCS_MULT | 0.85 | 06 | owner |  |
| OPP_K_HI / OPP_K_LO | 0.35 / 0.12 | 06 | data |  |
| LAM_TEAM / LAM_CONF | 3.0 / 0.5 | utils/opponent_strength | data |  |
| CROSS_WEIGHT | 1.5 | utils/opponent_strength | data |  |
| POST_WEIGHT | 1 | utils/opponent_strength | data |  |
| QB composite (pass yd, rush yd, pass TD, rush TD, INT) | [1.05, 0.6, 25, 18, -20] | 06 | owner |  |
| QB_EFFICIENCY_BLEND / QB_ATT_REF / QB_ATT_FLOOR | 0.5 / 28.0 / 10.0 | 06 | data |  |
| SOLO_MULT / ASSIST_MULT | 1.25 / 0.65 | 06 | data |  |
| COVERAGE_CREDIT | {CB: 4.2, DB: 3.7, S: 3} | 06 | data |  |
| ARCHETYPE_SCALE | {ball_hawk: 13.4, coverage: 8.5, run_support: 14.8} | 06 | data |  |
| SECONDARY_ARCHETYPE_WEIGHTS | {CB: {coverage: 0.4, ball_hawk: 0.4, run_support: 0.2}, S: {run_support: 0.5, ball_hawk: 0.3, coverage: 0.2}, DB: {run_support: 0.3333, ball_hawk: 0.3333, coverage: 0.3333}} | 06 | design |  |
| OPPORTUNITY_CLIP / OPPORTUNITY_MIN_PLAYS | [0.85, 1.2] / 300 | 06 | data |  |
| EDGE_OVR_ANCHORS[QB] (the score that earns each rating from 85 up) | {85: 1100, 90: 1450, 96: 1676.9, 99: 2385} | 07 | data | frozen |
| EDGE_OVR_ANCHORS[RB] (the score that earns each rating from 85 up) | {87: 500, 93: 650, 96: 712.6, 99: 1029} | 07 | data | frozen |
| EDGE_OVR_ANCHORS[WR] (the score that earns each rating from 85 up) | {85: 319.1, 90: 459.9, 96: 611.4, 99: 950} | 07 | data | frozen |
| EDGE_OVR_ANCHORS[TE] (the score that earns each rating from 85 up) | {87: 270, 97: 384, 99: 650} | 07 | data | frozen |
| EDGE_OVR_ANCHORS[EDGE] (the score that earns each rating from 85 up) | {85: 30.3, 90: 40.8, 96: 65.7, 99: 82.8} | 07 | data | frozen |
| EDGE_OVR_ANCHORS[DL] (the score that earns each rating from 85 up) | {85: 30.5, 90: 43, 96: 59.9, 99: 71} | 07 | data | frozen |
| EDGE_OVR_ANCHORS[LB] (the score that earns each rating from 85 up) | {85: 39, 90: 52.1, 96: 70.9, 99: 96.7} | 07 | data | frozen |
| EDGE_OVR_ANCHORS[CB] (the score that earns each rating from 85 up) | {85: 11.6, 90: 16.5, 96: 19.1, 99: 24.2} | 07 | data | frozen |
| EDGE_OVR_ANCHORS[S] (the score that earns each rating from 85 up) | {85: 10.9, 90: 14.4, 96: 18.9, 99: 25.8} | 07 | data | frozen |
| EDGE_OVR_ANCHORS[DB] (the score that earns each rating from 85 up) | {85: 11.2, 90: 14.5, 96: 20.8, 99: 25.5} | 07 | data | frozen |
| PLAYTIME_TIERS (QB starter / role / reserve) | [100, 30, 5] | 07 | design | frozen |
| COMPOSITE_OVR_ANCHORS (OL, K, P: composite 0-1 to a rating, first and last anchor) | {K: 30 at 0 to 90 at 1, OL: 30 at 0 to 88 at 0.65, P: 30 at 0 to 90 at 1} | 07 | data | frozen |
| STARS_OVR_DELTA | {5: -3, 4: -8, 3: -15, 2: -22, 1: -28, 0: -33} | 07 | design |  |
| LOW_GAME_WEIGHT (games, busy) | {(1, False): 0.115, (1, True): 0.175, (2, False): 0.173, (2, True): 0.209} | 07 | data |  |
| LOW_GAME_CEILING | 72 | 07 | design |  |
| PART_SEASON_CURVE[DL/EDGE, 8-9] (a, b) | [31.93, 0.5696] | 07 | data |  |
| PART_SEASON_CURVE[QB, 8-9] (a, b) | [14.83, 0.8129] | 07 | data |  |
| PART_SEASON_CURVE[RB, 8-9] (a, b) | [6.837, 0.9135] | 07 | data |  |
| PACE_SHARE_EDGES / PACE_SHARE_K (pre-v4.24 pace scale, still the paced EDGE of 06) | [0.5, 0.75, 0.9] / [0.8927, 0.9541, 0.99, 1.0057] | 06 | data |  |
| _CLASSIC_EDGE_CAP / _CLASSIC_DL_CAP (pre-2016 EDGE and DL) | 88 / 85 | 07 | design |  |
| REGULAR_SEASON_GAMES | 12 | utils/seasons | design |  |
| W_ROSTER_BLEND / W_STATS_BLEND / W_RESULTS_BLEND (the headline) | 0.1667 / 0.1667 / 0.6667 | 10 | owner |  |
| W_ROSTER_PLAY / W_STATS_PLAY (the play rating) | 0.6 / 0.4 | 10 | design |  |
| SLOT_STARTERS | {QB: 1, RB: 2, WR: 5, TE: 1, DL: 4, LB: 4, DB: 6} | 10 | design |  |
| SLOT_WEIGHTS | {QB: 0.139, RB: 0.091, WR: 0.087, TE: 0.029, OL: 0.101, DL: 0.131, LB: 0.144, DB: 0.252, K: 0.012, P: 0.015} | 10 | data |  |
| STARTER_SHARE | 0.85 | 10 | data |  |
| SCHED_COEF | 1.57 | 10 | data |  |
| SHRINK_GAMES_K | 8 | 10 | data |  |
| IN_PROGRESS_MIN_FBS / IN_PROGRESS_MIN_ALL (games still to come for a season in progress) | 5 / 20 | 10 | design |  |
| CAP / BONUS / LAM / BETA (the results reading and power) | 28.0 / 8.0 / 2.0 / 0.98 | utils/results_rating | data |  |
| EARNED_CAP / EARNED_BONUS (the earned order) | 7.0 / 12.0 | utils/results_rating | design |  |
<!-- constants:end -->

The validators behind the numbers in this document:

```bash
# pipeline (cfb-analytics-pipeline/)
pytest tests/ -q
python scripts/validate_ratings.py --season 2025          # distributions, 85+/90+ counts per position
python scripts/validate_vs_draft.py                       # drafted share by rating band
python scripts/validate_reliability.py                    # split-half reliability, share of ceiling
python scripts/validate_opponent_strength.py              # the opponent solve and the postseason question
python scripts/validate_snapshot_invariance.py --season 2025 --postseason-weeks 5 15   # a week-N snapshot sees nothing later
python scripts/validate_playoff_markers.py                # §8
python scripts/spec_constants.py --update-spec            # §12
# app (cfb-analytics-app/)
node --check js/*.js
node tools/registry-check.mjs                             # the Build Log audit
node tools/contrast-check.mjs
```

## Questions for the owner

1. **Which player ratings look wrong to you?** Give names and what you expected. Each becomes a case in §10.2 with its decomposition; a rule changes only if the
   case shows a pattern across its position group.
2. **Which ranking should the Rankings screen show first?** Answered in v4.27: the earned order, which the owner approved. Power, the blend and the headline rating
   are one chip away. §8 says how each did against the committee and the playoff games.
3. **Should the pre-playoff marker test become a gate, and under what rule?** Here is a proposal for you to accept or change: on the week-15 snapshot, a release
   that changes a team reading must not lower that reading's playoff-game accuracy below the record baseline, nor its selection count below the
   previous release's, and any drop is published with the release. The gate would be added in a release of its own, never in the one it first judges.
