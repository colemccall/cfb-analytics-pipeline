# CFB Analytics — Audit Findings & Design Rationale

This document records the durable conclusions from the v2.0 audit. It explains *why* key decisions
were made so future contributors don't re-litigate them from scratch.

---

## 1. Position Group System — Why 12 Groups

**Old system (10 groups):** QB, RB, WR, TE, OL, DL, LB, DB, K, P  
**New system (12 groups):** QB, RB, WR, TE, OL, **EDGE**, DL, LB, **CB**, **S**, K, P

### EDGE (new group, split from DL + LB)

Raw API positions mapped here: `EDGE`, `OLB`, `DE`

Modern CFB uses OLBs and 4-3 DEs primarily as pass rushers. Their dominant stats are
sacks + hurries + TFL, not interior tackles. Grouping them with interior DTs (who absorb
blocks and generate different stat signatures) produced biased ratings in both directions.

`DE` in the CFB API means "4-3 end or 3-4 rush end" — an edge rusher. True interior linemen
are `DT`/`NT`/`NG`. The rare 3-4 nose-end hybrid still evaluates correctly on disruption stats.

### CB and S (split from DB)

Old system had a single `DB` group. Problem: corners accumulate PBU/INTs; safeties accumulate
tackles and coverage assists. Rating them together meant whichever stat type had more volume
in a given season dominated the entire group. The split fixes this cleanly.

- **CB**: `CB`, `NB`, generic `DB` (most common fallback)
- **S**: `S`, `FS`, `SS`, `SAF`

### LB = interior only

`MLB` and `ILB` are true interior linebackers. Removing `OLB` from this group makes the
LB formula cleaner (tackling + coverage focus without edge-rusher pollution).

### Why MLB was falling to ATH

The old `POSITION_GROUP_MAP` had no entry for `MLB` or `ILB`, so they fell through to the
`ATH` catch-all and received no rating. Fixed in v2.0 by explicitly mapping both to `LB`.

---

## 2. Multi-Tier Playing-Time System — Why 4 Tiers

**Old system:** Binary starter / not-starter threshold. Players below the threshold got
a recruiting-only fallback at the same level as redshirts who never played.

**New system:** 4 tiers, each controlling how much weight stats get vs recruiting:

| Tier | Formula | Notes |
|------|---------|-------|
| Starter | 100% stats formula | Full EDGE weight, formula drives rating |
| Role | 75% stats + 25% recruiting | Meaningful but limited sample |
| Reserve | 40% stats + 60% recruiting | Recruiting anchors more heavily |
| Bench | 100% recruiting | No production to evaluate |

**Why no caps:** A 5-star backup at Alabama shouldn't be capped at 60 just because there's
a star above them. They haven't had the *opportunity* to prove it, but their recruiting
signal is real. The tier system controls *trust in stats*, not the ceiling.

**The Sire Gaines 2024 case:** ~30 carries, great game at Georgia Southern, below the old
60-carry starter threshold → old system gave him a pure recruiting fallback. New system:
30 carries ≥ 25 role threshold → role tier → blended formula → rating reflects "showed
potential but limited sample."

**Distribution shape:** The sigmoid normalization already handles loaded vs lean classes
naturally. Loaded classes cluster higher; lean classes cluster lower. That's honest and
intentional — no artificial flattening.

---

## 3. Per-Game Opponent Quality Adjustment

**Old system:** Season-averaged opponent SP+ (too coarse). A defender who had 4 sacks vs
Alabama and 1 vs UMass gets the same multiplier as one who had 1 vs Alabama and 4 vs UMass.

**New system:** Per-game opponent adjustment using the opponent's SP+ offense (for defenders)
or SP+ defense (for offensive players) for each specific game.

**Why per-game matters for Jeanty 2024:** Boise State played Georgia, Oregon, and Penn State
in the playoffs. Season-averaged SP+ dilutes this. Per-game adjustment correctly inflates
those playoff contributions relative to the Mountain West schedule.

**Coverage by position (expected):**
- QB/RB/WR/TE: play-level EPA attribution (~65-90% of starters with valid EDGE)
- EDGE/DL/LB/CB/S: per-game stat composite × per-game opponent SP+ offense (~95% of starters)
- OL/K/P: team-level proxy or formula-only (no EDGE)

**Implementation:** Script 08 has two separate paths:
- `compute_edge()`: offensive positions, play-level EPA × situation weight × opp multiplier
- `compute_defensive_edge_per_game()`: defensive positions, stat composite × per-game opp SP+ offense

---

## 4. Same-Name Same-Season Collisions

`player_seasons` (one row per player × season × team) solves *cross-season* collisions.
It does not solve *same-season same-name* (two QBs named "Jake Williams" both playing in 2024).

**Defense in depth:**
1. Use `cfb_api_id` as canonical identity wherever the API provides it
2. Multi-attribute matching for scraped sources: name + team + position + class_year
3. If multiple players match name+team but can't be disambiguated: return None (don't guess)
4. Diagnostic query runs as part of script 01 verification — surfaces any same-season
   same-team duplicate names

**Ryan Williams case (Alabama WR vs Western Kentucky):** Two distinct players. Diagnostic
query identifies contamination by checking distinct `cfb_api_id` values per name. Fix:
delete the wrong `player_seasons` rows. The v2 model means this only affects that specific
(player_id, season, team_id) record, not the player's entire career.

---

## 5. EDGE Definition by Position

**Offensive positions (QB/RB/WR/TE):** EDGE = situation-weighted, opponent-adjusted EPA
per play. Aggregated as `sum(adj_epa) / sqrt(plays_counted)` to reward volume without
over-indexing on a handful of big plays.

**Defensive positions (EDGE/DL/LB/CB/S):** EDGE = weighted stat composite per game ×
per-game opponent SP+ offense multiplier, aggregated as `sum(adj_score) / sqrt(games_played)`.

Stat composites by position:
- EDGE: `sacks*5 + hur*1.5 + tfl*2 + tot*0.1`
- DL: `sacks*4 + hur*1 + tfl*2.5 + tot*0.15`
- LB: `sacks*3 + tfl*2 + tot*0.5 + ints*3 + pbu*1.5`
- CB: `ints*4 + pbu*2 + tot*0.3 + tfl*1`
- S: `tot*0.6 + ints*3.5 + pbu*1.5 + tfl*1.5 + sacks*2`

**Why old defensive EDGE had <2% coverage:** Script 08 was attempting to attribute
defenders from play text via regex parsing. The CFB API play text doesn't consistently
name defenders. The per-game stat composite approach uses the `game_aggregate` stats rows
(populated by `upsert_game_stats()` in script 01) which have near-complete coverage.

---

## 6. NIL and Coaching Changes — Why Deferred

**NIL:** On3 is JS-rendered (Playwright needed), layout changes frequently, and NIL's
primary value is research correlation ("NIL vs production scatter") rather than rating
prediction. Adding it to the formula with empty data creates dead weight. Deferred to a
later phase where it will be added as a research feature, not a rating component.

**Coaching changes:** The seed CSV has 22 rows (manual entry, HC tenures only). Applying
a ratings penalty based on incomplete data would bias the distribution against teams that
happen to be in the data. Deferred until API-sourced HC + OC/DC data is comprehensive.

---

## 7. Distribution Validation Requirements

After any weight or tier change, script 06 prints per-position distribution stats and
flags violations. Targets (informational, not hard block):

| Position | mean | p10 | p50 | p90 | p99 |
|----------|------|-----|-----|-----|-----|
| All | 62–68 | 35–50 | 62–70 | 80–87 | 92–98 |

**Intentional variance:** Loaded classes (2024 RBs, 2021 WRs) will cluster higher. That's
correct — the distribution isn't supposed to be identical every year. Validation warns on
extreme drift (mean < 58 or > 72) but does not block the upsert.

**Hard rule:** Never adjust weights to fix one player. If a single player looks wrong,
check the data first (contamination, stats mismatch). Only tune weights when the
*entire position group distribution* is off-target.

---

## 8. Known Remaining Gaps (Out of Scope for This Release)

| Gap | Status | Notes |
|-----|--------|-------|
| OL individual differentiation | Future phase | Requires PFF grades or EA Sports OVR; team proxy is best available |
| Engine B (breakout XGBoost) | Separate project | Lagged 2021–2024 features → 2025 breakout prediction |
| NIL data | Deferred | Playwright scraper needed; research-only when built |
| Coaching changes (full) | Deferred | API for HC; manual for OC/DC; not in formula until complete |
| Per-play opponent quality | Not needed | Per-game opp SP+ is sufficient granularity for this release |
| Sub-ratings computation | Placeholder | `sub_ratings` column exists; script 06 doesn't populate it yet |
