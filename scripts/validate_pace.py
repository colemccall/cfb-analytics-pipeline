"""Does rescaling a part-season EDGE to full-season pace recover the final EDGE?

Read-only against real data. Runs script 06 twice on a PRIVATE copy of the data
(CFB_DATA_DIR): the whole of a finished season, then the same season cut after
regular-season week N with --in-progress. It then asks what the in-season rating
pipeline has to be able to claim:

1. **Bias.** Without a correction, EDGE is lower at week N than at the end,
   because EDGE = sum(composite * opp_mult) / sqrt(games_played) and a player
   who has played fewer games scores less for the same per-game output. The
   median ratio final / unscaled says by how much. The same ratio after the
   sqrt(r) rescale (r = team games scheduled / team games completed) says how
   much is left.
2. **Order.** Spearman between the week-N EDGE and the final EDGE, per position:
   how well does a part-season rank the players?

It also counts who cannot be rated yet: players with fewer than MIN_GAMES (3)
kept games at the cut, who appear only once the season has progressed.

Measured 2025, cut at week 8 (median team 7 games played of 12): unscaled EDGE
runs 27% under the final value; rescaled it lands at 0.96, i.e. about 4% over;
rank agreement 0.86 (CB) to 0.95 (QB). The numbers print every run.

Usage:
    python scripts/validate_pace.py                      # 2025, cut at week 8
    python scripts/validate_pace.py --season 2024 --through-week 6
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

MIN_PLAYERS = 40


def _copy_data(dst: Path) -> None:
    """Copy data/raw and data/computed into `dst` so 06 can write there freely."""
    for sub in ("raw", "computed"):
        shutil.copytree(ROOT / "data" / sub, dst / sub)


def _run_06(data_dir: Path, season: int, extra: list) -> pd.DataFrame:
    """Run script 06 against `data_dir` and return that season's player_edge rows.

    The child process gets CFB_DATA_DIR so it reads and writes only the private
    copy. Raises if 06 exits non-zero.
    """
    env = dict(os.environ, CFB_DATA_DIR=str(data_dir), PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-B", "-W", "ignore", str(ROOT / "scripts" / "06_compute_edge_scores.py"),
           "--season", str(season)] + extra
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError(f"06 failed: {out.stderr[-400:]}")
    rows = json.loads((data_dir / "raw" / "player_edge.json").read_text(encoding="utf-8"))
    df = pd.DataFrame(rows)
    return df[df["season"] == season].copy()


def compare(full: pd.DataFrame, cut: pd.DataFrame, positions: pd.DataFrame) -> pd.DataFrame:
    """One row per position: bias before and after the pace rescale, and rank agreement.

    `cut` already carries the rescaled edge_score and the raw one (edge_raw), so
    both estimators come from the same rows. Only players rated in BOTH runs count.
    """
    m = (full[["player_season_id", "edge_score"]]
         .merge(cut[["player_season_id", "edge_score", "edge_raw"]], on="player_season_id",
                suffixes=("_final", "_paced"))
         .merge(positions, on="player_season_id")
         .dropna(subset=["edge_score_final", "edge_score_paced", "edge_raw"]))
    rows = []
    for pg, d in m.groupby("position_group"):
        if len(d) < MIN_PLAYERS:
            continue
        rows.append({
            "pos": pg, "n": len(d),
            "spearman": round(float(spearmanr(d["edge_raw"], d["edge_score_final"])[0]), 3),
            "ratio_unscaled": round(float((d["edge_score_final"] / d["edge_raw"]).median()), 3),
            "ratio_paced": round(float((d["edge_score_final"] / d["edge_score_paced"]).median()), 3),
        })
    return pd.DataFrame(rows)


def main() -> None:
    """Run the full and cut passes on a private data copy and print the table.

    Writes data/computed/pace_validation.json (the table plus the headline
    ratios) so the live-season export can quote the measurement it relies on.
    The private copy is removed afterwards.
    """
    ap = argparse.ArgumentParser(description="Back-test the in-season pace correction")
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--through-week", type=int, default=8)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="pace_"))
    try:
        print(f"copying data to {tmp} ...")
        _copy_data(tmp)
        print("full season ...")
        full = _run_06(tmp, args.season, [])
        print(f"cut after week {args.through_week}, --in-progress ...")
        cut = _run_06(tmp, args.season, ["--through-week", str(args.through_week), "--in-progress"])
        ps = pd.DataFrame(json.loads((tmp / "raw" / "player_seasons.json").read_text(encoding="utf-8")))
        positions = ps[ps["season"] == args.season][["id", "position_group"]].rename(columns={"id": "player_season_id"})
        table = compare(full, cut, positions)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    rated_full = set(full.loc[full["edge_score"].notna(), "player_season_id"])
    rated_cut = set(cut.loc[cut["edge_score"].notna(), "player_season_id"])
    pace = cut["pace_factor"].dropna()
    head = {
        "season": args.season, "through_week": args.through_week,
        "median_team_pace_r": round(float(pace.median()), 3) if len(pace) else None,
        "median_ratio_unscaled": round(float(table["ratio_unscaled"].median()), 3),
        "median_ratio_paced": round(float(table["ratio_paced"].median()), 3),
        "rated_at_cut": len(rated_cut), "rated_at_end": len(rated_full),
        "rated_only_at_end": len(rated_full - rated_cut),
    }
    print(f"\n=== {args.season}, cut after week {args.through_week} ===\n")
    print(table.to_string(index=False))
    print(f"\nmedian across positions: final/unscaled {head['median_ratio_unscaled']}, "
          f"final/paced {head['median_ratio_paced']} (1.0 = unbiased)")
    print(f"rated at the cut: {head['rated_at_cut']:,}; rated by the end: {head['rated_at_end']:,}; "
          f"not yet rated at the cut: {head['rated_only_at_end']:,}")
    out = ROOT / "data" / "computed" / "pace_validation.json"
    out.write_text(json.dumps({"summary": head, "by_position": table.to_dict("records")}, indent=1), encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
