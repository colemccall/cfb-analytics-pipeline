"""Does a results rating add anything to a quality rating when ranking teams mid-season?

Reproduces the measurement behind utils/results_rating.py's constants. For every season 2016-2025
except 2020, cut the regular season after week 8 and predict every LATER regular-season game between
two FBS teams from information available at the cut:

  quality  the team's PREVIOUS season final rating (script 10), the best quality reading that exists
           at the cut for a past season
  results  the opponent-adjusted margin rating (SRS) from weeks 1-8 only

Models are fitted leave-one-season-out, so the season being scored never trains its own weights.
Reports winner accuracy and margin MAE beside two dumb baselines (the home team always wins; always
predict the average margin) and a placebo that deals the results ratings out to the wrong teams.
Writes data/computed/ranking_validation.json. The weights fitted on all seasons are compared with
the constants in utils/results_rating.py and the script exits 1 if they have drifted by more than 10%.

Usage:
    python scripts/validate_rankings.py
"""

import json
import statistics as st
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.results_rating import srs_ratings, W_QUALITY, W_RESULTS  # noqa: E402
from utils.store import read_computed, read_raw, COMPUTED_DIR  # noqa: E402


def build_rows():
    """One row per scored game: quality gap, results gap, shuffled results gap, home flag, margin."""
    games = read_raw("games")
    tr = read_computed("team_ratings")
    tr = tr[tr["engine"].fillna("edge") != "projected"] if "engine" in tr.columns else tr
    rating = {(int(r.team_id), int(r.season)): float(r.overall_rating) for r in tr.itertuples() if r.overall_rating == r.overall_rating}
    fbs = set(int(t) for t in tr["team_id"].unique())
    rows = []
    for season in [s for s in range(2016, 2026) if s != 2020]:
        g = games[(games["season"] == season) & (games["season_type"] == "regular")]
        g = g[g["home_score"].notna() & g["away_score"].notna() & g["home_team_id"].notna() & g["away_team_id"].notna()].to_dict("records")
        g = [x for x in g if int(x["home_team_id"]) in fbs and int(x["away_team_id"]) in fbs]
        early = [x for x in g if (x["week"] if x["week"] == x["week"] else 99) <= 8]
        late = [x for x in g if (x["week"] if x["week"] == x["week"] else 0) > 8]
        if len(early) < 200 or len(late) < 100:
            continue
        R, _, _ = srs_ratings(early, fbs)
        shuffled = dict(zip(R.keys(), np.random.default_rng(season).permutation(list(R.values()))))
        for x in late:
            h, a = int(x["home_team_id"]), int(x["away_team_id"])
            qh, qa = rating.get((h, season - 1)), rating.get((a, season - 1))
            if h not in R or a not in R or qh is None or qa is None:
                continue
            rows.append({"season": season, "q": qh - qa, "s": R[h] - R[a], "p": shuffled[h] - shuffled[a],
                         "home": 0.0 if x.get("neutral_site") == True else 1.0,   # noqa: E712
                         "y": float(x["home_score"]) - float(x["away_score"])})
    return rows


def fit(train, cols):
    X = np.array([[r[c] for c in cols] + [r["home"]] for r in train])
    y = np.array([r["y"] for r in train])
    return np.linalg.lstsq(X, y, rcond=None)[0]


def score(rows, cols):
    """Leave-one-season-out score of a margin model on `cols`: (winner accuracy, margin MAE, weights on all seasons)."""
    hit = n = 0
    mae = []
    for season in sorted({r["season"] for r in rows}):
        w = fit([r for r in rows if r["season"] != season], cols)
        for r in (r for r in rows if r["season"] == season):
            pred = sum(w[i] * r[c] for i, c in enumerate(cols)) + w[-1] * r["home"]
            mae.append(abs(pred - r["y"]))
            if r["y"] != 0:
                n += 1
                hit += (pred > 0) == (r["y"] > 0)
    return hit / n, st.mean(mae), fit(rows, cols)


def main() -> int:
    """Run the backtest, print the table, write the JSON; return 1 if the fitted weights drifted from the constants."""
    rows = build_rows()
    seasons = sorted({r["season"] for r in rows})
    print(f"{len(rows):,} scored games, seasons {seasons}")
    nz = [r for r in rows if r["y"] != 0]
    mu = st.mean(r["y"] for r in rows)
    out = {"games": len(rows), "seasons": seasons,
           "home_always_wins": sum(r["y"] > 0 for r in nz) / len(nz),
           "average_margin_mae": st.mean(abs(r["y"] - mu) for r in rows), "models": {}}
    print(f"  home team always wins          winner {out['home_always_wins']:.3f}")
    print(f"  always the average margin      MAE {out['average_margin_mae']:.2f}")
    fitted = None
    for label, cols in (("quality only (last season)", ["q"]), ("results only (SRS wk 1-8)", ["s"]),
                        ("quality + results", ["q", "s"]), ("PLACEBO quality + shuffled results", ["q", "p"])):
        acc, mae, w = score(rows, cols)
        out["models"][label] = {"winner": acc, "mae": mae, "weights": [float(x) for x in w]}
        print(f"  {label:36s} winner {acc:.3f}  MAE {mae:.2f}  weights {np.round(w, 3)}")
        if label == "quality + results":
            fitted = w
    (COMPUTED_DIR / "ranking_validation.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    drift = max(abs(fitted[0] - W_QUALITY) / W_QUALITY, abs(fitted[1] - W_RESULTS) / W_RESULTS)
    print(f"\nfitted weights {fitted[0]:.3f} / {fitted[1]:.3f} vs constants {W_QUALITY} / {W_RESULTS}: drift {drift:.1%}")
    return 1 if drift > 0.10 else 0


if __name__ == "__main__":
    sys.exit(main())
