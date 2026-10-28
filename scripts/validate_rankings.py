"""Does power (rating + conference + results) rank teams better than the rating alone?

Reproduces the measurement behind utils/results_rating.py's constants. For every season 2016-2025
except 2020, cut the regular season after week 8 and predict every LATER regular-season game between
two FBS teams from information available at the cut:

  quality  the team's PREVIOUS season final rating (script 10), the best quality reading that exists
           at the cut for a past season
  results  the games of weeks 1-8 only

Models are calibrated leave-one-season-out (margin ~ k * power gap + home edge), so the season being
scored never trains its own calibration. Reports winner accuracy and margin MAE beside two dumb
baselines (the home team always wins; always predict the average margin), the rating alone, variants
that drop the win bonus or the conference level, and a placebo that deals the results parts out to the
wrong teams. Also fits the slope BETA (margin points per rating point) and exits 1 if it drifted from
the constant in utils/results_rating.py by more than 10%.
Writes data/computed/ranking_validation.json.

Usage:
    python scripts/validate_rankings.py
"""

import json
import statistics as st
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import results_rating as rr  # noqa: E402
from utils.store import COMPUTED_DIR, read_computed, read_raw  # noqa: E402

SEASONS = [s for s in range(2016, 2026) if s != 2020]


def load():
    """Games, previous-season ratings and season conferences, as plain lookups."""
    games = read_raw("games")
    tr = read_computed("team_ratings")
    tr = tr[tr["engine"].fillna("edge") != "projected"] if "engine" in tr.columns else tr
    rating = {(int(r.team_id), int(r.season)): float(r.overall_rating) for r in tr.itertuples() if r.overall_rating == r.overall_rating}
    recs = read_raw("team_records")
    conf = {(int(r.team_id), int(r.season)): r.conference for r in recs.dropna(subset=["team_id", "conference"]).itertuples()}
    return games, rating, conf, set(int(t) for t in tr["team_id"].unique())


def power_gaps(games, rating, conf, fbs, **kw):
    """Per later game: the home-minus-away power gap (and a placebo gap), home flag, margin, season."""
    rows = []
    for season in SEASONS:
        g = games[(games["season"] == season) & (games["season_type"] == "regular")]
        g = g[g["home_score"].notna() & g["away_score"].notna() & g["home_team_id"].notna() & g["away_team_id"].notna()].to_dict("records")
        g = [x for x in g if int(x["home_team_id"]) in fbs and int(x["away_team_id"]) in fbs]
        wk = lambda x, d: x["week"] if x["week"] == x["week"] else d   # noqa: E731
        early = [x for x in g if wk(x, 99) <= 8]
        late = [x for x in g if wk(x, 0) > 8]
        quality = {t: rating[(t, season - 1)] for t in fbs if (t, season - 1) in rating}
        if len(early) < 200 or len(late) < 100 or not quality:
            continue
        conf_of = lambda t, s=season: conf.get((int(t), s))   # noqa: E731
        parts, _ = rr.power_components(early, quality, conf_of, **kw)
        fixed = {t: p["rating_part"] + p["conf_part"] for t, p in parts.items()}
        # placebo: keep the rating and conference parts, deal the results parts to the wrong teams
        rng = np.random.default_rng(season)
        shuf = dict(zip(parts.keys(), rng.permutation([p["results_part"] for p in parts.values()])))
        for x in late:
            h, a = int(x["home_team_id"]), int(x["away_team_id"])
            if h not in parts or a not in parts:
                continue
            rows.append({"season": season, "p": parts[h]["power"] - parts[a]["power"],
                         "q": parts[h]["rating_part"] - parts[a]["rating_part"],
                         "x": (fixed[h] + shuf[h]) - (fixed[a] + shuf[a]),
                         "home": 0.0 if x.get("neutral_site") == True else 1.0,   # noqa: E712
                         "y": float(x["home_score"]) - float(x["away_score"])})
    return rows


def score(rows, col):
    """Leave-one-season-out score of margin ~ k * rows[col] + home edge: (winner accuracy, margin MAE, slope on all)."""
    hit = n = 0
    mae = []
    for season in sorted({r["season"] for r in rows}):
        X = np.array([[r[col], r["home"]] for r in rows if r["season"] != season])
        y = np.array([r["y"] for r in rows if r["season"] != season])
        w = np.linalg.lstsq(X, y, rcond=None)[0]
        for r in (r for r in rows if r["season"] == season):
            pred = w[0] * r[col] + w[1] * r["home"]
            mae.append(abs(pred - r["y"]))
            if r["y"] != 0:
                n += 1
                hit += (pred > 0) == (r["y"] > 0)
    w = np.linalg.lstsq(np.array([[r[col], r["home"]] for r in rows]), np.array([r["y"] for r in rows]), rcond=None)[0]
    return hit / n, st.mean(mae), float(w[0])


def main() -> int:
    """Run the backtest, print the table, write the JSON; return 1 if the rating slope has drifted from BETA."""
    games, rating, conf, fbs = load()
    base = power_gaps(games, rating, conf, fbs)
    nz = [r for r in base if r["y"] != 0]
    mu = st.mean(r["y"] for r in base)
    out = {"games": len(base), "seasons": sorted({r["season"] for r in base}),
           "home_always_wins": sum(r["y"] > 0 for r in nz) / len(nz),
           "average_margin_mae": st.mean(abs(r["y"] - mu) for r in base), "models": {}}
    print(f"{len(base):,} scored games, seasons {out['seasons']}")
    print(f"  home team always wins                 winner {out['home_always_wins']:.3f}")
    print(f"  always the average margin             MAE {out['average_margin_mae']:.2f}")

    def show(label, rows, col):
        acc, mae, k = score(rows, col)
        out["models"][label] = {"winner": acc, "mae": mae, "slope": k}
        print(f"  {label:40s} winner {acc:.3f}  MAE {mae:.2f}  slope {k:.3f}")
        return k

    beta_fit = show("rating only (last season)", base, "q")
    show("POWER: rating + conference + results", base, "p")
    show("  placebo: results dealt to wrong teams", base, "x")
    show("  without the win bonus", power_gaps(games, rating, conf, fbs, bonus=0.0), "p")
    show("  without the conference level", power_gaps(games, rating, conf, fbs, lam_conf=1e9), "p")
    (COMPUTED_DIR / "ranking_validation.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    drift = abs(beta_fit - rr.BETA) / rr.BETA
    print(f"\nrating slope fitted {beta_fit:.3f} vs constant BETA {rr.BETA}: drift {drift:.1%}")
    return 1 if drift > 0.10 else 0


if __name__ == "__main__":
    sys.exit(main())
