"""How should the headline team rating mix roster, stats and results? Reproduces W_ROSTER_BLEND / W_STATS_BLEND / W_RESULTS_BLEND in script 10.

Part 1, a finished season. For every team-season 2016-2025 the three readings are the roster half (script 10's slot score through
roster_to_ovr), the published stats half, and the results half (the season's points strength through results_to_ovr). The outcome
is the NEXT season's points strength (it is not the reading's own season, which would make the results reading predict itself).
Reported: the mean within-season Spearman of each reading, the old two-reading blend, the shipped three-reading blend, and
non-negative least squares weights fitted leave-one-season-out on the z-scored readings, and a 0.1 grid of blends. The shipped setting
(results 2/3, roster 1/6, stats 1/6) is the owner's, as the FCS weight is; the gate asks that roster and stats each keep at least 0.1 and
that the blend stay within 0.02 of the best on the grid.

Part 2, a season in progress. Cut after week 4, 6, 8 and 10: the results so far (points strength from the games to that week), and
the previous season's roster (a leak-free talent reading: the cut roster ratings are not on file), predict the strength shown in the
games AFTER the cut; leave-one-season-out OLS on within-season z-scores. The coefficients say how much a team's results so far
should count against what it was before the season, and whether that share grows with games played.

Part 3, does the box score add anything the results do not? Cut after week 6, 8 and 10, 2017-2025: the strength shown after the cut on
points so far and last season's strength, with and without the stats half built from the per-game team box scores in the API cache
(season averages through the cut, graded as script 10 grades them, plus the schedule adjustment). With --cut-roster it also adds a roster
reading at the cut (the 2021-2025 cut runs of script 06 through the frozen anchors and the slot score; about half an hour).

Usage:
    python scripts/validate_team_overall.py
    python scripts/validate_team_overall.py --cut-roster
"""

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import nnls
from scipy.stats import spearmanr

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils import opponent_strength as ostr  # noqa: E402
from utils.store import read_raw  # noqa: E402

_spec = importlib.util.spec_from_file_location("_vtr", ROOT / "scripts" / "validate_team_roster.py")
vtr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vtr)
t10 = vtr.t10

CUTS = [4, 6, 8, 10]


def roster_ovr(lists: dict, lu) -> float | None:
    """The roster half exactly as script 10 builds it: the slot score, and the position-weighted half when it carries weight."""
    slots = t10.slot_score(lists, lu)
    if slots is None:
        return None
    if t10.W_ROSTER_WEIGHTED == 0:
        return t10.roster_to_ovr(slots)
    ros = t10.compute_roster_splits({g: [x for x in xs if x >= 55.0] for g, xs in lists.items()}, lu)
    weighted = t10.blend([(ros["pass_off"], t10.W_PASS_OFF), (ros["run_off"], t10.W_RUN_OFF), (ros["pass_def"], t10.W_PASS_DEF),
                          (ros["run_def"], t10.W_RUN_DEF), (ros["special"], t10.W_SPEC)])
    if weighted is None:
        return t10.roster_to_ovr(slots)
    return t10.roster_to_ovr(slots) * t10.W_ROSTER_SLOTS + t10.roster_to_ovr(weighted) * t10.W_ROSTER_WEIGHTED


def table(R: dict, pub: pd.DataFrame, Y: dict) -> pd.DataFrame:
    """One row per team-season 2016-2025: the three readings and the same- and next-season strength."""
    rows = []
    for r in pub.itertuples():
        if r.season < 2016 or (r.season, r.team) not in Y or (r.season, r.team) not in R:
            continue
        ro = roster_ovr(R[(r.season, r.team)], r.lu if r.lu == r.lu else None)
        if ro is None:
            continue
        rows.append({"season": r.season, "team": r.team, "R": ro, "S": r.sh, "P": t10.results_to_ovr(Y[(r.season, r.team)]),
                     "y": Y[(r.season, r.team)], "ynext": Y.get((r.season + 1, r.team))})
    return pd.DataFrame(rows)


def zs(T: pd.DataFrame, c: str) -> pd.Series:
    """A column as within-season z-scores."""
    return (T[c] - T.groupby("season")[c].transform("mean")) / T.groupby("season")[c].transform("std")


def spear(T: pd.DataFrame, v, target: str) -> float:
    """Mean within-season Spearman of v with T[target]."""
    v = pd.Series(np.asarray(v), index=T.index)
    out = []
    for s in sorted(T["season"].unique()):
        m = (T["season"] == s) & T[target].notna()
        if m.sum() > 30:
            out.append(spearmanr(v[m], T.loc[m, target])[0])
    return float(np.mean(out))


def part1(T: pd.DataFrame) -> dict:
    """Next-season persistence of each reading and of the blends; returns the fitted all-seasons weights."""
    T = T[T["ynext"].notna()].copy()
    Z = pd.DataFrame({c: zs(T, c) for c in ("R", "S", "P")})
    yn = zs(T.assign(ynext=T["ynext"]), "ynext")
    print(f"PART 1 - {len(T)} team-seasons with a next season; outcome = next season's points strength\n")
    print(f"{'reading':46s} {'next':>7s}")
    for name, v in (("roster alone", T["R"]), ("stats alone", T["S"]), ("results alone", T["P"]),
                    ("old blend 0.60 roster + 0.40 stats", 0.6 * Z["R"] + 0.4 * Z["S"]),
                    (f"shipped {t10.W_ROSTER_BLEND:.2f} / {t10.W_STATS_BLEND:.2f} / {t10.W_RESULTS_BLEND:.2f}",
                     t10.W_ROSTER_BLEND * Z["R"] + t10.W_STATS_BLEND * Z["S"] + t10.W_RESULTS_BLEND * Z["P"]),
                    ("equal thirds", (Z["R"] + Z["S"] + Z["P"]) / 3)):
        print(f"{name:46s} {spear(T, v, 'ynext'):7.3f}")
    X, y, pred = Z.values, yn.values, np.zeros(len(T))
    for s in sorted(T["season"].unique()):
        te = (T["season"] == s).values
        w, _ = nnls(X[~te], y[~te])
        pred[te] = X[te] @ w
    print(f"{'NNLS, leave-one-season-out':46s} {spear(T, pred, 'ynext'):7.3f}")
    w, _ = nnls(X, y)
    fitted = dict(zip("RSP", w / w.sum()))
    print(f"\nNNLS weights on all seasons (sum 1): roster {fitted['R']:.2f}, stats {fitted['S']:.2f}, results {fitted['P']:.2f}")
    grid = {(r / 10, s_ / 10, (10 - r - s_) / 10): spear(T, (r / 10) * Z["R"] + (s_ / 10) * Z["S"] + ((10 - r - s_) / 10) * Z["P"], "ynext")
            for r in range(11) for s_ in range(11 - r)}
    best = max(grid.values())
    print(f"best blend on a 0.1 grid: {max(grid, key=grid.get)} -> {best:.3f}")
    print("blends that keep every reading at 0.2 or more, best first:")
    for k_, v_ in sorted(((k, v) for k, v in grid.items() if min(k) >= 0.2), key=lambda kv: -kv[1])[:4]:
        print(f"   roster {k_[0]:.1f} stats {k_[1]:.1f} results {k_[2]:.1f} -> {v_:.3f}")
    fitted["_best"] = best
    fitted["_shipped"] = spear(T, t10.W_ROSTER_BLEND * Z["R"] + t10.W_STATS_BLEND * Z["S"] + t10.W_RESULTS_BLEND * Z["P"], "ynext")
    print(f"shipped {t10.W_ROSTER_BLEND:.3f} / {t10.W_STATS_BLEND:.3f} / {t10.W_RESULTS_BLEND:.3f}: {fitted['_shipped']:.3f}")
    print("correlations of the readings (all seasons):", Z.corr().round(2).values[np.triu_indices(3, 1)].tolist(), "(R-S, R-P, S-P)")
    return fitted


def cut_strengths(season: int, cut: int, teams) -> tuple:
    """(strength through week `cut`, strength in the regular and post-season games after it) for every team that played in both."""
    recs, games = read_raw("team_records"), read_raw("games")
    cur = {int(r.id): r.conference for r in teams.itertuples()}
    sc = {int(r.team_id): r.conference for r in recs[recs["season"] == season].dropna(subset=["team_id", "conference"]).itertuples()}
    conf = lambda t: sc.get(int(t)) if t != ostr.FCS else None  # noqa: E731
    g = games[(games["season"] == season) & games["home_score"].notna() & games["away_score"].notna()]
    early = g[(g["season_type"] == "regular") & (g["week"] <= cut)]
    late = g[(g["season_type"] != "regular") | (g["week"] > cut)]
    ids = sorted(sc) or sorted(cur)
    return (ostr.team_strength(early.to_dict("records"), conf, ids), ostr.team_strength(late.to_dict("records"), conf, ids))


def part2(R: dict, pub: pd.DataFrame, Y: dict) -> None:
    """Coefficients of the strength after the cut on the results so far and last season's roster, by cut week."""
    teams = read_raw("teams")
    lu = {(r.season, r.team): (r.lu if r.lu == r.lu else None) for r in pub.itertuples()}
    print("\nPART 2 - a season in progress: strength AFTER the cut on [results so far, last season's roster, last season's strength]\n")
    print(f"{'cut':>4s} {'n':>5s} {'R2 results only':>16s} {'R2 prev roster':>15s} {'R2 both':>8s}   coef results / roster / prev strength   results share")
    for cut in CUTS:
        rows = []
        for s in range(2017, 2026):
            early, late = cut_strengths(s, cut, teams)
            for t, e in early.items():
                if t not in late or (s - 1, t) not in R or (s - 1, t) not in Y:
                    continue
                ro = roster_ovr(R[(s - 1, t)], lu.get((s - 1, t)))
                if ro is not None:
                    rows.append({"season": s, "P": e["off"] + e["dfn"], "Rp": ro, "Yp": Y[(s - 1, t)], "y": late[t]["off"] + late[t]["dfn"]})
        D = pd.DataFrame(rows)
        z = {c: (D[c] - D.groupby("season")[c].transform("mean")) / D.groupby("season")[c].transform("std") for c in ("P", "Rp", "Yp", "y")}

        def loso(cols):
            X = np.column_stack([np.ones(len(D))] + [z[c].values for c in cols])
            yv, pred, coefs = z["y"].values, np.zeros(len(D)), []
            for s in sorted(D["season"].unique()):
                te = (D["season"] == s).values
                b = np.linalg.lstsq(X[~te], yv[~te], rcond=None)[0]
                pred[te] = X[te] @ b
                coefs.append(b[1:])
            return 1 - np.sum((yv - pred) ** 2) / np.sum((yv - yv.mean()) ** 2), np.mean(coefs, 0)

        r_p, _ = loso(["P"])
        r_r, _ = loso(["Rp"])
        r_b, c = loso(["P", "Rp", "Yp"])
        print(f"{cut:4d} {len(D):5d} {r_p:16.3f} {r_r:15.3f} {r_b:8.3f}   {c[0]:.2f} / {c[1]:.2f} / {c[2]:.2f}                         {c[0] / (c[0] + c[1] + c[2]):.2f}")


def box_scores(season: int) -> pd.DataFrame:
    """One row per game of a season from the cached /games/teams box scores: both teams' ids, points, yards, third downs, sacks plus TFL, turnovers."""
    from utils import api_client as ac
    teams = read_raw("teams")
    cfb2db = {int(r.cfb_api_id): int(r.id) for r in teams.itertuples() if r.cfb_api_id == r.cfb_api_id}

    def num(d, k):
        try:
            return float(d.get(k))
        except (TypeError, ValueError):
            return np.nan

    rows = []
    for st, weeks in (("regular", range(1, 16)), ("postseason", range(1, 8))):
        for wk in weeks:
            f = os.path.join(ac.CACHE_DIR, ac._cache_key("/games/teams", {"year": season, "week": wk, "seasonType": st}) + ".json")
            if not os.path.exists(f):
                continue
            for g in json.load(open(f, encoding="utf8")):
                ts = g.get("teams", [])
                if len(ts) != 2 or ts[0].get("points") is None or ts[1].get("points") is None:
                    continue
                d = [{x["category"]: x["stat"] for x in t["stats"]} for t in ts]
                rec = {"week": wk, "season_type": st, "gid": g["id"]}
                for i, t in enumerate(ts):
                    p = "h" if t.get("homeAway") == "home" else "a"
                    try:
                        m, n = str(d[i].get("thirdDownEff")).split("-")
                        m, n = float(m), float(n)
                    except (ValueError, AttributeError):
                        m = n = np.nan
                    rec.update({p + "_id": cfb2db.get(t.get("teamId")), p + "_pts": float(t["points"]), p + "_yds": num(d[i], "totalYards"), p + "_t3": m,
                                p + "_t3n": n, p + "_hav": num(d[i], "sacks") + num(d[i], "tacklesForLoss"), p + "_to": num(d[i], "turnovers")})
                if "h_id" in rec and "a_id" in rec:
                    rows.append(rec)
    return pd.DataFrame(rows)


def stats_half_at_cut(early: pd.DataFrame, ids: list, conf) -> dict:
    """The stats half as the headline builds it, from the games through the cut: season averages graded on the fixed anchors plus 1.57 x the schedule faced."""
    a = early.rename(columns=lambda c: c.replace("h_", "me_").replace("a_", "op_"))
    b = early.rename(columns=lambda c: c.replace("a_", "me_").replace("h_", "op_"))
    tg = pd.concat([a, b], ignore_index=True)
    tg = tg[tg["me_id"].isin(ids)]
    agg = tg.groupby("me_id").agg(g=("gid", "count"), yds=("me_yds", "mean"), yda=("op_yds", "mean"), t3=("me_t3", "sum"), t3n=("me_t3n", "sum"),
                                  d3=("op_t3", "sum"), d3n=("op_t3n", "sum"), hav=("me_hav", "mean"), to_for=("op_to", "sum"), to_ag=("me_to", "sum"))
    games = [{"home_team_id": r.h_id, "away_team_id": r.a_id, "home_score": r.h_pts, "away_score": r.a_pts, "neutral_site": False, "season_type": "regular"}
             for r in early.itertuples()]
    sched = ostr.schedule_faced(games, conf, ids)
    out = {}
    for t, r in agg.iterrows():
        if r.g < 4:
            continue
        stats = {"yards_pg": r.yds, "yards_allowed_pg": r.yda, "third_down_pct": 100 * r.t3 / r.t3n, "third_down_def_pct": 100 * r.d3 / r.d3n,
                 "sacks_pg": r.hav, "tfl_pg": 0.0, "turnover_margin": (r.to_for - r.to_ag) / r.g * 12}
        off, dfn, adj = t10.team_stats_split(stats, sched.get(t))
        if off is not None and dfn is not None:
            out[t] = 0.5 * off + 0.5 * dfn + adj
    return out


def cut_roster_table() -> pd.DataFrame:
    """A roster reading at each cut for 2021-2025 from script 06's cut runs: paced EDGE through the anchors, the slot score, no line unit, no tier."""
    spec = importlib.util.spec_from_file_location("_vps", ROOT / "scripts" / "validate_pace_scale.py")
    vps = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vps)
    rows = []
    for s in range(2021, 2026):
        D = vps.collect([s])
        D = D[D["edge_score"].notna() & (D["edge_score"] > 0)]
        D = D.assign(r=[vps.t07.edge_to_ovr(e, pg) for e, pg in zip(D["edge_score"], D["position_group"])])
        for (cut, tid), g in D.groupby(["cut", "team_id"]):
            by = {pg: sorted(x["r"].tolist(), reverse=True) for pg, x in g.groupby("position_group")}
            sc = t10.slot_score(by, None)
            if sc is not None:
                rows.append({"season": s, "cut": int(cut), "team": int(tid), "R": t10.roster_to_ovr(sc)})
    return pd.DataFrame(rows)


def part3(with_roster: bool) -> None:
    """Held-out R2 of the strength after the cut on points so far + last season's strength, with and without the stats half (and the cut roster)."""
    recs, games = read_raw("team_records"), read_raw("games")
    rows, prev = [], {}
    for s in range(2016, 2026):
        sc = {int(r.team_id): r.conference for r in recs[recs["season"] == s].dropna(subset=["team_id", "conference"]).itertuples()}
        conf = lambda t, sc=sc: sc.get(int(t)) if t != ostr.FCS else None  # noqa: E731
        ids = sorted(sc)
        D = box_scores(s)
        g = games[(games["season"] == s) & games["home_score"].notna() & games["away_score"].notna()]

        def strength(df, conf=conf, ids=ids):
            return {t: v["off"] + v["dfn"] for t, v in ostr.team_strength(df.to_dict("records"), conf, ids).items()}

        for cut in (6, 8, 10):
            early = D[(D["season_type"] == "regular") & (D["week"] <= cut)]
            ie = [i for i in ids if i in set(early["h_id"]) | set(early["a_id"])]
            A = stats_half_at_cut(early, ie, conf)
            P = strength(g[(g["season_type"] == "regular") & (g["week"] <= cut)])
            L = strength(g[(g["season_type"] != "regular") | (g["week"] > cut)])
            for t in ie:
                if t in A and t in P and t in L and t in prev:
                    rows.append({"season": s, "cut": cut, "team": t, "A": A[t], "P": P[t], "y": L[t], "yprev": prev[t]})
        prev = strength(g)
    T = pd.DataFrame(rows).replace([np.inf, -np.inf], np.nan).dropna()
    if with_roster:
        T = T.merge(cut_roster_table(), on=["season", "cut", "team"])
    T = T[T.groupby(["season", "cut"])["team"].transform("size") >= 60]
    print("\nPART 3 - does the box score add anything the results do not? held-out R2 of the strength after the cut\n")
    cols = ["A", "P", "yprev", "y"] + (["R"] if with_roster else [])
    for cut in (6, 8, 10):
        d = T[T["cut"] == cut].reset_index(drop=True)
        Z = {c: ((d[c] - d.groupby("season")[c].transform("mean")) / d.groupby("season")[c].transform("std")).values for c in cols}

        def loso(use, d=d, Z=Z):
            X = np.column_stack([np.ones(len(d))] + [Z[c] for c in use])
            y, pred = Z["y"], np.zeros(len(d))
            for s_ in sorted(d["season"].unique()):
                te = (d["season"] == s_).values
                b = np.linalg.lstsq(X[~te], y[~te], rcond=None)[0]
                pred[te] = X[te] @ b
            return 1 - np.sum((y - pred) ** 2) / np.sum((y - y.mean()) ** 2)

        parts = [("last season", ["yprev"]), ("points so far", ["P"]), ("points + last season", ["P", "yprev"]), ("+ stats half", ["P", "A", "yprev"])]
        if with_roster:
            parts.append(("+ cut roster", ["P", "R", "yprev"]))
        print(f"cut after week {cut} (n={len(d)}): " + " | ".join(f"{name} {loso(use):.3f}" for name, use in parts))


def main() -> int:
    """Run the parts; return 1 if roster or stats is under 0.1 or the shipped blend is more than 0.02 below the best next-season Spearman."""
    ap = argparse.ArgumentParser(description="Headline mix validation")
    ap.add_argument("--cut-roster", action="store_true", help="also test a roster reading at the cut (about half an hour)")
    args = ap.parse_args()
    Y, R, pub = vtr.strengths(), vtr.rosters(), vtr.published()
    T = table(R, pub, Y)
    fitted = part1(T)
    part2(R, pub, Y)
    part3(args.cut_roster)
    gap = fitted["_best"] - fitted["_shipped"]
    print(f"\nshipped blend {fitted['_shipped']:.3f} against the best {fitted['_best']:.3f} (gap {gap:.3f}; the gate allows 0.02; roster and stats at least 0.1)")
    return 1 if gap > 0.02 or min(t10.W_ROSTER_BLEND, t10.W_STATS_BLEND) < 0.1 - 1e-9 else 0


if __name__ == "__main__":
    sys.exit(main())
