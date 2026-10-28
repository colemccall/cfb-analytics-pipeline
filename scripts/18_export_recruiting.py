"""Export the recruiting board: recruiting_{year}.json for the menu's Recruiting screen.

Local only, no API: reads data/raw recruiting, players and teams (already on disk) and
writes cfb-analytics-app/data/recruiting_{year}.json for each class year in
CLASS_YEARS. Each file holds two things:

  prospects  the class's best recruits by national rank: name, position, stars, national
             rank, composite score, hometown state, and the team he committed to
  teams      one row per team with at least one commit: commits, four- and five-star
             counts, average composite, and a class rank

CLASS RANK is deliberately simple and says what it is: teams are ranked by the AVERAGE
COMPOSITE OF THEIR BEST 15 RECRUITS, and a class with fewer than 15 matched commits is
left unranked rather than guessed. It is a transparent summary of the data we hold, not
a reproduction of anyone's published class ranking.

What the data cannot say: only recruits matched to a player in our players table are
listed (about 80% of a class; the rest are unmatched and absent), a recruit who has
not signed shows no team, and a position comes from the players table or, failing that,
the position group of a roster he later appears on (null if neither). A missing composite stays null, never zero.

Usage:
    python scripts/18_export_recruiting.py
"""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw

APP_DATA = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"
CLASS_YEARS = range(2020, 2027)
TOP_N = 15           # recruits that count toward a class rank
PROSPECTS = 400      # best recruits listed per class
PLAYERS_PER_CLASS = 45   # commits listed per team (the state counts cover all of them)


def _num(v):
    """A float, or None for a missing value (never 0)."""
    return None if v is None or pd.isna(v) else float(v)


def _txt(v):
    """A string, or None for a missing value. A pandas NaN left in a row would be
    written as a literal NaN token, which is not JSON and kills the browser's parse."""
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


def build_class(year: int, rec: pd.DataFrame, players: pd.DataFrame, teams: pd.DataFrame,
                roster_pos: dict) -> dict:
    """One class year's prospects and team summaries, as described in the module docstring.

    Joins recruiting to players (name, position, hometown) and to teams (school), drops
    recruits with no matching player, and ranks teams by the average composite of their
    best TOP_N recruits.
    """
    r = rec[rec["recruit_year"] == year].copy()
    pl = players[["id", "name", "position", "hometown_state"]].rename(columns={"id": "player_id"})
    r = r.merge(pl, on="player_id", how="inner")
    # The players table rarely carries a recruit's position; his position GROUP on a
    # roster he later appears on does. Used when the table has none; null otherwise.
    r["position"] = [p if isinstance(p, str) and p else roster_pos.get(int(i)) for p, i in zip(r["position"], r["player_id"])]
    school = dict(zip(teams["id"], teams["school"]))

    r = r.sort_values(["national_rank", "composite_score"], ascending=[True, False], na_position="last")
    prospects = []
    for _, x in r.head(PROSPECTS).iterrows():
        tid = None if pd.isna(x["committed_team_id"]) else int(x["committed_team_id"])
        prospects.append({
            "player_id": int(x["player_id"]), "name": _txt(x["name"]), "position": _txt(x.get("position")),
            "stars": None if pd.isna(x["stars"]) else int(x["stars"]),
            "national_rank": None if pd.isna(x["national_rank"]) else int(x["national_rank"]),
            "composite": _num(x["composite_score"]), "state": _txt(x.get("hometown_state")),
            "team_id": tid, "team": school.get(tid),
        })

    committed = r[r["committed_team_id"].notna()].copy()
    committed["committed_team_id"] = committed["committed_team_id"].astype(int)
    rows = []
    for tid, g in committed.groupby("committed_team_id"):
        best = g["composite_score"].dropna().sort_values(ascending=False).head(TOP_N)
        g = g.sort_values(["composite_score"], ascending=False, na_position="last")
        states = g["hometown_state"].dropna().astype(str).value_counts()
        rows.append({
            "team_id": int(tid), "team": school.get(tid), "commits": int(len(g)),
            # The class itself, best first, and where it came from by state, for the team's
            # recruiting screen and its map. Every matched commit is in `states`; the list is
            # capped so a 40-man class does not bloat the file.
            "states": {str(k): int(v) for k, v in states.items()},
            "players": [{
                "player_id": int(x["player_id"]), "name": _txt(x["name"]), "position": _txt(x.get("position")),
                "stars": None if pd.isna(x["stars"]) else int(x["stars"]),
                "national_rank": None if pd.isna(x["national_rank"]) else int(x["national_rank"]),
                "composite": _num(x["composite_score"]), "state": _txt(x.get("hometown_state")),
            } for _, x in g.head(PLAYERS_PER_CLASS).iterrows()],
            "five_stars": int((g["stars"] == 5).sum()), "four_stars": int((g["stars"] == 4).sum()),
            "avg_composite": round(float(g["composite_score"].mean()), 4) if g["composite_score"].notna().any() else None,
            "top_avg": round(float(best.mean()), 4) if len(best) >= TOP_N else None,
        })
    ranked = sorted([x for x in rows if x["top_avg"] is not None], key=lambda x: -x["top_avg"])
    for i, x in enumerate(ranked):
        x["class_rank"] = i + 1
    for x in rows:
        x.setdefault("class_rank", None)
    rows.sort(key=lambda x: (x["class_rank"] is None, x["class_rank"] or 0, -(x["commits"])))
    return {"year": year, "matched_recruits": int(len(r)), "top_n": TOP_N, "prospects": prospects, "teams": rows}


def main() -> None:
    """Write recruiting_{year}.json for every class year that has recruits on file.

    Prints a one-line summary per class. Years with no recruits are skipped, so adding a
    year to CLASS_YEARS before its class is harvested does nothing harmful.
    """
    rec, players, teams = read_raw("recruiting"), read_raw("players"), read_raw("teams")
    ps = read_raw("player_seasons")
    roster_pos = {}
    if not ps.empty:
        latest = ps.dropna(subset=["player_id", "position_group"]).sort_values("season").drop_duplicates("player_id", keep="last")
        roster_pos = {int(a): b for a, b in zip(latest["player_id"], latest["position_group"])}
    if rec.empty or players.empty:
        sys.exit("No recruiting or players data on disk — run scripts 01 and 02.")
    APP_DATA.mkdir(parents=True, exist_ok=True)
    for year in CLASS_YEARS:
        c = build_class(year, rec, players, teams, roster_pos)
        if not c["matched_recruits"]:
            print(f"{year}: no matched recruits — skipped")
            continue
        (APP_DATA / f"recruiting_{year}.json").write_text(
            json.dumps(c, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        ranked = sum(1 for t in c["teams"] if t["class_rank"] is not None)
        print(f"{year}: {c['matched_recruits']:,} matched recruits, {len(c['teams'])} teams, {ranked} ranked")


if __name__ == "__main__":
    main()
