"""Does a quarterback's efficiency predict his offense beyond his volume? Reproduces QB_EFFICIENCY_BLEND in scripts/06_compute_edge_scores.py.

Unit: a QB-season with at least 4 games (10+ attempts each) through week 8 and at least 3 games after, 2017-2025. Features come
from the games through week 8. The outcome is his team's points over expectation (points scored + the opponent's defensive
strength, centred by season) in his games AFTER week 8. The control is the team's offense strength the previous season. Everything
is leave-one-season-out OLS on within-season z-scores, scored by held-out R2.

Reports: volume, efficiency (yards, TD, INT and completions per attempt) and "points over expectation so far" (impact) on the
same footing; the blend of today's volume score with the same score restated at a standard attempt count (lambda 0..1); the
interception weight; and a placebo that shuffles attempts across games within a season.

The impact feature is reported to show why it is NOT used: it predicts the later offense best of all, but it is the team's offense.

Usage:
    python scripts/validate_qb_composite.py
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils import opponent_strength as ostr  # noqa: E402
from utils.store import read_raw  # noqa: E402

_spec = importlib.util.spec_from_file_location("_t06q", ROOT / "scripts" / "06_compute_edge_scores.py")
t06 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t06)

SEASONS = range(2016, 2026)
EFF = ["ypa", "tdr", "intr", "cmpp"]


def base_composite(stats: dict) -> float:
    """Today's volume score for one QB game, before the efficiency blend."""
    return sum(t06._coerce_float(stats.get(k)) * w for k, w in t06.OFFENSIVE_COMPOSITE["QB"].items())


def num(d: dict, k: str) -> float:
    """A stat as a float, 0.0 when missing."""
    return t06._coerce_float(d.get(k))


def game_table() -> pd.DataFrame:
    """One row per QB game (10+ attempts): stats, the volume score and the team's points over expectation."""
    recs, games = read_raw("team_records"), read_raw("games")
    rows = []
    for s in SEASONS:
        sc = {int(r.team_id): r.conference for r in recs[recs["season"] == s].dropna(subset=["team_id", "conference"]).itertuples()}
        conf = lambda t, sc=sc: sc.get(int(t)) if t != ostr.FCS else None  # noqa: E731
        g = games[(games["season"] == s) & games["home_score"].notna() & games["away_score"].notna()]
        sol = ostr.solve(g.to_dict("records"), conf, sorted(sc))
        gi = games[games["season"] == s].set_index("id")
        for r in t06._load_game_stats(s, {"QB"}).itertuples():
            gm = gi.loc[r.game_id] if r.game_id in gi.index else None
            if gm is None or gm.season_type != "regular" or pd.isna(gm.home_score):
                continue
            me = int(r.player_team_id)
            home = me == gm.home_team_id
            opp = gm.away_team_id if home else gm.home_team_id
            opp = int(opp) if opp == opp and opp is not None else ostr.FCS
            att = t06._qb_attempts(r.data)
            if att < 10:
                continue
            cmp_ = t06._coerce_float(str(r.data.get("passingC/ATT") or "0/0").split("/")[0])
            pts = float(gm.home_score if home else gm.away_score)
            rows.append(dict(season=s, pid=r.player_id, team=me, week=int(gm.week), att=att, cmp=cmp_, yds=num(r.data, "passingYDS"),
                             td=num(r.data, "passingTD"), ints=num(r.data, "passingINT"), comp=base_composite(r.data),
                             poe=pts + (sol[opp]["dfn"] if opp in sol else sol[ostr.FCS]["dfn"]), off=sol.get(me, {}).get("off")))
    out = pd.DataFrame(rows)
    out["poe"] = out["poe"] - out.groupby("season")["poe"].transform("mean")
    return out


def qb_seasons(G: pd.DataFrame) -> pd.DataFrame:
    """One row per QB-season with pre-week-9 features, the later outcome and last season's team offense."""
    off = {s: G[G.season == s].groupby("team")["off"].first().to_dict() for s in G.season.unique()}
    out = []
    for (s, pid, team), x in G.groupby(["season", "pid", "team"]):
        pre, post = x[x.week <= 8], x[x.week > 8]
        if len(pre) < 4 or len(post) < 3 or (s - 1) not in off or team not in off[s - 1]:
            continue
        a = pre.att.sum()
        out.append(dict(season=s, pid=pid, team=team, vol=pre.comp.mean(), ypa=pre.yds.sum() / a, tdr=pre.td.sum() / a,
                        intr=pre.ints.sum() / a, cmpp=pre.cmp.sum() / a, poe_pre=pre.poe.mean(), ctrl=off[s - 1][team], y=post.poe.mean()))
    return pd.DataFrame(out)


def heldout_r2(D: pd.DataFrame, cols: list, shuffle: set | None = None) -> tuple:
    """Leave-one-season-out R2 of the z-scored outcome on the z-scored columns, and the mean coefficients."""
    z = lambda c: (c - c.groupby(D.season).transform("mean")) / c.groupby(D.season).transform("std")  # noqa: E731
    X = np.column_stack([z(D[c]).values for c in cols])
    if shuffle:
        rng = np.random.default_rng(0)
        for j, c in enumerate(cols):
            if c in shuffle:
                for s in D.season.unique():
                    m = (D.season == s).values
                    X[m, j] = rng.permutation(X[m, j])
    X = np.column_stack([np.ones(len(D)), X])
    y = z(D["y"]).values
    pred, coefs = np.zeros(len(D)), []
    for s in sorted(D.season.unique()):
        te = (D.season == s).values
        b = np.linalg.lstsq(X[~te], y[~te], rcond=None)[0]
        pred[te] = X[te] @ b
        coefs.append(b[1:])
    return 1 - np.sum((y - pred) ** 2) / np.sum((y - y.mean()) ** 2), np.mean(coefs, 0)


def blended(G: pd.DataFrame, D: pd.DataFrame, lam: float, att_ref: float, int_mult: float = 1.0) -> pd.DataFrame:
    """D with `cnew`: the mean over his pre-week-9 games of (1 - lam) x volume + lam x volume / attempts x att_ref."""
    g = G[G.week <= 8].copy()
    g["c"] = (1 - lam) * g["comp"] + lam * g["comp"] / g["att"].clip(lower=t06.QB_ATT_FLOOR) * att_ref - (int_mult - 1.0) * 20.0 * g["ints"]
    return D.merge(g.groupby(["season", "pid", "team"])["c"].mean().rename("cnew").reset_index(), on=["season", "pid", "team"])


def main() -> int:
    """Print the held-out table and return 1 if the shipped blend is not the best of the grid."""
    G = game_table()
    D = qb_seasons(G)
    ref = t06.QB_ATT_REF
    print(f"{len(G):,} QB games, {len(D)} QB-seasons {int(D.season.min())}-{int(D.season.max())}; median attempts {G['att'].median():.0f}, shipped reference {ref:.0f}\n")
    print("held-out R2 of his offense's points over expectation after week 8 (leave-one-season-out):")
    for tag, cols in (("control only (last season's team offense)", ["ctrl"]), ("control + volume", ["ctrl", "vol"]),
                      ("control + efficiency", ["ctrl"] + EFF), ("control + volume + efficiency", ["ctrl", "vol"] + EFF),
                      ("control + points over expectation so far (impact)", ["ctrl", "poe_pre"]),
                      ("volume alone", ["vol"]), ("efficiency alone", EFF), ("impact alone", ["poe_pre"])):
        r2, _ = heldout_r2(D, cols)
        print(f"   {tag:52s} {r2:.4f}")
    r2, _ = heldout_r2(D, ["ctrl", "vol"] + EFF, shuffle=set(EFF))
    print(f"   placebo: efficiency shuffled across QBs within season     {r2:.4f}\n")
    print("blend of today's score with the same score at a standard attempt count (control + blended score):")
    scores = {}
    for lam in (0.0, 0.25, 0.5, 0.75, 1.0):
        d = blended(G, D, lam, ref)
        scores[lam] = heldout_r2(d.assign(vol=d["cnew"]), ["ctrl", "vol"])[0]
        print(f"   lambda {lam:4.2f}: {scores[lam]:.4f}")
    for im in (1.5, 2.0, 3.0):
        d = blended(G, D, t06.QB_EFFICIENCY_BLEND, ref, im)
        print(f"   interception weight x{im} at the shipped blend: {heldout_r2(d.assign(vol=d['cnew']), ['ctrl', 'vol'])[0]:.4f}")
    rng = np.random.default_rng(0)
    Gp = G.copy()
    Gp["att"] = Gp.groupby("season")["att"].transform(lambda v: rng.permutation(v.values))
    d = blended(Gp, D, t06.QB_EFFICIENCY_BLEND, ref)
    print(f"   placebo (attempts shuffled across games within season) at the shipped blend: {heldout_r2(d.assign(vol=d['cnew']), ['ctrl', 'vol'])[0]:.4f}")
    best = max(scores, key=scores.get)
    return 0 if abs(best - t06.QB_EFFICIENCY_BLEND) < 1e-9 else 1


if __name__ == "__main__":
    sys.exit(main())
