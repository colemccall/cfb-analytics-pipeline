"""How a finished season looked week by week, and how well each week's number held up. Read-only; descriptive.

Reads the weekly files script 17b wrote for a finished season (weekly_teams_{season}.json, weekly_players_{season}.json:
the rating chain replayed on the games through each week, with today's method) and answers, with numbers beside a naive
baseline each time:

  (1) PLAYERS   how close was a player's rating after week N to his rating after the last replayed week? Spearman and mean
                absolute error in rating points by position family, against carrying forward last season's rating and
                against the position's mean. Only players rated at both weeks, so a player who had not yet played is not in.
  (2) TEAMS     the same for the headline rating: rank agreement with the last replayed week, against wins so far and against
                last season's headline.
  (3) PICKS     the winner of every FBS game in week N+1, picked by the higher power after week N (neutral site: no home edge),
                against always picking the home team and against the team with more wins so far; and a placebo in which the
                powers of one week are dealt to other teams. n is games.
  (4) PATHS     Boise State, and the playoff field of the season, by week: headline rank after weeks 5, 9 and the last, with
                the seed they got.
  (5) AVAILABILITY  the signed gap between a player's rating after week 8 and after the last week, by the share of his
                team's games he had played by week 8: does the in-season scale (per-game level x sqrt(share of games)) leave
                part-time players short, or did they finish where it said? (The Gaines question, v4.24.)

The last replayed week is the "final" here: the same machinery, regular season only. It is NOT the published season rating
(which also counts the line unit, the postseason and a non-in-progress games rule); section (0) compares the two, so the
size of that difference is on the page.

    python scripts/report_replay.py --season 2025
"""

import argparse
import json
import statistics as st
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).parent.parent
FAMILY = {"QB": "QB", "RB": "RB", "WR": "WR/TE", "TE": "WR/TE", "DL": "DL/EDGE", "EDGE": "DL/EDGE",
          "LB": "LB", "CB": "DB", "S": "DB", "DB": "DB", "K": "K/P", "P": "K/P"}
FIRST_WEEK = 5                  # the first week the site's Rankings offers; the tables start there
SHARE_BUCKETS = ((0.5, "<=0.5"), (0.75, "0.5-0.75"), (0.9, "0.75-0.9"), (9.9, ">0.9"))


def _read(name: str):
    return json.loads((ROOT / "data" / "computed" / name).read_text(encoding="utf-8"))


def _rho(a, b) -> float:
    return float(spearmanr(a, b)[0]) if len(a) > 5 else float("nan")


def load(season: int):
    """The weekly players and teams, the position of every player, and last season's final ratings."""
    wp, wt = _read(f"weekly_players_{season}.json"), _read(f"weekly_teams_{season}.json")
    ps = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "player_seasons.json").read_text(encoding="utf-8")))
    pos = dict(zip(ps[ps["season"] == season]["player_id"], ps[ps["season"] == season]["position_group"]))
    ratings = pd.DataFrame(json.loads((ROOT / "data" / "computed" / "ratings.json").read_text(encoding="utf-8")))
    ratings = ratings[ratings["engine"] == "edge"]
    ps_player = dict(zip(ps["id"], ps["player_id"]))
    prev = ratings[ratings["season"] == season - 1].assign(pid=lambda d: d["player_season_id"].map(ps_player))
    prev = dict(zip(prev["pid"], prev["overall_rating"]))
    tr = pd.DataFrame(json.loads((ROOT / "data" / "computed" / "team_ratings.json").read_text(encoding="utf-8")))
    prev_team = tr[(tr["season"] == season - 1) & (tr["engine"] == "edge")].set_index("team_id")["overall_rating"].to_dict()
    final_pub = ratings[ratings["season"] == season].assign(pid=lambda d: d["player_season_id"].map(ps_player))
    return wp, wt, pos, prev, prev_team, dict(zip(final_pub["pid"], final_pub["overall_rating"]))


def at_week(rows, week):
    """The row of a player or team for `week`, or None."""
    return next((r for r in rows if (r[0] if isinstance(r, list) else r["week"]) == week), None)


def players_table(wp, pos, prev, last):
    """(1) week-N rating against the last week's, by position family, with two baselines."""
    print(f"\n(1) PLAYERS: rating after week N against after week {last} (players rated at both)")
    print("    family  week      n   Spearman   MAE   bias(N-final) | carry-forward last season: n  Spearman  MAE | position mean MAE")
    fin = {int(pid): at_week(rows, last) for pid, rows in wp["players"].items()}
    out = []
    for fam in ("QB", "RB", "WR/TE", "DL/EDGE", "LB", "DB"):
        for wk in (FIRST_WEEK, 7, 9, 11, 13):
            if wk >= last:
                continue
            a, b, c = [], [], []
            for pid, rows in wp["players"].items():
                pid = int(pid)
                if FAMILY.get(pos.get(pid)) != fam or not fin.get(pid):
                    continue
                r = at_week(rows, wk)
                if r and r[1] is not None and fin[pid][1] is not None:
                    a.append(r[1]); b.append(fin[pid][1]); c.append(prev.get(pid))
            if len(a) < 30:
                continue
            a, b = np.array(a), np.array(b)
            have = [i for i, x in enumerate(c) if x is not None]
            cf = np.array([c[i] for i in have])
            mean_mae = float(np.mean(np.abs(b - b.mean())))
            row = (fam, wk, len(a), _rho(a, b), float(np.mean(np.abs(a - b))), float(np.mean(a - b)),
                   len(have), _rho(cf, b[have]) if have else float("nan"), float(np.mean(np.abs(cf - b[have]))) if have else float("nan"), mean_mae)
            out.append(row)
            print(f"    {fam:<8}{wk:>4}{row[2]:>7,}{row[3]:>10.3f}{row[4]:>7.2f}{row[5]:>+9.2f}   |{row[6]:>29,}{row[7]:>9.3f}{row[8]:>7.2f} |{row[9]:>8.2f}")
    return out


def teams_table(wt, prev_team, last):
    """(2) week-N headline rank against the last week's, with two baselines."""
    print(f"\n(2) TEAMS: headline after week N against after week {last} (138 FBS teams; Spearman)")
    print("    week   headline   wins so far   last season's headline")
    teams = wt["teams"]
    fin = {tid: at_week(v["weeks"], last) for tid, v in teams.items()}
    rows = []
    for wk in range(FIRST_WEEK, last):
        cur = {tid: at_week(v["weeks"], wk) for tid, v in teams.items()}
        ids = [t for t in teams if cur[t] and fin[t] and cur[t]["head"] is not None and fin[t]["head"] is not None]
        f = [fin[t]["head"] for t in ids]
        h = _rho([cur[t]["head"] for t in ids], f)
        w = _rho([cur[t]["w"] / max(cur[t]["w"] + cur[t]["l"], 1) for t in ids], f)
        pv = [t for t in ids if int(t) in prev_team and prev_team[int(t)] is not None]
        p = _rho([prev_team[int(t)] for t in pv], [fin[t]["head"] for t in pv])
        rows.append((wk, h, w, p))
        print(f"    {wk:>4}{h:>11.3f}{w:>14.3f}{p:>25.3f}")
    return rows


def picks_table(wt, season, last):
    """(3) winner of every FBS game in week N+1, by week-N power, against two baselines and a placebo."""
    g = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "games.json").read_text(encoding="utf-8")))
    g = g[(g["season"] == season) & (g["season_type"] == "regular") & g["home_score"].notna() & g["away_score"].notna()
          & g["home_team_id"].notna() & g["away_team_id"].notna()]
    teams = {int(t): v["weeks"] for t, v in wt["teams"].items()}
    rng = np.random.default_rng(3)
    n = hit = home_hit = wins_hit = plc_hit = tie = 0
    for wk in range(FIRST_WEEK, last):
        cur = {t: at_week(r, wk) for t, r in teams.items()}
        pw = {t: r["power"] for t, r in cur.items() if r and r["power"] is not None}
        dealt = dict(zip(pw, rng.permutation(list(pw.values()))))
        for r in g[g["week"] == wk + 1].itertuples():
            h, a = int(r.home_team_id), int(r.away_team_id)
            if h not in pw or a not in pw or r.home_score == r.away_score:
                continue
            home_won = r.home_score > r.away_score
            n += 1
            hit += (pw[h] > pw[a]) == home_won
            plc_hit += (dealt[h] > dealt[a]) == home_won
            home_hit += home_won
            wh, wa = cur[h]["w"] / max(cur[h]["w"] + cur[h]["l"], 1), cur[a]["w"] / max(cur[a]["w"] + cur[a]["l"], 1)
            if wh == wa:
                tie += 1
                wins_hit += 0.5
            else:
                wins_hit += (wh > wa) == home_won
    print(f"\n(3) PICKS: the winner of every FBS-vs-FBS game in weeks {FIRST_WEEK + 1}-{last}, by the power after the week before ({n:,} games)")
    print(f"    power {hit / n:.1%} · placebo (powers dealt to other teams) {plc_hit / n:.1%} · always the home team {home_hit / n:.1%} · "
          f"better record so far {wins_hit / n:.1%} ({tie} ties counted half)")
    return {"n": n, "power": hit / n, "placebo": plc_hit / n, "home": home_hit / n, "record": wins_hit / n}


def paths_table(wt, season, last):
    """(4) Boise State and the playoff field, by week."""
    teams = {int(t): v for t, v in wt["teams"].items()}
    ranks = {}
    for wk in (FIRST_WEEK, 9, last):
        rated = sorted(((at_week(v["weeks"], wk), t) for t, v in teams.items()), key=lambda x: -(x[0]["head"] if x[0] and x[0]["head"] is not None else -1e9))
        ranks[wk] = {t: i + 1 for i, (r, t) in enumerate(rated) if r and r["head"] is not None}
    cfp_path = ROOT / "data" / "raw" / "cfp_participants.json"
    cfp = pd.DataFrame(json.loads(cfp_path.read_text(encoding="utf-8"))) if cfp_path.exists() else pd.DataFrame()
    field = cfp[cfp["season"] == season] if len(cfp) else cfp
    print(f"\n(4) PATHS: headline rank after weeks {FIRST_WEEK}, 9 and {last}")
    print("    team                    wk%d   wk9  wk%d   record at %d   CFP seed" % (FIRST_WEEK, last, last))
    names = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "teams.json").read_text(encoding="utf-8")))
    by_name = dict(zip(names["school"], names["id"]))
    seeds = {}
    if len(field):
        for r in field.itertuples():
            tid = by_name.get(getattr(r, "school", None) or getattr(r, "team", None))
            if tid is not None:
                seeds[int(tid)] = getattr(r, "seed", None)
    wanted = [by_name["Boise State"]] + [t for t in seeds if t != by_name["Boise State"]]
    for t in wanted:
        v = teams.get(int(t))
        if not v:
            continue
        end = at_week(v["weeks"], last)
        print(f"    {v['school']:<22}{ranks[FIRST_WEEK].get(int(t), '-'):>5}{ranks[9].get(int(t), '-'):>6}{ranks[last].get(int(t), '-'):>6}"
              f"   {end['w']}-{end['l']:<9}  {seeds.get(int(t), '-')}")


def availability_table(wp, wt, pos, last, week=8):
    """(5) rating after `week` minus the last week's, by share of the team's games played by `week`."""
    games_by_team = {int(t): at_week(v["weeks"], week) for t, v in wt["teams"].items()}
    team_of = {}
    print(f"\n(5) AVAILABILITY: rating after week {week} minus after week {last}, by share of his team's games he had played by week {week}")
    print("    share        n   mean gap   median   p10     p90   | of players rated 85+ at week %d: n, mean gap" % week)
    ps = pd.DataFrame(json.loads((ROOT / "data" / "raw" / "player_seasons.json").read_text(encoding="utf-8")))
    team_of = dict(zip(ps["player_id"], ps["team_id"])) if False else {}
    by_player = ps.drop_duplicates("player_id", keep="last")
    team_of = dict(zip(by_player["player_id"], by_player["team_id"]))
    rows = []
    for pid, prow in wp["players"].items():
        pid = int(pid)
        a, b = at_week(prow, week), at_week(prow, last)
        t = games_by_team.get(int(team_of.get(pid, -1)))
        if not a or not b or a[1] is None or b[1] is None or not t or not a[2] or FAMILY.get(pos.get(pid)) in (None, "K/P") or a[3] != "P":
            continue
        share = min(1.0, a[2] / max(t["w"] + t["l"], 1))
        rows.append((next(lbl for top, lbl in SHARE_BUCKETS if share <= top), b[1] - a[1], a[1]))
    df = pd.DataFrame(rows, columns=["bucket", "gap", "r8"])
    for _, lbl in SHARE_BUCKETS:
        d = df[df["bucket"] == lbl]["gap"]
        top = df[(df["bucket"] == lbl) & (df["r8"] >= 85)]["gap"]
        if len(d):
            print(f"    {lbl:<9}{len(d):>7,}{d.mean():>+10.2f}{d.median():>+9.2f}{d.quantile(.1):>+7.1f}{d.quantile(.9):>+8.1f}   |{len(top):>30}{(top.mean() if len(top) else float('nan')):>+10.2f}")
    return df


def against_published(wp, final_pub, pos, last):
    """(0) the last replayed week against the published season rating, by position family."""
    print(f"\n(0) THE LAST REPLAYED WEEK ({last}) AGAINST THE PUBLISHED SEASON RATING (the machinery, not an identity)")
    print("    family        n   median |diff|   p90 |diff|   mean signed (replay - published)")
    by = {}
    for pid, rows in wp["players"].items():
        r = at_week(rows, last)
        pub = final_pub.get(int(pid))
        if r and r[1] is not None and pub is not None:
            by.setdefault(FAMILY.get(pos.get(int(pid)), "other"), []).append(r[1] - pub)
    for fam, d in sorted(by.items()):
        a = np.abs(d)
        print(f"    {fam:<8}{len(d):>9,}{np.median(a):>14.2f}{np.quantile(a, .9):>13.2f}{np.mean(d):>+22.2f}")


def main() -> None:
    """Print sections 0 to 5 for one replayed season."""
    ap = argparse.ArgumentParser(description="How a replayed finished season held up, week by week")
    ap.add_argument("--season", type=int, required=True)
    args = ap.parse_args()
    wp, wt, pos, prev, prev_team, final_pub = load(args.season)
    last = max(wp["weeks"])
    print(f"{args.season}: weeks {wp['weeks'][0]}-{last} replayed; {len(wp['players']):,} players, {len(wt['teams'])} teams")
    against_published(wp, final_pub, pos, last)
    players_table(wp, pos, prev, last)
    teams_table(wt, prev_team, last)
    picks_table(wt, args.season, last)
    paths_table(wt, args.season, last)
    availability_table(wp, wt, pos, last)


if __name__ == "__main__":
    main()
