"""Can a ranking respect head-to-head results without predicting worse? Read-only; writes nothing.

The Rankings screen puts teams above ones that beat them (scripts/report_head_to_head.py: in 2026, 11 of 19 games among
the top 25). Two remedies are scored here on 2016-2025, the way scripts/validate_rankings.py scores power: fit on the games
through week 8 (and 10), then score every later FBS-vs-FBS game.

  A  the weights of a result: games-only power (ratings all equal, as while a season is played) with the margin cap and the
     win/loss bonus varied. A bigger bonus and a smaller cap make a win count for more and a blowout for less.
  B  a head-to-head tie-break: the numbers stay, but teams whose power is within EPS points per game of the next team down
     are ordered among themselves by their results against each other (wins minus losses within the group), then by power.

Per variant: how often the loser outranks the winner among the games already played at the cut (all FBS, and both teams in the
top 25), how often that loser also has an equal or worse record, and the share of LATER games whose winner the ranking picks
(by power with the home edge, and by rank order alone, which is the like-for-like pick for a tie-break).

    python scripts/validate_head_to_head.py
"""

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils.results_rating import power_components  # noqa: E402

SEASONS = list(range(2016, 2026))
CUTS = (8, 10)
GRID = [(28, 8), (28, 16), (28, 24), (21, 16), (14, 16), (14, 24), (10, 24)]     # (cap, bonus); (28, 8) is production
EPS = (1.0, 2.0, 3.0, 5.0)


def load():
    """FBS-vs-FBS decided regular-season games per season, and the FBS team ids."""
    teams = json.loads((ROOT / "data" / "raw" / "teams.json").read_text(encoding="utf-8"))
    fbs = {int(t["id"]) for t in teams}
    games = json.loads((ROOT / "data" / "raw" / "games.json").read_text(encoding="utf-8"))
    by = {s: [] for s in SEASONS}
    for g in games:
        s = g.get("season")
        if s not in by or g.get("season_type") != "regular" or g.get("week") is None:
            continue
        h, a, hs, as_ = g.get("home_team_id"), g.get("away_team_id"), g.get("home_score"), g.get("away_score")
        if None in (h, a) or hs is None and as_ is None:
            continue
        hs, as_ = hs or 0, as_ or 0                       # the shutout rule: one missing score is a 0
        if int(h) in fbs and int(a) in fbs and hs != as_:
            by[s].append({"home_team_id": int(h), "away_team_id": int(a), "home_score": hs, "away_score": as_,
                          "week": int(g["week"]), "neutral_site": bool(g.get("neutral_site"))})
    return fbs, by


def fit(games, fbs, cap, bonus):
    """Games-only power (equal ratings) of every team with a game, plus the home edge."""
    teams, info = power_components(games, {t: 0.0 for t in fbs}, lambda t: None, cap=cap, bonus=bonus)
    return {t: p for t, p in teams.items() if p["games"] > 0}, info["home_edge"]


def tiebreak_order(teams, games, eps):
    """Rank order (best first) of teams by power, with each run of teams within `eps` of the run's FIRST team ordered by their
    results against each other (a run is anchored at its first team, so a chain of near-ties cannot grow without limit)."""
    order = sorted(teams, key=lambda t: -teams[t]["power"])
    out, i = [], 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and teams[order[i]]["power"] - teams[order[j + 1]]["power"] < eps:
            j += 1
        group = order[i:j + 1]
        if len(group) > 1:
            s = {t: 0 for t in group}
            for g in games:
                h, a = g["home_team_id"], g["away_team_id"]
                if h in s and a in s:
                    w, l = (h, a) if g["home_score"] > g["away_score"] else (a, h)
                    s[w] += 1
                    s[l] -= 1
            group.sort(key=lambda t: (-s[t], -teams[t]["power"]))
        out.extend(group)
        i = j + 1
    return out


def score(rank, teams, played, later, pick):
    """(inversion share, hard-inversion share, top-25 inversion share, pick accuracy) for one ranking."""
    n = inv = hard = n25 = inv25 = 0
    for g in played:
        h, a = g["home_team_id"], g["away_team_id"]
        if h not in rank or a not in rank:
            continue
        w, l = (h, a) if g["home_score"] > g["away_score"] else (a, h)
        n += 1
        bad = rank[l] < rank[w]
        inv += bad
        hard += bad and teams[l]["losses"] >= teams[w]["losses"]
        if rank[w] <= 25 and rank[l] <= 25:
            n25 += 1
            inv25 += bad
    hit = tot = 0
    for g in later:
        h, a = g["home_team_id"], g["away_team_id"]
        if h not in rank or a not in rank:
            continue
        tot += 1
        hit += pick(g) == (g["home_score"] > g["away_score"])
    return inv / n, hard / n, (inv25 / n25 if n25 else float("nan")), hit / tot, n25


def main() -> None:
    """Run every variant over every season and cut; print one pooled line per variant."""
    fbs, by = load()
    print("seasons 2016-2025, cuts after weeks 8 and 10; pooled over the 20 season-cuts (production: cap 28, bonus 8)\n")
    print(f"{'variant':<34}{'loser above winner':>20}{'+ record no better':>20}{'top-25 pairs':>14}{'picks (power+edge)':>18}{'picks (rank only)':>16}")
    rows = {}
    for cap, bonus in GRID:
        acc = []
        for s in SEASONS:
            for cut in CUTS:
                played = [g for g in by[s] if g["week"] <= cut]
                later = [g for g in by[s] if g["week"] > cut]
                teams, edge = fit(played, fbs, cap, bonus)
                rank = {t: i + 1 for i, t in enumerate(sorted(teams, key=lambda t: -teams[t]["power"]))}
                pick = lambda g: teams[g["home_team_id"]]["power"] + (0 if g["neutral_site"] else edge) > teams[g["away_team_id"]]["power"]   # noqa: E731
                by_rank = lambda g: rank[g["home_team_id"]] < rank[g["away_team_id"]]       # noqa: E731
                acc.append(score(rank, teams, played, later, pick) + (score(rank, teams, played, later, by_rank)[3],))
        rows[("A", cap, bonus)] = np.nanmean(acc, axis=0)
        r = rows[("A", cap, bonus)]
        tag = " (production)" if (cap, bonus) == (28, 8) else ""
        print(f"A  cap {cap:<3} bonus {bonus:<3}{tag:<14}{r[0]:>19.1%}{r[1]:>20.1%}{r[2]:>14.1%}{r[3]:>18.1%}{r[5]:>16.1%}")
    for eps in EPS:
        acc = []
        for s in SEASONS:
            for cut in CUTS:
                played = [g for g in by[s] if g["week"] <= cut]
                later = [g for g in by[s] if g["week"] > cut]
                teams, edge = fit(played, fbs, 28, 8)
                order = tiebreak_order(teams, played, eps)
                rank = {t: i + 1 for i, t in enumerate(order)}
                pick = lambda g: rank[g["home_team_id"]] < rank[g["away_team_id"]]       # noqa: E731
                acc.append(score(rank, teams, played, later, pick) + (score(rank, teams, played, later, pick)[3],))
        r = np.nanmean(acc, axis=0)
        print(f"B  tie-break within {eps:<3} pts/game{'':<8}{r[0]:>19.1%}{r[1]:>20.1%}{r[2]:>14.1%}{'':>18}{r[5]:>16.1%}")
    base = rows[("A", 28, 8)]
    print(f"\nreference: always pick the home team / a ranking with no information is about 50-58% / 50%; production picks {base[3]:.1%}.")


if __name__ == "__main__":
    main()
