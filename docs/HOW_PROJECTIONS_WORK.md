# How the projections work

*Written 2026-08-12. Nothing in this document proposes a change — it explains what the engine
does today, using one real player throughout. Options are in `ALTERNATIVES.md` §D.*

---

## First, the vocabulary

### MAE — Mean Absolute Error

Take every prediction. Subtract what actually happened. Drop the minus signs. Average them.

> **MAE 8.17** means the typical projection misses the real rating by about **8 points**.

That is the whole definition. Two things make it useful and one makes it misleading:

- **Useful:** it is in the units you already understand. An MAE of 8 on a 30–99 scale is a
  miss of roughly one tier — projecting a 78 who turns out to be an 86.
- **Useful:** dropping the signs means overshooting by 10 and undershooting by 10 both count
  as 10. An average error of "zero" would hide a model that is wildly wrong in both directions.
- **Misleading on its own.** A number like 8.17 looks precise. It is only meaningful next to a
  baseline, and the honest baseline is the dumbest possible rule:

| Method | MAE |
|---|---|
| Assume every player repeats last season ("naive carry-forward") | **9.10** |
| Our offensive model | **8.17** |
| Assume every player repeats last season, defence | **9.63** |
| Our defensive model | **8.45** |

**The model is worth about one rating point over guessing "no change."** That is a real
improvement and it is also the correct size to hold in your head. Anyone quoting 8.17 without
9.10 beside it is selling something.

### The other three numbers you will see

- **Coverage** — we publish an 80% range with every projection. "Coverage 80.6%" means that
  when we checked, the true answer landed inside that range 80.6% of the time. Close to 80 is
  the goal; much higher means the range is uselessly wide, much lower means we are overconfident.
- **Spread (`sd_ratio`)** — how wide our predictions are compared to how wide reality is.
  **76% for offence, 70% for defence**: our projections are noticeably more bunched together
  than real outcomes. This is deliberate and is explained in stage 4.
- **`vs_cohort`** — the projection minus what similar players historically did next. This, not
  the raw projection, is the actual claim. "+7" means we think he beats his peers; "−7" means
  we think he trails them.

---

## The worked example

**Jaden Mickey**, defensive back. Notre Dame 2022–2024, Boise State 2025.

| Season | Team | Games | EDGE percentile | OVR |
|---|---|---:|---:|---:|
| 2022 | Notre Dame | 7 | 10.3 | 50.2 |
| 2023 | Notre Dame | 9 | 58.2 | 72.9 |
| 2024 | Notre Dame | **3** — interrupted | 15.6 | 63.5 |
| 2025 | **Boise State** | 11 | **92.8** | **85.2** |
| 2026 | Boise State | — | — | **projected 73.7** |

A career year, a transfer that worked, and the model says he drops 11.5 points. Here is exactly
how it gets there.

---

## Stage 1 — the career becomes a curve

Raw EDGE scores are not comparable across seasons or positions, so every season is converted to
a **percentile within its own season and position group**. Mickey's career becomes:

```text
10.3  →  58.2  →  15.6  →  92.8
```

This is the object the model reasons about. Not his rating, not his stats — the *shape* of that
path.

## Stage 2 — the shape becomes ~30 numbers

Slope, acceleration, distance from peak, career mean, consistency, availability, strength of
opponents faced, class year, recruiting grade, and the cohort baseline from stage 3.

Crucially, the shape features are computed **twice**: once over the raw path, and once over
*healthy seasons only* — seasons where he was actually available. Both sets are fed to the
model, on the theory that the gap between them is itself the signal that something was
interrupted, and the model can learn which to trust.

**This is where Mickey's projection is decided,** so it is worth doing the arithmetic.

*Acceleration* is a second difference — the change in the change.

```text
raw path      10.3 → 58.2 → 15.6 → 92.8
  changes            +47.9  −42.6  +77.2
  acceleration              −90.5  +119.8      ← ends at roughly +120

healthy only  10.3 → 58.2 → 92.8              (2024 dropped: 3 games)
  changes            +47.9  +34.6
  acceleration              −13.3              ← roughly −13
```

The missed 2024 digs a false trough. Climbing out of a trough that was never a decline
registers as a **violent upward spike**. The model has learned — correctly, across thousands of
careers — that violent spikes regress. It applies that lesson here, to a spike that is an
artifact of a missed season rather than a real surge.

A second, smaller contributor: the consistency feature is **direction-blind**. A steady climb
of 10 → 58 → 93 scores as "inconsistent" identically to a player bouncing between those values.

## Stage 3 — the cohort baseline

Separately, we ask what actually happened to similar players: same position, same class year,
same production decile. For Mickey:

> Seniors at this production level historically lost **4.5** OVR the next season (n = 31).

So before the model says anything, the expectation for a player like him is `85.2 − 4.5 ≈ 80.7`.

## Stage 4 — the model, and a deliberate half-measure

An XGBoost regressor predicts next season's OVR from those ~30 features. Then its output is
stretched:

```text
calibrated = mu + k × (prediction − mu)
k          = 1 + 0.5 × (real_spread / predicted_spread − 1)
```

Machine-learning regressors minimise average error, and the way to do that is to predict close
to the average — which makes every projection too timid and erases the extremes that make a
projection interesting. The stretch pushes predictions back outward, but only **halfway**
(`VARIANCE_LAMBDA = 0.5`).

**That half-measure is why the spread is 70–76% of reality**, and it has a visible consequence:
the model under-calls decline at every level. Historically 81% of players rated 90+ decline the
next year; we project 66% of them to. It is a deliberate trade — going further would improve
honesty and worsen MAE.

## Stage 5 — the range

The 80% range is not calculated from theory. We take the model's errors on a held-out set of
players, bucket them by **position and** predicted rating, and read off the 10th and 90th
percentiles. Below 60 rows in a cell the position's own band is noise and the family's is used
instead.

**Per position since v4.5.** One band for all of defence gave a corner and a linebacker the
same interval, and coverage ran from 72.8% (CB) to 84.6% (DL) against an 80% target that only
held in aggregate — two errors in opposite directions, cancelling. Measured per position,
defence's mean distance from the target falls from 4.2 to 2.3 points and CB lands at 80.4%.
Offence was already calibrated and did not move (1.7 points either way).

The obvious way to do this was to scale each position's width by its measured reliability —
a corner's rating disagrees with itself more, so his interval should be wider. It was built
first and **rejected on its own gate**: it fixed CB and DB and broke S, LB, TE and QB, taking
the mean distance from target from 3.2 to 5.9 points. Reliability bounds what a rating can
*know*; it does not describe how a projection of it *errs*, and those are different quantities
however closely related they sound. See `ALTERNATIVES.md` §D8.

For Mickey: prediction 73.7, range **[57.6, 83.6]**.

**Note what that range excludes: his own 85.2.** The model is asserting worse than 1-in-10 odds
that he repeats last season. Across all 5,883 projections only 0.6% make a claim that strong —
though 29.6% of players rated 90+ do.

---

## Putting Mickey together

The model reports its four biggest drivers:

| Driver | Effect |
|---|---|
| `pct_accel` — recent acceleration | **−3.65** |
| `ovr` — current rating | +2.34 |
| `cohort_next` — cohort baseline | +1.66 |
| `n_seasons` — experience | +0.97 |

Cohort says 80.7. The acceleration artifact takes most of the difference down to 73.7 — 7.0
below even the cohort baseline. The comparables it found (Trayvon Henderson, Trevon Flowers,
Tyler Baker-Williams) averaged −8.6, which is the model showing its work honestly: it found
players with similar *shapes* and they did decline.

---

## What the projection does **not** know

This is the part worth reacting to.

| | Known? |
|---|---|
| Who he played **for** — the strength of his own program each season | **No.** There is no own-team feature at all. |
| That he **transferred** | **No.** `FEATURE_COLS` has no transfer term. |
| **Where** he transferred to, or whether it was a step up or down | **No.** |
| What he did **at each stop** | **Partly.** The percentile path is per season, so implicitly per stop — but which school is not attached. |
| Who he played **against** | **Yes.** `opp_sp_last` and `opp_sp_trend`, and EDGE is already opponent-adjusted game by game — so his 92.8 at Boise State is *not* inflated by a weaker schedule. |
| Whether he is likely to **leave for the NFL** | **No.** The cohort curves are built only from players who stayed, which is why the top of the scale is optimistic. |

To the model, a defensive back who moved from Notre Dame to Boise State and immediately
produced at the 93rd percentile is indistinguishable from one who did it in place.

---

## Two things to hold together

It is tempting to read Mickey and conclude the engine is pessimistic. It is not:

- **Interrupted careers are treated *more* generously than clean ones** — mean projected change
  **+6.06** versus **+3.55**, and 20.5% projected down versus 29.3%. The interruption handling
  works in aggregate. Mickey is a tail case where one feature dominated.
- **The systemic error runs the other way.** Spread is compressed to 70–76%, so the model
  under-predicts decline at *every* rating level.

Those two facts pull in opposite directions, which is precisely why the fix is not one dial.
Anything that makes Mickey look better by making the model more optimistic makes the bigger
problem worse.

---

## Where the numbers in this document came from

- Holdout metrics, `trajectory.json._meta`: offence n=2,136, naive 9.10, model 8.17,
  coverage 80.6%, sd_ratio 0.761. Defence n=4,301, naive 9.63, model 8.45, coverage 78.8%,
  sd_ratio 0.695.
- Mickey's drivers, comparables and path: `trajectory_detail.json`, key `26476`.
- Interrupted-vs-clean comparison and the decline-rate table: computed across all 5,883
  projections and 18 seasons of historical transitions, 2026-08-12.
