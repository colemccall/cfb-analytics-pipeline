# tests/

The pytest suite. Run it with the project's virtual environment (pytest is not in the system Python):

```bash
.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
```

As of v4.24: **564 pass and 1 fails by design.** The failure is
`test_export_contract.py::TestRatingBasis::test_recruiting_basis_rows_exist_and_are_not_rare` (recruiting-basis
rows are 4.7% of rated rows against a 5% floor). It has been left failing on purpose since v4.19: low-game players
moved from a recruiting to a blended basis, and the root `CLAUDE.md` rule is that a gate is never changed in the same
change as the output it judges. It waits on the owner's decision.

## What guards what

| File | Guards |
|---|---|
| `test_export_contract.py` | **The seam between the repos**: everything written into `cfb-analytics-app/data/`, valid JSON (no `NaN` token), one season per file, one row per player-season, the live and weekly fields. Reads the real export, so run it after script 12. |
| `test_frontend_config.py` | Constants in the app's `js/config.js` that must agree with what the exporter wrote. |
| `test_edge_anchors.py`, `test_composite_to_100.py`, `test_composite_to_ovr.py`, `test_playtime_tier.py` | The frozen mappings: EDGE → rating anchors, recruiting composite scale, playing-time tiers. |
| `test_coverage_credit.py`, `test_defense_v43.py`, `test_player_inputs_v420.py` | What a player's game score is made of (defence credits; the v4.20 QB efficiency blend and FCS weight). |
| `test_low_game_ratings.py`, `test_slots_and_pace.py`, `test_player_inputs_v420.py` | Part-season players: the low-game rule, the in-season pace scale, share of team games. |
| `test_team_ratings.py`, `test_schedule_adjust.py`, `test_results_rating.py`, `test_line_unit.py` | The team rating, its schedule adjustment, power, and the line unit that replaced the withdrawn OL rating. |
| `test_weekly_progression.py` | Week-by-week snapshots: box-score totals equal `/stats/season`, the game grades, records through a week, the last week equals the live file. |
| `test_store_*.py`, `test_clean_nan.py`, `test_api_cache.py` | The persistence layer: per-season replace, `CFB_DATA_DIR`, the shutout rule, the NaN rule, the cache-bypass switch. |
| `test_stat_agg.py`, `test_matching.py`, `test_interruptions.py`, `test_coaching.py`, `test_shrinkage.py`, `test_headshots.py` | Helpers in `utils/` and the research scripts. |
| `test_docstrings.py` | The documentation rule: every module-level function over five lines has a docstring. |

Tests that read real data (`test_export_contract.py`, parts of `test_weekly_progression.py`) describe the files as
they are on disk — rerun them after any export.
