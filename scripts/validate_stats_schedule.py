"""Is the stats half of the team rating schedule-blind, and does adjusting it for the schedule help?

Reproduces the measurement behind SCHED_COEF in scripts/10_compute_team_ratings.py.

  1. THE DEFECT. For every team-season 2015-2025 (no 2020), regress the stats half (offense, defense) on the team's own
     points strength and on the strength of the schedule it faced, both from utils/opponent_strength.py, z-scored
     within the season. A nonzero coefficient on the schedule means the box score carries the schedule.
  2. THE COEFFICIENT. The same regression in OVR points per point per game of schedule, refitted leaving each
     season out in turn; exits 1 if SCHED_COEF has drifted from the fit by more than 10%.
  3. THE TEST. Like validate_rankings.py: the previous season's final quality predicts every later (week 9+)
     regular-season FBS game, calibrated leave-one-season-out. The quality is the shipped one (0.6 roster half +
     0.4 stats half) rebuilt with the stats half raw, adjusted at several weights, adjusted with the schedules
     dealt to the wrong teams (a placebo), and replaced by our own points strength (a ceiling that puts results in the rating).
  4. THE LEVEL GAP. Power-4 minus Group-of-6 of each quality, in rating points and in standard deviations of the
     season's spread; the games themselves imply the last row.

Reads sub_ratings.roster_half / stats_half_raw written by script 10 from v4.18. Writes
data/computed/stats_schedule_validation.json.

Usage:
    python scripts/validate_stats_schedule.py
"""

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from utils import opponent_strength as ostr  # noqa: E402
from utils.store import COMPUTED_DIR, read_computed, read_raw  # noqa: E402
import validate_rankings as vr  # noqa: E402

_spec = importlib.util.spec_from_file_location("_t10v", Path(__file__).parent / "10_compute_team_ratings.py")
t10 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t10)

P4 = {"SEC", "Big Ten", "Big 12", "ACC", "Pac-12"}
SEASONS = [s for s in range(2015, 2026) if s != 2020]


def build_table():
    """One row per team-season: the halves, the team's own points strength and the schedule it faced."""
    games = read_raw("games")
    recs = read_raw("team_records")
    conf = {(int(r.team_id), int(r.season)): r.conference for r in recs.dropna(subset=["team_id", "conference"]).itertuples()}
    ts = read_computed("team_season_stats")
    tr = read_computed("team_ratings")
    tr = tr[tr["engine"].fillna("edge") != "projected"]
    rows = []
    for season in SEASONS:
        g = games[(games["season"] == season) & games["home_score"].notna() & games["away_score"].notna()].to_dict("records")
        teams = sorted({t for (t, s) in conf if s == season})
        conf_of = lambda t, s=season: conf.get((int(t), s)) if t != ostr.FCS else None   # noqa: E731
        sol = ostr.solve(g, conf_of, teams)
        sched = ostr.schedule_faced(g, conf_of, teams)
        st = ts[ts["season"] == season].set_index("team_id")
        ov = tr[tr["season"] == season].set_index("team_id")
        for t in teams:
            if t not in st.index or t not in ov.index or t not in sched:
                continue
            sub = ov.loc[t, "sub_ratings"]
            sub = json.loads(sub) if isinstance(sub, str) else (sub or {})
            if sub.get("roster_half") is None or sub.get("stats_half_raw") is None:
                continue
            off, dfn, adj = t10.team_stats_split(st.loc[t].to_dict())
            if off is None or dfn is None:
                continue
            rows.append(dict(season=season, team_id=t, p4=conf[(t, season)] in P4, s_off=off, s_def=dfn, adj=adj,
                             roster=sub["roster_half"], stats_raw=sub["stats_half_raw"],
                             pts_off=sol[t]["off"], pts_def=sol[t]["dfn"], fd=sched[t][0], fo=sched[t][1]))
    df = pd.DataFrame(rows)
    df["pts"] = df["pts_off"] + df["pts_def"]
    return df, games, conf, set(int(t) for t in tr["team_id"].unique())


def ols(y, cols):
    """Least squares of y on the columns plus a constant: (coefficients, standard errors)."""
    X = np.column_stack([np.ones(len(y))] + cols)
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    r = y - X @ b
    se = np.sqrt(np.diag(r @ r / (len(y) - X.shape[1]) * np.linalg.inv(X.T @ X)))
    return b, se


def fit_c(d):
    """OVR points of the stats half per point per game of schedule faced, own points strength held fixed."""
    co = ols(d["s_off"].values, [d["pts_off"].values, d["fd"].values])[0][2]
    cd = ols(d["s_def"].values, [d["pts_def"].values, d["fo"].values])[0][2]
    return co, cd


def main() -> int:
    """Print the four sections, write the JSON; return 1 if SCHED_COEF has drifted from the fit by more than 10%."""
    df, games, conf, fbs = build_table()
    out = {"team_seasons": len(df), "seasons": sorted(int(s) for s in df["season"].unique())}
    print(f"{len(df):,} team-seasons, seasons {out['seasons']}\n")

    # 1. the defect, in standard deviations
    z = df.copy()
    for c in ("s_off", "s_def", "pts_off", "pts_def", "fd", "fo"):
        z[c] = z.groupby("season")[c].transform(lambda x: (x - x.mean()) / x.std())
    print("1. STATS HALF ON [own points strength, schedule faced], z-scored within the season")
    for lab, y, own, sch in (("offense", "s_off", "pts_off", "fd"), ("defense", "s_def", "pts_def", "fo")):
        b, se = ols(z[y].values, [z[own].values, z[sch].values])
        print(f"   {lab}: own strength {b[1]:+.3f} (t={b[1] / se[1]:+.1f})   schedule faced {b[2]:+.3f} (t={b[2] / se[2]:+.1f})")
        out[f"defect_{lab}"] = {"own": float(b[1]), "schedule": float(b[2]), "t_schedule": float(b[2] / se[2])}
    sd_fd, sd_fo = float(df["fd"].std()), float(df["fo"].std())
    gap = float(df[df["p4"]]["fd"].mean() - df[~df["p4"]]["fd"].mean())
    print(f"   schedule faced: sd {sd_fd:.2f} (defenses) / {sd_fo:.2f} (offenses) points per game; "
          f"Power 4 minus Group of 6 = {gap:+.2f} points per game of tougher defenses faced")
    out["schedule_sd"] = [sd_fd, sd_fo]
    out["schedule_p4_minus_g6"] = gap

    # 2. the coefficient, leave one season out
    allc = fit_c(df)
    coefs = {s: fit_c(df[df["season"] != s]) for s in sorted(df["season"].unique())}
    lo = min(min(-a, -b) for a, b in coefs.values())
    hi = max(max(-a, -b) for a, b in coefs.values())
    print(f"\n2. COEFFICIENT (OVR points per point per game): offense {-allc[0]:.2f}, defense {-allc[1]:.2f}; "
          f"leaving each season out {lo:.2f} to {hi:.2f}; SCHED_COEF = {t10.SCHED_COEF}")
    out["coefficient"] = {"offense": float(-allc[0]), "defense": float(-allc[1]), "loso_min": float(lo), "loso_max": float(hi),
                          "shipped": t10.SCHED_COEF}
    drift = abs(t10.SCHED_COEF - (-(allc[0] + allc[1]) / 2)) / t10.SCHED_COEF

    # 3. the test
    def quality(fn):
        q = {}
        for s in sorted(df["season"].unique()):
            d = df[df["season"] == s]
            for t, v in zip(d["team_id"].values, fn(s, d)):
                q[(int(t), int(s))] = float(v)
        return q

    def stats_adj(d, c, shuffle=None):
        fd, fo = d["fd"].values.copy(), d["fo"].values.copy()
        if shuffle is not None:
            rng = np.random.default_rng(shuffle)
            fd, fo = rng.permutation(fd), rng.permutation(fo)
        return 0.5 * (d["s_off"].values - c[0] * fd) + 0.5 * (d["s_def"].values - c[1] * fo) + d["adj"].values

    ps = df["stats_raw"].std() / df["pts"].std()
    variants = {
        "current (0.6 roster + 0.4 raw stats)": lambda s, d: 0.6 * d["roster"].values + 0.4 * d["stats_raw"].values,
        "roster half only": lambda s, d: d["roster"].values,
        "stats 0.4 adjusted (shipped)": lambda s, d: 0.6 * d["roster"].values + 0.4 * stats_adj(d, coefs[s]),
        "stats 0.5 adjusted": lambda s, d: 0.5 * d["roster"].values + 0.5 * stats_adj(d, coefs[s]),
        "stats 0.6 adjusted": lambda s, d: 0.4 * d["roster"].values + 0.6 * stats_adj(d, coefs[s]),
        "stats 0.3 adjusted": lambda s, d: 0.7 * d["roster"].values + 0.3 * stats_adj(d, coefs[s]),
        "placebo: schedules dealt to wrong teams": lambda s, d: 0.6 * d["roster"].values + 0.4 * stats_adj(d, coefs[s], shuffle=int(s)),
        "stats half := own points strength (ceiling)": lambda s, d: 0.6 * d["roster"].values + 0.4 * (df["stats_raw"].mean() + (d["pts"].values - df["pts"].mean()) * ps),
    }
    print("\n3. PRIOR-SEASON QUALITY PREDICTING LATER GAMES          RATING alone        POWER")
    out["models"] = {}
    quals = {}
    for name, fn in variants.items():
        quals[name] = q = quality(fn)
        rows = vr.power_gaps(games, q, conf, fbs)
        qa, qm, qk = vr.score(rows, "q")
        pa, pm, _ = vr.score(rows, "p")
        out["models"][name] = {"rating_winner": qa, "rating_mae": qm, "rating_slope": qk, "power_winner": pa, "power_mae": pm}
        print(f"   {name:46s} {qa:.3f} / {qm:5.2f}      {pa:.3f} / {pm:5.2f}")

    # 4. level gap
    print("\n4. POWER 4 MINUS GROUP OF 6, mean over seasons")
    out["level_gap"] = {}
    for name, q in quals.items():
        g, zz = [], []
        for s in sorted(df["season"].unique()):
            d = df[df["season"] == s]
            v = np.array([q[(int(t), int(s))] for t in d["team_id"]])
            gp = v[d["p4"].values].mean() - v[~d["p4"].values].mean()
            g.append(gp)
            zz.append(gp / v.std())
        out["level_gap"][name] = {"points": float(np.mean(g)), "sd": float(np.mean(zz))}
        print(f"   {name:46s} {np.mean(g):5.2f} points   {np.mean(zz):.2f} sd")
    pts = []
    spread = (0.6 * df["roster"] + 0.4 * df["stats_raw"]).std()    # the headline's own spread, so the rows compare
    for s in sorted(df["season"].unique()):
        d = df[df["season"] == s]
        v = d["pts"].values * (spread / df["pts"].std())
        pts.append(v[d["p4"].values].mean() - v[~d["p4"].values].mean())
    print(f"   {'own points strength alone (what the games imply)':46s} {np.mean(pts):5.2f} points (scaled to the headline's spread)")
    out["level_gap"]["games_imply_points"] = float(np.mean(pts))

    (COMPUTED_DIR / "stats_schedule_validation.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nSCHED_COEF {t10.SCHED_COEF} vs fitted {-(allc[0] + allc[1]) / 2:.3f}: drift {drift:.1%}")
    return 1 if drift > 0.10 else 0


if __name__ == "__main__":
    sys.exit(main())
