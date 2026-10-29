"""Does our own opponent strength (utils/opponent_strength.py) separate teams, and what settings are best?

Two tests on 2016-2025 (no 2020), scored the way scripts/validate_rankings.py scores power:

  A. EARLY CUT. Solve strengths from weeks 1-8 only and predict every LATER regular-season FBS game
     (margin ~ k x strength gap + home edge, calibrated leave-one-season-out). Winner accuracy and margin MAE
     beside the dumb baselines. Varies the team ridge, the conference ridge (1e9 = no conference level) and the
     weight on cross-conference games.
  B. NEXT SEASON. Solve a season's strengths from its regular season, or from regular season plus postseason at
     weight POST_WEIGHT, and predict every regular-season game of the NEXT season. This is where the postseason
     weight is chosen: it shows whether bowls and the playoff add information about the following year.

Usage:
    python scripts/validate_opponent_strength.py
"""

import itertools
import statistics as st
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import opponent_strength as os_  # noqa: E402
from utils.store import read_raw  # noqa: E402

SEASONS = [s for s in range(2016, 2026) if s != 2020]


def load():
    """Games as dicts per season, the FBS team set per season and conference lookups."""
    games = read_raw("games")
    recs = read_raw("team_records").dropna(subset=["team_id", "conference"])
    conf = {(int(r.team_id), int(r.season)): r.conference for r in recs.itertuples()}
    g = games[games["home_score"].notna() & games["away_score"].notna()]
    by = {s: g[g["season"] == s].to_dict("records") for s in set(g["season"].astype(int))}
    return by, conf


def fbs_pair(x, conf, season):
    """(home, away) ids when both are FBS teams of ours in `season`, else None."""
    h, a = x.get("home_team_id"), x.get("away_team_id")
    if h is None or a is None or h != h or a != a:
        return None
    h, a = int(h), int(a)
    return (h, a) if (h, season) in conf and (a, season) in conf else None


def score(rows):
    """Leave-one-season-out score of margin ~ k x gap + home edge: (winner accuracy, margin MAE, slope)."""
    hit = n = 0
    mae = []
    for season in sorted({r["season"] for r in rows}):
        X = np.array([[r["gap"], r["home"]] for r in rows if r["season"] != season])
        y = np.array([r["y"] for r in rows if r["season"] != season])
        w = np.linalg.lstsq(X, y, rcond=None)[0]
        for r in (r for r in rows if r["season"] == season):
            pred = w[0] * r["gap"] + w[1] * r["home"]
            mae.append(abs(pred - r["y"]))
            if r["y"] != 0:
                n += 1
                hit += (pred > 0) == (r["y"] > 0)
    w = np.linalg.lstsq(np.array([[r["gap"], r["home"]] for r in rows]), np.array([r["y"] for r in rows]), rcond=None)[0]
    return hit / n, st.mean(mae), float(w[0])


def early_cut_rows(by, conf, **kw):
    """Per later game: strength gap from weeks 1-8, home flag, margin, season."""
    rows = []
    for season in SEASONS:
        g = [x for x in by.get(season, []) if x["season_type"] == "regular"]
        wk = lambda x, d: x["week"] if x["week"] == x["week"] else d   # noqa: E731
        early = [x for x in g if wk(x, 99) <= 8]
        late = [x for x in g if wk(x, 0) > 8]
        conf_of = lambda t, s=season: conf.get((int(t), s)) if t != os_.FCS else None   # noqa: E731
        ids = [t for (t, s) in conf if s == season]
        out = os_.solve(early, conf_of, ids, **kw)
        for x in late:
            p = fbs_pair(x, conf, season)
            if not p:
                continue
            gap = os_.strength_gap(out, *p)
            rows.append({"season": season, "gap": gap, "home": 0.0 if x.get("neutral_site") == True else 1.0,   # noqa: E712
                         "y": float(x["home_score"]) - float(x["away_score"])})
    return rows


def next_season_rows(by, conf, post_weight):
    """Per game of season s+1: strength gap from season s (regular season, plus postseason at post_weight)."""
    rows = []
    for season in [s for s in SEASONS if s + 1 in SEASONS]:
        g = [x for x in by.get(season, []) if x["season_type"] == "regular" or post_weight > 0]
        conf_of = lambda t, s=season: conf.get((int(t), s)) if t != os_.FCS else None   # noqa: E731
        ids = [t for (t, s) in conf if s == season]
        out = os_.solve(g, conf_of, ids, post_weight=post_weight)
        for x in by.get(season + 1, []):
            if x["season_type"] != "regular":
                continue
            p = fbs_pair(x, conf, season + 1)
            if not p:
                continue
            gap = os_.strength_gap(out, *p)
            if gap is None:
                continue
            rows.append({"season": season, "gap": gap, "home": 0.0 if x.get("neutral_site") == True else 1.0,   # noqa: E712
                         "y": float(x["home_score"]) - float(x["away_score"])})
    return rows


def main() -> int:
    """Print both tables."""
    by, conf = load()
    print("A. Weeks 1-8 strengths -> every later regular-season game (winner %, margin MAE)")
    best = None
    for lt, lc, xw in itertools.product([1.0, 3.0, 10.0], [0.1, 0.5, 3.0, 1e9], [1.0, 1.5, 2.5]):
        rows = early_cut_rows(by, conf, lam_team=lt, lam_conf=lc, cross_weight=xw)
        acc, mae, k = score(rows)
        tag = "(no conference level)" if lc == 1e9 else ""
        print(f"  team ridge {lt:4.1f}  conference ridge {lc:7.1f}  cross weight {xw:.1f}   winner {acc:.3f}  MAE {mae:.2f}  slope {k:.2f} {tag}")
        if best is None or acc > best[0]:
            best = (acc, lt, lc, xw)
    print(f"  best: winner {best[0]:.3f} at team ridge {best[1]}, conference ridge {best[2]}, cross weight {best[3]}")
    print("\nB. Season s strengths -> every regular-season game of s+1")
    for pw in (0.0, 0.5, 1.0, 2.0):
        acc, mae, k = score(next_season_rows(by, conf, pw))
        print(f"  postseason weight {pw:.1f}   winner {acc:.3f}  MAE {mae:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
