# Ideas parking lot

Ideas go here instead of being built. Each one gets:
- one paragraph;
- why it is interesting;
- what must be true before it is worth building;
- who raised it.

An idea leaves this file in one of three ways:
- it is built, and its Build Log entry links back here;
- it is rejected, and moves to `ALTERNATIVES.md` with the measurement that killed it;
- it is overtaken, and says by what.

This is one file and the only list. A page on the site would be a second copy; see the last idea.

*Started 2026-11-06 (v4.26). "Raised by" is the owner or Claude (an idea Claude had and did not build).*

---

## The week-over-week change view

A panel on the team and player cards, and a Rankings column, that splits this week's move into "games played" and "model
changed". It uses the frozen snapshots and the replay; the decomposition and a mock are in `SNAPSHOTS.md`.

**Why it is interesting:** it turns "why did this number move?" from a guess into a measurement. It would also let the Build Log
say what each release did to the numbers ("v4.27 moved 131 teams, mean 0.4").

**What must be true first:** two snapshots of a season (week 10 will be the second), and a `model` snapshot frozen at every
mid-week release. It is page code, so the owner says go.

**Raised by:** the owner (the snapshot brief, 2026-11-06).

## Recover 2026 weeks 1 to 8 from the app's git history

Every published `live_2026.json` is in a commit of the app repository. Reading each version back out would give "what the site
showed" for weeks the snapshots missed, without regenerating anything. Each would be marked `recovered` rather than `frozen`, with
the commit it came from.

**Why it is interesting:** the change view could start at week 1 instead of week 10, and the season's history would be on the
page rather than in git.

**What must be true first:**
- the commits that changed `live_2026.json` can be told apart from same-day rebuilds;
- each recovered file's week is the week it claimed, which v4.24 showed was not always so ("week 9" once meant 7 of 314 games);
- the model fingerprint of each commit can be computed from that commit's code.

**Raised by:** Claude.

## Make the accessibility audit a gate

`tools/ui-smoke/a11y.mjs` measures rendered contrast, text size, target size and unnamed controls on every screen, and it is
report-only today.

**Why it is interesting:** the token check (`contrast-check.mjs`) passed while text over the team photos measured 1.7:1. Only a
rendered measurement sees what the pages compose.

**What must be true first:** the failures it reports are fixed or listed as accepted with a reason. Since v4.26.1 they are fixed:
0 text failures on any screen it covers. The gate ships in a release of its own, never with the fixes it would judge (CLAUDE.md §3).

**Raised by:** Claude (v4.26).

## An ideas page on the site, from this file

The site's About section could list these ideas, so "what is being considered" is as visible as "what was done".

**Why it is interesting:** the site is meant to explain itself, and unbuilt ideas are part of that story.

**What must be true first:** one source. Either the page is generated from this file, which needs a tiny Markdown-to-registry
step since the app has no build, or this file is generated from a registry. Two hand-kept copies are the two-histories problem
`CLAUDE.md` §6 already lists.

**Raised by:** the owner (the snapshot brief: "optionally an ideas page"). Not built, for the reason above.

## A machine-checkable schema for the export contract

`exports/v1/schema.json` describes every field for a person. A JSON Schema (draft 2020-12) generated from it would let the sibling
apps validate a file mechanically before they trust it.

**Why it is interesting:** a sibling app built months later can tell, in one call, whether the files it fetched still match v1.

**What must be true first:** a sibling app exists and says it wants it. Until then the Python test (`tests/test_export_contract_v1.py`)
is the check.

**Raised by:** Claude.

## Contract v1.1: what a sim would ask for next

The candidates are:
- per-group sub-ratings (a defensive back's three jobs, the team's position-group scores);
- the schedule and results per game;
- the line unit's five measurements;
- the earned order (`earned_rating`), which the site ranks by since v4.27.

Each is published on the site already, so each would be an additive field (v1.1), not a break.

**Why it is interesting:** a play-calling sim needs unit strengths more than one overall number.

**What must be true first:** the sim's first prototype names the fields it is missing. Fields nobody reads are the
`supplemental-data` lesson (17 datasets harvested, 3 read).

**Raised by:** Claude.

## A harvest-time check for games stored in the wrong week

Four 2025 lower-division games are stored as regular-season week 1 and dated in December, and they made every 2025 replay week
look finished (v4.25.1). A check at harvest time would flag any game whose date is far from its week's other games, before the
row is merged.

**Why it is interesting:** the defect it would have caught cost a rebuild of two seasons, and the rows are still mislabelled.

**What must be true first:** a tolerance that survives week 0, bye weeks and rescheduled games. The 2024 and 2025 files can be
measured for that today.

**Raised by:** Claude (v4.25.1).

## Find why some players' season totals hold games their per-game rows lack

Of defenders with 10 or more season tackles, the per-game rows hold under half of them for 26 in 2024 (1.0%) and 5 in 2025 (0.2%).
A week-N snapshot reads only the per-game rows, so these players rate lower there (Joshua Eaton: 24 tackles in the totals, one
game of 3 on file).

**Why it is interesting:** if the gap is a matching failure, the same players are under-rated in every weekly view.

**What must be true first:** a cause. It could be name matching, a transfer mid-season, or games missing from the API's per-game
endpoint. Each points to a different fix.

**Raised by:** Claude (v4.25.1).

## Treat a missing recruiting grade as unknown on the low-game rule

A player with no recruiting grade on file is read as zero stars, 33 points below the position average, so he starts near 40 however he played. v4.27 took two-game players of a
season in progress off that rule; one game, and two in a finished season, are still on it. Of the players on the rule who earned a rating the next season, those with no grade were rated
35.5 and earned 66.7, against 54.4 and 63.9 for recruited players (gap 21.71, shuffled 0.41). Correcting the level cuts their miss from 31.18 to 6.22, level with the dumb baseline
(`scripts/validate_low_game_weights.py`).

**Why it is interesting:** it is the standing rule "missing means unknown, never zero", broken on one path; v4.24 listed it and deferred it.

**What must be true first:** a measurement of every path that reads stars for a missing grade, and the projection gates, since it changes finished seasons. Its own release.

**Raised by:** Claude (v4.27, from Max Cutforth).

## Two games in a finished season

A two-game player of a season in progress is rated on his own play since v4.27; the same player keeps the low-game rule once the season ends, so his rating can fall when the season is
finished if he plays no more. The owner's rule was stated for the season being played. Whether it reaches finished seasons is a separate question: it needs the same test on them and the
projection gates.

**Why it is interesting:** one rule for every season, and no step at the end of the year.

**What must be true first:** the owner's say, and a full recompute.

**Raised by:** Claude (v4.27).

## A defensive back's coverage credit should not fall for a game he sat out

A defensive back's coverage credit counts in every game he played, and it scales with his rank in his team's secondary by
cumulative tackles: the top five get all of it, 6th gets 60%, 7th 30%, 8th nothing (`build_coverage_participation`, script 06).
Teammates can pass him while he sits, and the credit for games already played goes with it. Brycen Scott (Louisville) kept his 17
tackles in week 10, went from 6th to 8th, and fell 7.5 points without playing. The same week, 32 of 979 defensive backs who did not
play moved 2 or more points: a few on this rank cliff, the rest with their team's pass defense. No other position group did.

**Why it is interesting:** it is the owner's own objection ("why are we punishing him for games he did not play in"), in the one
place the v4.24 fix did not reach, and a per-game or per-appearance measure of being in the rotation might remove the cliff.

**What must be true first:** a measurement. Does a replacement hold up on the part-season test (`validate_part_time_rating.py`) and
against a shuffled placebo? `COVERAGE_CREDIT` is an input, so `ARCHETYPE_SCALE` must be re-measured with any change (CLAUDE.md §3).
It changes finished seasons too, so it is its own release with the projection gates.

**Raised by:** Claude (v4.26.2, from the week-10 moves).

---

## Left this file

- **Choose badge text by contrast, not by a luminance cut.** Built in v4.26.1 on the owner's go-ahead (Build Log
  `v4.26.1-badge-text-by-contrast`): black or white, whichever has the higher contrast with the fill, so every badge clears AA.
