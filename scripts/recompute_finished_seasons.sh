#!/usr/bin/env bash
# Full recompute of every finished season, in the documented order (root CLAUDE.md §4):
#   06 -> 07 -> 10 -> 11 -> 15 -> 16 -> 10 (projected) -> 13 -> 14 -> 12
# Run all of 06 before any 07 (07's pool averages read every season's EDGE).
#
# 2026 is NOT in the loops: it is a season being played, and 06/07 would write a part-season into the finished-season
# rows script 15 trains on. 2026 is rated by scripts 17 and 17b in a private copy.
#
# Offline by design: a dead proxy makes any cache miss fail the step instead of reaching the API (v4.24: a failed
# request raises). Stops at the first failing step.
#
#   bash scripts/recompute_finished_seasons.sh 2>&1 | tee recompute.log
set -euo pipefail
cd "$(dirname "$0")/.."
export HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 PYTHONIOENCODING=utf-8
unset CFB_REFRESH_YEARS
PY=./.venv/Scripts/python.exe
SEASONS=$(seq 2008 2025)

step() { echo; echo "=== $(date +%H:%M:%S) $*"; }

for s in $SEASONS; do step "06 $s"; $PY -B scripts/06_compute_edge_scores.py --season "$s"; done
for s in $SEASONS; do step "07 $s"; $PY -B scripts/07_compute_player_ratings.py --season "$s"; done
for s in $SEASONS; do step "10 $s"; $PY -B scripts/10_compute_team_ratings.py --season "$s"; done
step "11"; $PY -B scripts/11_compute_engine_b_ratings.py
step "15 --retrain"; $PY -B scripts/15_predict_trajectories.py --retrain
step "16"; $PY -B scripts/16_project_ratings.py --season 2026
step "10 2026 projected"; $PY -B scripts/10_compute_team_ratings.py --season 2026 --engine projected
step "13"; $PY -B scripts/13_team_performance_evaluator.py
step "14"; $PY -B scripts/14_recruiting_roi.py
echo; echo "=== $(date +%H:%M:%S) recompute of 2008-2025 complete (12 is run separately, after 17 and 17b)"
