"""Write out every head-to-head inversion in the 2025 and 2026 rankings, for the owner to read. Read-only; ranks nothing new.

An inversion is a decided FBS-vs-FBS regular-season game whose LOSER is ranked above its WINNER. Each is listed with both
teams' rank, record, the score and week, and how far apart the two ranking values are (a tiny gap is a near-tie). The pairs
whose loser also has an equal or worse record are marked HARD: those are the ones a reader finds hard to defend.

  2026  through the latest weekly snapshot, by the three methods the Rankings screen offers: games only (power), rating + games
        (blend) and the rating (headline), exactly as the screen shows them.
  2025  the published final: power (rating part plus results part) and the team rating, from team_ratings.json.

Nothing is changed: no ranking, constant or file the site reads. Output is a markdown document.

    python scripts/list_head_to_head_inversions.py --out docs/H2H_INVERSIONS_2025_2026.md
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.store import fill_shutouts  # noqa: E402


def games_of(season: int, ids: set, last_week: int | None = None) -> pd.DataFrame:
    """Decided regular-season games between two of `ids`, through `last_week` when given."""
    g = fill_shutouts(pd.DataFrame(json.loads((ROOT / "data" / "raw" / "games.json").read_text(encoding="utf-8"))))
    g = g[(g["season"] == season) & (g["season_type"] == "regular") & g["home_score"].notna() & g["away_score"].notna()
          & g["home_team_id"].isin(ids) & g["away_team_id"].isin(ids) & (g["home_score"] != g["away_score"])]
    return g if last_week is None else g[g["week"] <= last_week]


def records(season: int, ids: set, last_week: int | None) -> dict:
    """{team: (wins, losses)} over every regular-season game with a result, FCS opponents included."""
    g = fill_shutouts(pd.DataFrame(json.loads((ROOT / "data" / "raw" / "games.json").read_text(encoding="utf-8"))))
    g = g[(g["season"] == season) & (g["season_type"] == "regular") & g["home_score"].notna() & g["away_score"].notna()]
    if last_week is not None:
        g = g[g["week"] <= last_week]
    rec = {t: [0, 0] for t in ids}
    for r in g.itertuples():
        hw = r.home_score > r.away_score
        if r.home_score == r.away_score:
            continue
        for t, won in ((r.home_team_id, hw), (r.away_team_id, not hw)):
            if pd.notna(t) and int(t) in rec:
                rec[int(t)][0 if won else 1] += 1
    return {t: tuple(v) for t, v in rec.items()}


def section(title: str, vals: dict, rec: dict, games: pd.DataFrame, names: dict, unit: str) -> list:
    """Markdown lines listing every inversion of one ranking."""
    vals = {t: v for t, v in vals.items() if v is not None}
    order = sorted(vals, key=lambda t: -vals[t])
    rank = {t: i + 1 for i, t in enumerate(order)}
    sd = pd.Series(list(vals.values())).std()
    found = []
    for r in games.itertuples():
        hw = r.home_score > r.away_score
        w, l = int(r.home_team_id if hw else r.away_team_id), int(r.away_team_id if hw else r.home_team_id)
        if w in rank and l in rank and rank[l] < rank[w]:
            hard = rec[l][1] >= rec[w][1]
            found.append((rank[l], rank[w], l, w, int(r.week), abs(r.home_score - r.away_score), vals[l] - vals[w], hard, r.neutral_site))
    found.sort()
    hard_n = sum(1 for f in found if f[7])
    out = [f"### {title}", "",
           f"{len(found)} of {len(games)} games have the loser ranked above the winner; {hard_n} of those also have the loser at an equal or worse record (HARD). "
           f"Value gap is in {unit}; one standard deviation of the ranking value is {sd:.1f}.", "",
           "| Loser | Rec | Beaten by | Rec | Wk | Margin | Gap | Hard | Gap in sd |", "|---|---|---|---|---|---|---|---|---|"]
    for rl, rw, l, w, wk, m, gap, hard, neutral in found:
        out.append(f"| #{rl} {names[l]} | {rec[l][0]}-{rec[l][1]} | #{rw} {names[w]} | {rec[w][0]}-{rec[w][1]} | {wk} | {m:.0f}{' (N)' if neutral else ''} | {gap:.1f} | "
                   f"{'HARD' if hard else ''} | {gap / sd:.2f} |")
    out.append("")
    return out


def main() -> None:
    """Build the 2026 and 2025 sections and write the document."""
    ap = argparse.ArgumentParser(description="List every head-to-head inversion for 2025 and 2026")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    teams = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "teams.json").read_text(encoding="utf-8")))
    names = dict(zip(teams["id"].astype(int), teams["school"]))
    lines = ["# Head-to-head inversions, 2025 and 2026", "",
             "Every decided FBS-vs-FBS regular-season game whose loser is ranked above its winner, by each ranking the site shows. "
             "Listed for reading only (v4.24): no ranking, constant or file the site reads was changed. "
             "HARD = the loser's record is also no better than the winner's. A small gap in standard deviations is a near-tie.", ""]

    wt = json.loads((ROOT / "data" / "computed" / "weekly_teams_2026.json").read_text(encoding="utf-8"))
    last = max(wt["weeks"])
    rows = {int(t): next(r for r in v["weeks"] if r["week"] == last) for t, v in wt["teams"].items()}
    ids = set(rows)
    rec = records(2026, ids, last)
    g26 = games_of(2026, ids, last)
    lines += [f"## 2026, through week {last}", ""]
    for key, title in (("power", "Games only (power)"), ("blend", "Rating + games"), ("head", "Rating (headline)")):
        lines += section(f"{title}", {t: r[key] for t, r in rows.items()}, rec, g26, names, "points per game" if key != "head" else "rating points")

    tr = pd.DataFrame(json.loads((ROOT / "data" / "computed" / "team_ratings.json").read_text(encoding="utf-8")))
    tr = tr[(tr["season"] == 2025) & (tr["engine"] == "edge")]
    ids25 = {int(t) for t in tr["team_id"]}
    rec25 = records(2025, ids25, None)
    g25 = games_of(2025, ids25)
    lines += ["## 2025, the published final (regular season)", ""]
    for col, title, unit in (("power_rating", "Power (rating part plus results part)", "points per game"), ("overall_rating", "Rating (headline)", "rating points")):
        lines += section(title, {int(r.team_id): getattr(r, col) if pd.notna(getattr(r, col)) else None for r in tr.itertuples()}, rec25, g25, names, unit)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.out} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
