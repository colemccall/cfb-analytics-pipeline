"""What moved between two runs of the live season: the biggest player and team moves, and whether past weeks held still.

Read-only. Compares live_{season}.json and the two weekly files in a BEFORE folder (a copy of data/computed taken
before a refresh) against the same files in an AFTER folder (default: data/computed):

    python scripts/report_rating_moves.py --season 2026 --before ../_snapshots/v4.24-pre/computed

Prints, descriptively — one week is one observation, so nothing here is a trend:

  players   the largest risers and fallers in the season-to-date rating. A player who did NOT play in the new games
            but still moved did so because his team played without him: the in-season scale reads his share of his
            team's games (the availability effect), not his play. Those are listed apart. Players on 1-2 games are
            tagged "low-game" (their rating is a recruiting prior plus a small shrunk game term).
  teams     the largest moves in the headline rating, with records and rank change.
  weeks     for every week both runs hold, how many players and teams changed. A week-N snapshot only knows weeks
            1..N, so a week that was already complete before the refresh should not move at all; any movement there
            is either new data for that week (a late score or box score) or a leak of later information.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
EPS = 0.05          # rating points; below this a difference is rounding
LOW_GAME_MAX = 2    # script 07's low-game rule (players on 1-2 productive games)


def _read(folder: Path, name: str) -> dict:
    return json.loads((folder / name).read_text(encoding="utf-8"))


def _team_games(live: dict) -> dict:
    return {t["team_id"]: (t["wins"] or 0) + (t["losses"] or 0) for t in live["teams"]}


def player_moves(before: dict, after: dict, top: int) -> None:
    """Print the player movement summary: counts, then the largest risers and fallers, split by whether the player played in the new games."""
    tg_b, tg_a = _team_games(before), _team_games(after)
    b = {p["player_id"]: p for p in before["players"]}
    a = {p["player_id"]: p for p in after["players"]}
    both = sorted(set(a) & set(b))
    print(f"\nPLAYERS  rated before {len(b):,}, after {len(a):,}; in both {len(both):,}; "
          f"new {len(set(a) - set(b)):,}; no longer rated {len(set(b) - set(a)):,}")
    rows = []
    for pid in both:
        pa, pb = a[pid], b[pid]
        d = pa["overall_rating"] - pb["overall_rating"]
        played = (pa["games_played"] or 0) > (pb["games_played"] or 0)
        rows.append((d, pa, pb, played))
    moved = [r for r in rows if abs(r[0]) >= EPS]
    print(f"  moved by {EPS}+: {len(moved):,} of {len(both):,}; mean |move| {sum(abs(r[0]) for r in moved) / max(len(moved), 1):.2f}")
    played_moves = [r for r in moved if r[3]]
    idle_moves = [r for r in moved if not r[3]]
    print(f"  of those, played in the new games: {len(played_moves):,}; did not play (availability effect): {len(idle_moves):,}")

    def line(r):
        d, pa, pb, _ = r
        tb, ta = tg_b.get(pb["team_id"]), tg_a.get(pa["team_id"])
        tag = " low-game" if (pa["games_played"] or 0) <= LOW_GAME_MAX else ""
        return (f"    {d:+6.1f}  {pb['overall_rating']:5.1f} -> {pa['overall_rating']:5.1f}  {pa['name']:<24} {pa['position_group']:<4} "
                f"{(pa['team'] or ''):<20} games {pb['games_played']}/{tb} -> {pa['games_played']}/{ta}{tag}")

    for title, pool, key in (("risers (played)", played_moves, lambda r: -r[0]), ("fallers (played)", played_moves, lambda r: r[0]),
                             ("risers (did not play)", idle_moves, lambda r: -r[0]), ("fallers (did not play)", idle_moves, lambda r: r[0])):
        print(f"  {title}:")
        for r in sorted(pool, key=key)[:top]:
            print(line(r))


def team_moves(before: dict, after: dict, top: int) -> None:
    """Print the largest headline-rating moves among teams, with records and rank change."""
    def ranked(live):
        ts = sorted(live["teams"], key=lambda t: -(t["overall_rating"] or -1e9))
        return {t["team_id"]: (i + 1, t) for i, t in enumerate(ts)}
    b, a = ranked(before), ranked(after)
    rows = []
    for tid in set(a) & set(b):
        (ra, ta), (rb, tb) = a[tid], b[tid]
        if ta["overall_rating"] is None or tb["overall_rating"] is None:
            continue
        rows.append((ta["overall_rating"] - tb["overall_rating"], ta, tb, ra, rb))
    print(f"\nTEAMS  {len(rows)} compared; headline moved by {EPS}+: {sum(1 for r in rows if abs(r[0]) >= EPS)}")
    for title, key in (("risers", lambda r: -r[0]), ("fallers", lambda r: r[0])):
        print(f"  {title}:")
        for d, ta, tb, ra, rb in sorted(rows, key=key)[:top]:
            print(f"    {d:+5.1f}  {tb['overall_rating']:5.1f} -> {ta['overall_rating']:5.1f}  {ta['school']:<22} "
                  f"{tb['wins']}-{tb['losses']} -> {ta['wins']}-{ta['losses']}  rank {rb} -> {ra}")


def week_stability(before_dir: Path, after_dir: Path, season: int) -> None:
    """Print, for every week both runs hold, how many players and teams changed and by how much at most."""
    pb = _read(before_dir, f"weekly_players_{season}.json")
    pa = _read(after_dir, f"weekly_players_{season}.json")
    tb = _read(before_dir, f"weekly_teams_{season}.json")
    ta = _read(after_dir, f"weekly_teams_{season}.json")
    print(f"\nWEEKS  before: weeks {pb['weeks']}; after: weeks {pa['weeks']}")
    print("  week | players in both | moved | largest | teams in both | moved | largest")
    for wk in sorted(set(pb["weeks"]) & set(pa["weeks"])):
        def at(doc):
            return {pid: next((r for r in rows if r[0] == wk), None) for pid, rows in doc["players"].items()}
        x, y = at(pb), at(pa)
        common = [k for k in x if x[k] and y.get(k)]
        dp = [abs(x[k][1] - y[k][1]) for k in common]
        mp = [d for d in dp if d >= EPS]

        def tat(doc):
            return {tid: next((r for r in v["weeks"] if r["week"] == wk), None) for tid, v in doc["teams"].items()}
        u, v = tat(tb), tat(ta)
        tc = [k for k in u if u[k] and v.get(k) and u[k]["head"] is not None and v[k]["head"] is not None]
        dt = [abs(u[k]["head"] - v[k]["head"]) for k in tc]
        mt = [d for d in dt if d >= EPS]
        print(f"  {wk:>4} | {len(common):>15,} | {len(mp):>5,} | {max(dp, default=0):7.2f} | {len(tc):>13} | {len(mt):>5} | {max(dt, default=0):7.2f}")


def main() -> None:
    """Print the player, team and week-stability comparisons between two runs."""
    ap = argparse.ArgumentParser(description="What moved between two runs of the live season")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--before", type=Path, required=True, help="folder with the earlier live and weekly files")
    ap.add_argument("--after", type=Path, default=ROOT / "data" / "computed")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    before, after = _read(args.before, f"live_{args.season}.json"), _read(args.after, f"live_{args.season}.json")
    print(f"{args.season}: before through week {before['through_week']} ({before['games_completed']:,} games), "
          f"after through week {after['through_week']} ({after['games_completed']:,} games)")
    player_moves(before, after, args.top)
    team_moves(before, after, max(args.top * 2 // 3, 10))
    week_stability(args.before, args.after, args.season)


if __name__ == "__main__":
    main()
