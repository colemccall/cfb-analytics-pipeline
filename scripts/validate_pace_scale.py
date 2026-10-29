"""Does the in-season pace scale recover the final EDGE? Reproduces the constants PACE_SHARE_K in scripts/06_compute_edge_scores.py.

Read-only against real data. For each season it runs script 06 on a PRIVATE copy of the data (CFB_DATA_DIR): the whole season, then the
same season cut after regular-season weeks 4, 6, 8 and 10 with --in-progress. Every player rated in both runs is one row: his
part-season EDGE (edge_raw), the team's scheduled/completed ratio, his games, and his final EDGE. Candidate scales are compared
leave-one-season-out, by the miss in RATING points (the final EDGE and the scaled one both through the frozen anchors), with the
signed miss by tier of the scaled rating:

    A  the paced value script 06 stores (v4.20: C; through v4.19: sqrt(r), a shrink toward the position mean and a share calibration)
    B  sqrt(r) alone
    C  sqrt(r) x a calibration by the share of his team's games he played (shipped from v4.20)
    D  C by position family      E  C by family and share      F  E by cut week

Prints the refit constants (median final / sqrt(r)-scaled by share bucket). Takes about half an hour for five seasons.

Usage:
    python scripts/validate_pace_scale.py                       # 2021-2025
    python scripts/validate_pace_scale.py --seasons 2024 2025
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.store import read_raw  # noqa: E402

_spec = importlib.util.spec_from_file_location("_t07pace", ROOT / "scripts" / "07_compute_player_ratings.py")
t07 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t07)

CUTS = [4, 6, 8, 10]
FAMILY = {"QB": "QB", "RB": "RB", "WR": "WR/TE", "TE": "WR/TE", "DL": "DL/EDGE", "EDGE": "DL/EDGE", "LB": "LB", "CB": "DB", "S": "DB", "DB": "DB"}
SHARE_EDGES = (0.5, 0.75, 0.9)


def run_06(data_dir: Path, season: int, extra: list) -> pd.DataFrame:
    """Run script 06 on `data_dir` (offline: any cache miss fails locally) and return that season's player_edge rows."""
    env = dict(os.environ, CFB_DATA_DIR=str(data_dir), PYTHONIOENCODING="utf-8", CFB_API_KEY="offline",
               HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    cmd = [sys.executable, "-B", "-W", "ignore", str(ROOT / "scripts" / "06_compute_edge_scores.py"), "--season", str(season)] + extra
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if out.returncode:
        raise RuntimeError(f"06 failed: {out.stderr[-400:]}")
    df = pd.DataFrame(json.loads((data_dir / "raw" / "player_edge.json").read_text(encoding="utf-8")))
    return df[df["season"] == season].copy()


def collect(seasons: list) -> pd.DataFrame:
    """One row per player-cut: part-season EDGE, the pace ratio, games, the team's completed games, final EDGE, position."""
    tmp = Path(tempfile.mkdtemp(prefix="pacescale_"))
    rows = []
    try:
        for sub in ("raw", "computed"):
            shutil.copytree(ROOT / "data" / sub, tmp / sub)
        ps = pd.DataFrame(json.loads((tmp / "raw" / "player_seasons.json").read_text(encoding="utf-8")))
        ps = ps[["id", "team_id", "position_group"]].rename(columns={"id": "player_season_id"})
        games = read_raw("games")
        for s in seasons:
            full = run_06(tmp, s, [])[["player_season_id", "edge_score"]].rename(columns={"edge_score": "final"})
            g = games[(games["season"] == s) & (games["season_type"] == "regular")]
            sched = {}
            for col in ("home_team_id", "away_team_id"):
                for t, n in g.groupby(col).size().items():
                    sched[int(t)] = sched.get(int(t), 0) + int(n)
            for c in CUTS:
                cut = run_06(tmp, s, ["--through-week", str(c), "--in-progress"])
                d = cut.merge(full, on="player_season_id").merge(ps, on="player_season_id")
                d = d[d["edge_raw"].notna() & d["final"].notna() & (d["edge_raw"] > 0) & (d["final"] > 0) & d["pace_factor"].notna()].copy()
                d["comp"] = (d["team_id"].map(lambda t: sched.get(int(t)) if t == t else np.nan) / d["pace_factor"]).round()
                d["season"], d["cut"] = s, c
                rows.append(d)
            print(f"  {s} done", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    D = pd.concat(rows, ignore_index=True)
    D = D[D["position_group"].isin(FAMILY) & D["comp"].notna() & (D["comp"] > 0)].copy()
    D["fam"] = D["position_group"].map(FAMILY)
    D["share"] = np.minimum(1.0, D["games_played"] / D["comp"])
    D["sb"] = [int(sum(x > e for e in SHARE_EDGES)) for x in D["share"]]
    D["cb"] = pd.cut(D["cut"], [0, 5, 7, 9, 20], labels=False)
    D["m1"] = D["edge_raw"] * np.sqrt(D["pace_factor"])
    return D


def fit_predict(train: pd.DataFrame, test: pd.DataFrame, keys: list) -> np.ndarray:
    """Predicted final EDGE = m1 x the median final/m1 of the training rows in the same cell (global median when a cell is empty)."""
    rho = train["final"] / train["m1"]
    k = rho.groupby([train[c] for c in keys]).median()
    glob = float(rho.median())
    return test["m1"].values * np.array([k.get(tuple(r) if len(keys) > 1 else r[0], glob) for r in test[keys].itertuples(index=False)])


def to_rating(edge, pg) -> np.ndarray:
    """EDGE through the frozen anchors, per row."""
    return np.array([t07.edge_to_ovr(x, p) for x, p in zip(edge, pg)])


def report(D: pd.DataFrame) -> None:
    """Print the leave-one-season-out comparison and the refit share constants."""
    D = D.copy()
    D["R_final"] = to_rating(D["final"], D["position_group"])
    models = {"A stored paced value (06 as it stands)": None, "B sqrt(r) alone": [], "C + share bucket": ["sb"], "D + position family": ["fam"],
              "E + family x share": ["fam", "sb"], "F + family x share x cut": ["fam", "sb", "cb"]}
    print(f"\n{len(D):,} player-cuts, seasons {sorted(D['season'].unique())}; miss = rating(final) - rating(model), so + means the model is low")
    print(f"{'model':42s} {'MAE':>6s} {'bias':>7s}   tiers <60 / 60-70 / 70-80 / 80+      85+: mean miss")
    for name, keys in models.items():
        pred = np.zeros(len(D))
        for s in sorted(D["season"].unique()):
            te = (D["season"] == s).values
            if keys is None:
                pred[te] = D["edge_score"].values[te]          # the stored paced value (whatever script 06 produced when this ran)
            elif keys == []:
                pred[te] = D["m1"].values[te]
            else:
                pred[te] = fit_predict(D[~te], D[te], keys)
        r = to_rating(pred, D["position_group"])
        e = D["R_final"].values - r
        tiers = " / ".join(f"{e[(r >= lo) & (r < hi)].mean():+.2f}" for lo, hi in ((0, 60), (60, 70), (70, 80), (80, 101)))
        print(f"{name:42s} {np.abs(e).mean():6.3f} {e.mean():+7.3f}   {tiers:34s} {e[r >= 85].mean():+.2f}")
    k = (D["final"] / D["m1"]).groupby(D["sb"]).median()
    print("\nrefit constants, median final / sqrt(r)-scaled by share of team games played (<=0.5, <=0.75, <=0.9, above):",
          [round(float(x), 4) for x in k.values])


def main() -> int:
    """Collect, report, and return 1 if the shipped constants are more than 0.03 from the refit."""
    ap = argparse.ArgumentParser(description="Back-test the in-season pace scale over several seasons")
    ap.add_argument("--seasons", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    args = ap.parse_args()
    D = collect(args.seasons)
    report(D)
    spec = importlib.util.spec_from_file_location("_t06pace", ROOT / "scripts" / "06_compute_edge_scores.py")
    t06 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t06)
    k = (D["final"] / D["m1"]).groupby(D["sb"]).median().values
    return 1 if np.max(np.abs(k - np.array(t06.PACE_SHARE_K))) > 0.03 else 0


if __name__ == "__main__":
    sys.exit(main())
