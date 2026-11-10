# Frozen weekly snapshots, and a sketch of the week-over-week change view

*v4.26, 2026-11-06. The writer is `scripts/22_freeze_snapshot.py`; the tests are `tests/test_snapshots.py`.*

## Why

Every week `live_{season}.json` is rebuilt, and the week-by-week replay (`17b`) is rebuilt from scratch with today's code.
Neither can answer two questions:
- what did the site show on a given day;
- did a number move because games were played or because the model changed?

A snapshot answers both. It holds the published numbers of one completed week, stamped with the model that made them, and it
is never regenerated or overwritten.

## Structure

```
cfb-analytics-app/data/snapshots/
  index.json                         every snapshot, oldest first; append-only
  2026-wk09-v4.26/                   {season}-wk{week}-{release}
    manifest.json                    the stamp, the kind of step, each file's sha256
    teams.json                       {fields, rows}: team_id, school, rating, offense, defense, power, roster, stats, results,
                                     wins, losses, rank_rating, rank_power
    players.json                     {fields, rows}: player_id, team_id, position_group, rating, basis, games (rated players only)
```

The manifest stamps each snapshot with:

| Field | What it is |
|---|---|
| `release` | `CONFIG.VERSION` at freeze (the human label; `tests/test_frontend_config.py` holds it equal to the newest Build Log entry) |
| `model_fingerprint` | sha256 of the rating code's syntax trees with docstrings removed (`utils/model_version.py`): a comment or docstring edit leaves it alone, any code edit changes it |
| `pipeline` | the pipeline's git commit and whether its working tree was dirty |
| `frozen_at` | the run date |
| `source` | the live file it was frozen from, when that file was generated, and its sha256 |
| `games` | games-played state: completed, scheduled, and scored games after the last complete week |
| `kind`, `previous` | what kind of step this is (below) and the season's previous snapshot |
| `note` | optional: anything the stamp alone would mislead about |

Teams and players carry their own games-played state: wins and losses per team, games per player.

## Rules (enforced by the writer, tested)

1. **Never overwritten.** If the folder or its index entry exists, the run is refused.
2. **Never silently changed.** Before writing, every listed snapshot is re-hashed. If any file differs from its manifest, the run
   stops. `python scripts/22_freeze_snapshot.py --verify` does the same check on its own, and the test suite runs it on every
   snapshot in the checkout.
3. **Every step changes something.** Against the season's previous snapshot, the new one is classified:

   | Kind | Week | Model fingerprint | Meaning |
   |---|---|---|---|
   | `first` | — | — | the season's first snapshot |
   | `data` | new | same | games were played; the model did not change |
   | `model` | same | new | a release changed the code; same games |
   | `data+model` | new | new | both; the change view separates them with the replay (below) |

   If neither the week nor the fingerprint changed, the run is refused even under a new release label. A UI-only release
   freezes nothing new.
4. **A model change needs a release.** A new fingerprint at the same week under the same release label would collide with an
   existing folder name, so it is refused. Bump `CONFIG.VERSION` and write the Build Log entry first.

## Where it sits in the weekly run

```bash
python scripts/01b_refresh_in_season.py --season 2026 --weeks N --stage   # then validate_week_ingest.py, then --merge
python scripts/17_live_season.py --season 2026 --offline                  # live_2026.json
python scripts/17b_weekly_progression.py --season 2026                    # weekly_*_2026.json
python scripts/12_export_frontend_json.py --season 2026 --cards           # the app's files, and cards/ for the player cards
python scripts/21_export_contract.py                                      # data/exports/v1 for the sibling apps
python scripts/22_freeze_snapshot.py                                      # data/snapshots/2026-wkNN-<release>/
```

Freeze after the export, so the snapshot is exactly what the site is about to show, and commit it with the app's data.

When a release changes the model mid-week, run 17, 12 and 22 again at the same week. The snapshot it writes is a `model` step:
the same games under the new code. That one extra run is what makes the change view below exact.

## The first snapshot

`2026-wk09-v4.26` was frozen on 2026-11-06 from the live file generated on 2026-11-02, after the fact. Its stamp names the code
at freeze, not the code that generated the file; its `note` says so. Between the two, v4.25 changed only the app, and v4.25.1's
two pipeline changes give the same output for a season in progress:
- the replay builder's postseason rule (no 2026 postseason exists);
- `season_in_progress` (the same verdict for 2026 at every week, measured in the v4.25.1 Build Log entry).

Weeks 1 to 8 of 2026 cannot be frozen honestly now: what the site showed then is in the app repository's git history, not on
disk. Recovering them is in `IDEAS.md`.

---

## Sketch: the week-over-week change view (proposed, not built)

**The question it answers.** "Boise State went from 45th to 41st and from 79.2 to 80.4 this week. How much of that is the
games they played, and how much is a change we made?"

**The decomposition.** Let S(w, m) be the published number for week w under model m. A frozen snapshot holds S(w, m_then).
The replay holds S(w, m_now) for every past week, because 17b rebuilds every week with today's code. So for a team or player
between snapshot A (week w, model a) and snapshot B (week w+1, model b):

```
total change = S(w+1, b) − S(w, a)
             = [S(w, b) − S(w, a)]        the model step: same games, new code   (replay week w under b, minus snapshot A)
             + [S(w+1, b) − S(w, b)]      the data step: new games, same code    (snapshot B, minus replay week w under b)
```

When a = b, the model step is zero by construction and the whole change is data. When a release ships mid-week and a `model`
snapshot was frozen (above), both terms come straight from snapshots, with no replay needed.

**Caveats the view would print:**
- the replay's team numbers leave out the line unit (up to 0.9 points);
- a late stat correction to an earlier week shows up as data;
- a week that is not 90% complete is not frozen.

**Mock** (a team card panel, desktop; a phone stacks the two columns). The values are illustrative, not measured:

```
┌ THIS WEEK ─────────────────────────────── week 9 → week 10 ┐
│ Rating     79.2 → 80.4   (+1.2)                            │
│   games    +1.5   beat Utah State 31–17 (graded 84)        │
│   model    −0.3   v4.27: schedule-adjusted stats re-fit  → │  ← link to the Build Log entry
│ Power rank   45 → 41    games +4 · model 0                 │
│ ─────────────────────────────────────────────────────────  │
│ Players who moved most        games   model                │
│   Sire Gaines RB   80.6→82.1  +1.5    0                    │
│   …                                                        │
│ Frozen: 2026-wk09-v4.26 · 2026-wk10-v4.27  (both verify)   │
└────────────────────────────────────────────────────────────┘
```

A season-wide version would be a Rankings column ("Δ games, Δ model") with the same split, and the Build Log would link each
release to the model step it caused ("v4.27 moved 131 teams, mean 0.4"), which makes "what did this release do to the numbers"
a measured answer.

**What must be true first:**
- at least two snapshots of a season (week 10 will be the second);
- a `model` snapshot frozen at every mid-week release;
- the decision that the replay may stand in for a missing model step, with its line-unit caveat.

**Build effort:** a dataLoader function for the index and two snapshots, one panel on the team card, one on the player card,
and a Rankings column. Page code: it is a design change, so it waits for the owner's go-ahead.
