# Research methods — how each finding is actually computed

*Written 2026-08-12. One section per finding, same five headings, so they can be compared.
Options for changing any of them are in `ALTERNATIVES.md` §E.*

Read this next to `FORMULAS.md`, because every finding here is built on ratings computed there
and inherits their limits. A finding cannot be more trustworthy than its inputs.

---

## The problem all four share

**Every finding is presented as a ranking, and none carries uncertainty.**

That is a real defect, not a stylistic one. Script 13's residual has a standard deviation of
**9.82 SP+ points** across **2,310 team-seasons**. Rank 2,310 noisy things and the top of the
list is selected substantially *for noise* — the team at #1 is disproportionately likely to be
there because it got lucky, not because it is best. This is the multiple-comparisons trap, and
a platform whose whole pitch is "we show our work" should not walk into it.

The fix is one shared helper — empirical-Bayes shrinkage toward the group mean, plus a
displayed interval — applied to all four. It is listed as E4 and it is the change I would make
before any other on this page.

---

## 1. Who beats the roster they recruited

**Script:** `13_team_performance_evaluator.py` → `team_performance.json` (2,310 rows)

### Question

Which programs consistently do more with the talent they sign than the talent alone predicts?

### Inputs

- `sp_overall` — SP+ rating per team-season, from the API
- A three-year rolling recruiting **talent composite** per team
- `is_p5` — conference tier flag
- 32 team-seasons with missing talent are imputed with the conference/season median

### Exact formula

```text
SP+  ≈  β₁ · talent_normalised  +  β₂ · is_p5  +  c        (least squares, np.linalg.lstsq)

performance_residual   = actual SP+ − predicted SP+
performance_percentile = percentile of that residual among all team-seasons
```

Fitted across all team-seasons at once. **R² = 0.474** — recruiting talent plus conference tier
explains about 47% of team performance. Residual SD = **9.82**.

### What it can support

That a program's performance was **higher or lower than a two-variable model of its recruiting
predicts**. That is a real, measurable quantity, and the residual is **not noise**: measured
year-over-year persistence is **r = 0.607**, decaying to **0.280** at three years. Something
durable is being captured.

### Known confounds

**Persistence does not identify coaching**, which is the claim the page currently implies.
Everything stable about a program that is missing from a two-variable model lands in that
residual:

- scheme, strength and conditioning, walk-on and JUCO pipelines, portal usage;
- **systematic error in the talent proxy itself.** If the 247 composite understates what a
  particular program's roster is worth, that understatement is persistent *by construction* and
  is indistinguishable from coaching. Boise State is exactly the case where this matters.

Also: SP+ is a performance measure for the same season, so the residual absorbs luck, injuries,
schedule quirks and measurement error alongside anything real. And the model has no
returning-production term, so "beat your talent" and "beat your talent *this year, with an
experienced roster*" are not separated.

**The decisive test is a coaching-change event study** — does the residual step when the head
coach changes, and does it travel with the coach? This was blocked by a 22-row seed table.
It is **no longer blocked**: `/coaches` is confirmed to cover 2010–2024 with tenure and
per-season SP+ splits, matching our school names at 100%.

---

## 2. What recruiting classes actually became

**Script:** `14_recruiting_roi.py` → `recruiting_roi.json` (2,700 rows)

### Question

Which programs turn recruits into contributors — separating programs that *recruit* well from
programs that *develop* well?

### Inputs

- Recruiting classes by team and year
- `peak_ovr` per player — the maximum OVR he ever reached across all rated seasons

### Exact formula

```text
contributor          = peak_ovr ≥ 75
hit_rate_pct         = contributors / recruits who were ever rated
hit_rate_class_pct   = contributors / ALL recruits in the class
bc_hit_rate_pct      = same, blue-chips only, suppressed below n = 3
```

### What it can support

What share of a class became rated contributors. As a descriptive fact about outcomes, it is
sound.

### Known confounds

**It cannot answer the question on the page.** Measured: correlation between a class's average
recruiting composite and its hit rate is **+0.266** (among rated recruits) and **+0.328** (over
all recruits).

| Class strength | n | hit rate (rated) | hit rate (all) |
|---|---:|---:|---:|
| Weak third | 757 | 28.8% | 16.7% |
| Middle | 780 | 29.5% | 17.8% |
| Strong third | 757 | **39.0%** | **25.6%** |

Better classes have higher hit rates, so the metric **tracks recruiting**. It is closer to a
restatement of the star ratings than a separation from them. (I had expected the opposite
confound — that weak programs would look good because their recruits play sooner. The data says
no, and the honest thing is to report that the hypothesis was wrong.)

Three more:

- **`peak_ovr` reads the future.** For recent classes it is censored — a 2024 recruit has not
  had time to peak. The `maturing` flag exists for this; the UI must respect it.
- **`hit_rate_class_pct` mixes two stories**: "developed poorly" and "transferred out" are both
  counted as failures to become a contributor.
- It inherits every limit of OVR itself, including that OL is barely rated at all and that two
  thirds of players have no meaningful production.

**The fix** (E1) is script 13's own trick one level down: compute each *recruit's* expected peak
OVR from his own composite, then aggregate the **residual** per class. A program that turns
3-stars into 80s scores well; a program that turns 5-stars into 80s does not. Every input for
this already exists.

---

## 3. Who beats their cohort next season

**Script:** `15_predict_trajectories.py` → `trajectory.json` (5,883 predictions)

### Question

Which players will exceed what similar players historically did next?

### Inputs and formula

The projection engine — see `HOW_PROJECTIONS_WORK.md` for the full walkthrough. The finding
surfaces `vs_cohort` = projected OVR − cohort baseline.

### What it can support

`vs_cohort` is the right framing and should be kept. Comparing to what comparable players
actually did is a falsifiable claim, and it is what stops "breakout" collapsing into "was bad
last year" — the defect that made the v3.1 labels useless (raw delta correlated **−0.87** with
current rating).

### Known confounds

Everything in `HOW_PROJECTIONS_WORK.md`, and two that bite hardest here:

- **Interrupted careers can be penalised by an artifact.** Jaden Mickey's raw acceleration is
  **+120** where his healthy-path acceleration is **−13**, and the model leaned on the raw one.
  This finding is where such a call is most visible.
- **Spread is compressed** to 70–76% of reality, so extreme calls in both directions are
  under-made.

The breakout gate added in v3.3 (a call now needs top-2 depth, ≥25% of the work ahead departing,
or ≥300 yards of his own) demoted 67 calls and is working as intended — it stopped the list
filling with fourth-stringers lifted by regression to the mean.

---

## 4. The recruits nobody wanted (hidden gems)

**Computed client-side** in `js/findings.js`, not by a pipeline script.

### Question

Which lightly-recruited players became real contributors?

### Exact formula

```javascript
rows.filter(p => p.stars >= 1 && p.stars <= 2 && p.overall_rating >= 70)
```

### What it can support

Almost nothing as constructed. It is **selection on the outcome with no denominator** — we show
the 2★ players who succeeded and never show how many 2★ players there were. The reader cannot
tell whether the list is remarkable or arithmetic.

### Known confounds

- No base rate. If 5% of 2★ recruits reach 70 and 35% of 5★ recruits do, the list is expected,
  not surprising — and we do not say which.
- `stars >= 1` silently excludes unrated recruits, who are the most extreme version of the very
  story the finding is about.
- Single season only (`PLAYED_SEASON`), so a player who peaked in 2021 is invisible.

**The fix** is the same as §2: express it against expectation, and show the base rate beside the
list. The claim "this is the list the whole platform exists to produce" deserves a denominator.

---

## 5. A finding we can now build, and could not before

**Validate our ratings against NFL draft outcomes.**

`/draft/picks` is confirmed available 2010–2024, ~255 picks a year, and its `collegeAthleteId`
matches our player ids at **94.5%**. That gives us, for the first time, an **independent,
historical, backtestable** check on whether our ratings identify the players the NFL wants.

It is worth stating why this is different from the EA comparison: EA is one season and is a
scouting *opinion*. The draft is fifteen seasons of decisions made with real money. It can
answer questions EA cannot — including whether our OL ordering carries any signal at all, which
is the one position where we currently have no external check whatsoever.

It also feeds the projection: cohort curves are built only from players who *stayed*, which is
why the top of the rating scale is optimistic. With draft data, departure can be modelled
instead of silently dropped (`ALTERNATIVES.md` D5).
