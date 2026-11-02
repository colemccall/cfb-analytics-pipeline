"""How often does a ranking put a team above one that beat it? Read-only; descriptive.

For a season (default 2026, through the latest weekly snapshot) and each ranking method the Rankings screen offers
(games only, rating + games, rating), counts the FBS-vs-FBS results in which the LOSER is ranked above the WINNER, and the
subset a reader finds hard to defend: the loser has the same record or a worse one (at least as many losses as the winner).

Also reports how close the ranks are in those pairs (the difference in the sorted value), so a "tie" can be told from a real
disagreement, and a placebo that deals the ranking to other teams (it shows what a ranking with no information would do).

    python scripts/report_head_to_head.py --season 2026
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys_path = str(ROOT)
import sys  # noqa: E402

sys.path.insert(0, sys_path)
from utils.store import fill_shutouts  # noqa: E402

METHODS = (("power", "Games only"), ("blend", "Rating + games"), ("head", "Rating (headline)"))


def load(season: int):
    """The latest weekly row of every team, and the season's decided FBS-vs-FBS regular-season games through it."""
    wt = json.loads((ROOT / "data" / "computed" / f"weekly_teams_{season}.json").read_text(encoding="utf-8"))
    last = max(wt["weeks"])
    rows = {int(t): next(r for r in v["weeks"] if r["week"] == last) for t, v in wt["teams"].items()}
    names = {int(t): v["school"] for t, v in wt["teams"].items()}
    g = fill_shutouts(pd.DataFrame(json.loads((ROOT / "data" / "raw" / "games.json").read_text(encoding="utf-8"))))
    g = g[(g["season"] == season) & (g["season_type"] == "regular") & (g["week"] <= last) & g["home_score"].notna() & g["away_score"].notna()
          & g["home_team_id"].isin(rows) & g["away_team_id"].isin(rows) & (g["home_score"] != g["away_score"])]
    return last, rows, names, g


def results(g: pd.DataFrame):
    """(winner, loser, margin) of every game."""
    out = []
    for r in g.itertuples():
        hw = r.home_score > r.away_score
        out.append((int(r.home_team_id if hw else r.away_team_id), int(r.away_team_id if hw else r.home_team_id), abs(r.home_score - r.away_score)))
    return out


def inversions(rows: dict, games: list, key: str, top: int | None):
    """Counts of games whose loser outranks the winner by `key`, among teams ranked in the top `top` (None = all)."""
    vals = {t: r[key] for t, r in rows.items() if r.get(key) is not None}
    order = sorted(vals, key=lambda t: -vals[t])
    rank = {t: i + 1 for i, t in enumerate(order)}
    n = inv = hard = 0
    gaps = []
    for w, l, m in games:
        if w not in rank or l not in rank:
            continue
        if top is not None and (rank[w] > top or rank[l] > top):
            continue
        n += 1
        if rank[l] < rank[w]:
            inv += 1
            if rows[l]["l"] >= rows[w]["l"]:
                hard += 1
                gaps.append(vals[l] - vals[w])
    return n, inv, hard, gaps, rank, vals


def main() -> None:
    """Print the inversion table and the hardest pairs for each ranking method."""
    ap = argparse.ArgumentParser(description="Head-to-head inversions in the rankings")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--examples", type=int, default=12)
    args = ap.parse_args()
    last, rows, names, g = load(args.season)
    games = results(g)
    print(f"{args.season} through week {last}: {len(games)} decided games between {len(rows)} FBS teams\n")
    print("  results where the LOSER is ranked above the WINNER (a), and where its record is also no better (b: losses >= the winner's)")
    print(f"  {'method':<20}{'among':>9}{'games':>7}{'(a)':>6}{'%':>6}{'(b)':>6}{'%':>6}   median value gap in (b)")
    for key, label in METHODS:
        for top, lbl in ((25, "top 25"), (40, "top 40"), (None, "all 138")):
            n, inv, hard, gaps, _, vals = inversions(rows, games, key, top)
            sd = np.std(list(vals.values()))
            med = f"{np.median(np.abs(gaps)):.2f} ({np.median(np.abs(gaps)) / sd:.2f} sd)" if gaps else "-"
            print(f"  {label if lbl == 'top 25' else '':<20}{lbl:>9}{n:>7}{inv:>6}{inv / max(n, 1):>6.0%}{hard:>6}{hard / max(n, 1):>6.0%}   {med}")
    # placebo: deal the power values to other teams (breaks which team is good, keeps the spread)
    rng = np.random.default_rng(1)
    ids = [t for t in rows if rows[t].get("power") is not None]
    dealt = {t: dict(rows[t], power=rows[s]["power"]) for t, s in zip(ids, rng.permutation(ids))}
    n, inv, hard, *_ = inversions(dealt, games, "power", None)
    print(f"\n  placebo (power dealt to other teams): (a) {inv / n:.0%}, (b) {hard / n:.0%} of {n} games: the floor of a ranking with no information")

    key = "power"
    n, inv, hard, gaps, rank, vals = inversions(rows, games, key, 40)
    print(f"\n  The hardest pairs by games-only power in the top 40: loser ranked above the winner, record no better ({hard})")
    shown = []
    for w, l, m in games:
        if w in rank and l in rank and rank[w] <= 40 and rank[l] <= 40 and rank[l] < rank[w] and rows[l]["l"] >= rows[w]["l"]:
            shown.append((rank[w] - rank[l], w, l, m))
    for gap, w, l, m in sorted(shown, reverse=True)[: args.examples]:
        print(f"    #{rank[l]:<3}{names[l]:<20}{rows[l]['w']}-{rows[l]['l']}  lost to  #{rank[w]:<3}{names[w]:<20}{rows[w]['w']}-{rows[w]['l']}  by {m:.0f}   (power {vals[l]:+.1f} vs {vals[w]:+.1f})")


if __name__ == "__main__":
    main()
