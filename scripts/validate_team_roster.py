"""How should a roster be scored? Reproduces the layout and SLOT_WEIGHTS in scripts/10_compute_team_ratings.py.

Unit: a team-season 2016-2025. The target is the team's points strength (offense + defense per game above an average FBS team,
from utils/opponent_strength, the project's yardstick), the same season ("fit") and the next season ("persistence"). The score of a
roster is a weighted mean of its position groups' starter-plus-depth scores (script 10's group_scores) with the line unit as the
offensive-line group; weights are chosen by the schemes below. Reported as the mean within-season Spearman rank correlation.

Schemes: the weights through v4.19 on the layout through v4.19 (QB 1 RB 2 WR 3 TE 1 DL 4 LB 3 DB 5, K and P together, 55 floor);
then on the v4.20 layout (QB 1 RB 2 WR 5 TE 1 DL 4 LB 4 DB 6, depth after the starters, every rated player) the weights fitted by
non-negative least squares (leave-one-season-out), a football prior, one weight per starter slot, and half-and-half mixes. Also
the starter share (0.80 / 0.85 / 0.90), the rating floor, and whether the position-weighted half adds anything to the slot score.

Usage:
    python scripts/validate_team_roster.py
"""

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import nnls
from scipy.stats import spearmanr

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils import opponent_strength as ostr  # noqa: E402
from utils.store import read_computed, read_raw  # noqa: E402

_spec = importlib.util.spec_from_file_location("_t10roster", ROOT / "scripts" / "10_compute_team_ratings.py")
t10 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t10)

SEASONS = range(2016, 2026)
GROUPS = ["QB", "RB", "WR", "TE", "OL", "DL", "LB", "DB", "K", "P"]
GROUP_OF = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "DL": "DL", "EDGE": "DL", "LB": "LB", "CB": "DB", "S": "DB", "DB": "DB", "K": "K", "P": "P"}
V419_STARTERS = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DL": 4, "LB": 3, "DB": 5}
V419 = {"layout": (V419_STARTERS, {g: 3 for g in V419_STARTERS}, 0.80),
        "weights": {"QB": 0.194, "RB": 0.189, "WR": 0.036, "TE": 0.016, "DL": 0.150, "LB": 0.123, "DB": 0.286, "KP": 0.008}}
PRIOR = {"QB": .20, "RB": .07, "WR": .13, "TE": .04, "OL": .17, "DL": .18, "LB": .08, "DB": .10, "K": .015, "P": .015}


def strengths() -> dict:
    """{(season, team_id): points strength} for 2015-2026 from every scored game of each season."""
    recs, games, teams = read_raw("team_records"), read_raw("games"), read_raw("teams")
    cur = {int(r.id): r.conference for r in teams.itertuples()}
    out = {}
    for s in range(2015, 2027):
        sc = {int(r.team_id): r.conference for r in recs[recs["season"] == s].dropna(subset=["team_id", "conference"]).itertuples()}
        conf = (lambda t, sc=sc: sc.get(int(t)) if t != ostr.FCS else None) if sc else (lambda t: cur.get(int(t)) if t != ostr.FCS else None)
        g = games[(games["season"] == s) & games["home_score"].notna() & games["away_score"].notna()]
        for t, v in ostr.team_strength(g.to_dict("records"), conf, sorted(sc) if sc else sorted(cur)).items():
            out[(s, t)] = v["off"] + v["dfn"]
    return out


def rosters() -> dict:
    """{(season, team_id): {position_group: [ratings, high to low]}} for every rated player 2015-2025 (earned ratings)."""
    rat = read_computed("ratings")
    ps = read_raw("player_seasons")[["id", "team_id", "position_group"]].rename(columns={"id": "player_season_id"})
    e = rat[(rat["engine"] == "edge") & rat["overall_rating"].notna() & rat["season"].between(2015, 2025)].merge(ps, on="player_season_id")
    e = e[e["team_id"].notna()]
    out = {}
    for (s, t, g), v in e.groupby(["season", "team_id", "position_group"])["overall_rating"]:
        out.setdefault((int(s), int(t)), {})[g] = sorted(v.tolist(), reverse=True)
    return out


def published() -> pd.DataFrame:
    """The line unit and stats half the shipped team ratings carry, per team-season (they do not depend on the roster layout)."""
    tr = read_computed("team_ratings")
    rows = []
    for r in tr[tr["season"].between(2015, 2025)].itertuples():
        sr = json.loads(r.sub_ratings) if isinstance(r.sub_ratings, str) else (r.sub_ratings or {})
        if sr.get("stats_half") is not None:
            rows.append({"season": int(r.season), "team": int(r.team_id), "lu": sr.get("line_unit"), "sh": sr.get("stats_half")})
    return pd.DataFrame(rows)


def build(R: dict, pub: pd.DataFrame, Y: dict, share: float, floor: float, layout=None) -> pd.DataFrame:
    """Group scores per team-season on a layout, with the same-season and next-season strength."""
    saved = (t10.SLOT_STARTERS, t10.SLOT_DEPTH, t10.STARTER_SHARE)
    if layout is not None:
        t10.SLOT_STARTERS, t10.SLOT_DEPTH, t10.STARTER_SHARE = layout
    else:
        t10.STARTER_SHARE = share
    rows = []
    try:
        for r in pub.itertuples():
            if r.season < 2016 or (r.season, r.team) not in R or (r.season, r.team) not in Y:
                continue
            lists = {g: [x for x in xs if x >= floor] for g, xs in R[(r.season, r.team)].items()}
            gs = t10.group_scores(lists, r.lu if r.lu == r.lu else None)
            gs.update({"season": r.season, "team": r.team, "y": Y[(r.season, r.team)], "ynext": Y.get((r.season + 1, r.team)), "sh": r.sh})
            rows.append(gs)
    finally:
        t10.SLOT_STARTERS, t10.SLOT_DEPTH, t10.STARTER_SHARE = saved
    return pd.DataFrame(rows)


def centered(X: pd.DataFrame, F: pd.DataFrame) -> pd.DataFrame:
    """Columns filled with the season mean where missing (fitting only), then centred within season."""
    X = X.apply(lambda c: c.fillna(c.groupby(F["season"]).transform("mean")))
    return X - X.groupby(F["season"]).transform("mean")


def fit_weights(F: pd.DataFrame, cols: list) -> dict:
    """Non-negative least squares of centred strength on the centred group scores, normalised to sum to one."""
    w, _ = nnls(centered(F[cols], F).values, (F["y"] - F.groupby("season")["y"].transform("mean")).values)
    return dict(zip(cols, w / w.sum()))


def loso_fit(F: pd.DataFrame, cols: list) -> np.ndarray:
    """Out-of-season predictions of centred strength from NNLS fitted on the other seasons."""
    X, y, pred = centered(F[cols], F).values, (F["y"] - F.groupby("season")["y"].transform("mean")).values, np.zeros(len(F))
    for s in sorted(F["season"].unique()):
        te = (F["season"] == s).values
        w, _ = nnls(X[~te], y[~te])
        pred[te] = X[te] @ w
    return pred


def score(F: pd.DataFrame, v: pd.Series, col: str) -> float:
    """Mean within-season Spearman of `v` with F[col]."""
    out = []
    for s in sorted(F["season"].unique()):
        m = (F["season"] == s).values & F[col].notna().values
        if m.sum() > 30:
            out.append(spearmanr(v[m], F[col][m])[0])
    return float(np.mean(out))


def composite(F: pd.DataFrame, w: dict) -> pd.Series:
    """Weighted mean of the group scores, a missing group filled with its season mean (fitting convenience only)."""
    X = F[list(w)].apply(lambda c: c.fillna(c.groupby(F["season"]).transform("mean")))
    return (X * pd.Series(w)).sum(axis=1) / sum(w.values())


def main() -> int:
    """Print the schemes and return 1 if the shipped SLOT_WEIGHTS are more than 0.02 from the half-and-half mix."""
    Y, R, pub = strengths(), rosters(), published()
    F = build(R, pub, Y, t10.STARTER_SHARE, 0.0)
    print(f"{len(F)} team-seasons {int(F['season'].min())}-{int(F['season'].max())}; groups missing: "
          + ", ".join(f"{g} {F[g].isna().mean():.1%}" for g in GROUPS if F[g].isna().any()) + "\n")
    # layout through v4.19, 55 floor, its own weights
    Fo = build(R, pub, Y, 0.8, 55.0, layout=V419["layout"])
    Fo["KP"] = Fo[["K", "P"]].mean(axis=1)
    old_w = V419["weights"]
    print(f"{'scheme':56s} {'fit':>6s} {'next':>6s}")
    print(f"{'layout and weights through v4.19 (no OL slot)':56s} {score(Fo, composite(Fo, old_w), 'y'):6.3f} {score(Fo, composite(Fo, old_w), 'ynext'):6.3f}")
    fitted = fit_weights(F, GROUPS)
    cnt = {"QB": 1, "RB": 2, "WR": 5, "TE": 1, "OL": 5, "DL": 4, "LB": 4, "DB": 6}
    equal = {g: cnt[g] / 28 * 0.97 for g in cnt}
    equal.update({"K": 0.015, "P": 0.015})
    schemes = {"v4.20 layout, fitted (NNLS, in-sample weights)": fitted, "v4.20 layout, football prior": PRIOR, "v4.20 layout, equal per starter": equal,
               "fitted + equal, half and half": {g: .5 * fitted[g] + .5 * equal[g] for g in GROUPS},
               "fitted + prior, half and half": {g: .5 * fitted[g] + .5 * PRIOR[g] for g in GROUPS},
               "shipped SLOT_WEIGHTS": t10.SLOT_WEIGHTS}
    for name, w in schemes.items():
        c = composite(F, w)
        print(f"{name:56s} {score(F, c, 'y'):6.3f} {score(F, c, 'ynext'):6.3f}")
    p = loso_fit(F, GROUPS)
    print(f"{'v4.20 layout, fitted leave-one-season-out':56s} {score(F, pd.Series(p), 'y'):6.3f}")
    print(f"{'team stats half alone (for scale)':56s} {score(F, F['sh'], 'y'):6.3f} {score(F, F['sh'], 'ynext'):6.3f}")
    print("\nweights (sum 1) and per player:")
    tbl = pd.DataFrame({k: pd.Series(v) for k, v in schemes.items() if k != "v4.20 layout, football prior"}).round(3)
    tbl["per player (shipped)"] = [round(t10.SLOT_WEIGHTS[g] / cnt.get(g, 1), 3) for g in tbl.index]
    print(tbl.to_string())
    print("\nstarter share (fitted weights): " + ", ".join(
        f"{sh:.2f} -> {score(build(R, pub, Y, sh, 0.0), composite(build(R, pub, Y, sh, 0.0), fitted), 'y'):.3f}" for sh in (0.80, 0.85, 0.90)))
    print("rating floor (fitted weights): " + ", ".join(
        f"{fl:.0f} -> {score(build(R, pub, Y, t10.STARTER_SHARE, fl), composite(build(R, pub, Y, t10.STARTER_SHARE, fl), fitted), 'y'):.3f}" for fl in (0, 40, 55)))
    # does the position-weighted half add to the slot score? (it also counts the line unit, so the two overlap)
    rows = []
    for r in pub.itertuples():
        lists = R.get((r.season, r.team))
        if r.season < 2016 or lists is None or (r.season, r.team) not in Y:
            continue
        lu = r.lu if r.lu == r.lu else None
        slots = t10.slot_score(lists, lu)
        ros = t10.compute_roster_splits({g: [x for x in xs if x >= 55.0] for g, xs in lists.items()}, lu)
        weighted = t10.blend([(ros["pass_off"], t10.W_PASS_OFF), (ros["run_off"], t10.W_RUN_OFF), (ros["pass_def"], t10.W_PASS_DEF),
                              (ros["run_def"], t10.W_RUN_DEF), (ros["special"], t10.W_SPEC)])
        if slots is None or weighted is None:
            continue
        rows.append({"season": r.season, "y": Y[(r.season, r.team)], "ynext": Y.get((r.season + 1, r.team)), "slots": t10.roster_to_ovr(slots),
                     "weighted": t10.roster_to_ovr(weighted)})
    H = pd.DataFrame(rows)
    H["mix"] = 0.6 * H["slots"] + 0.4 * H["weighted"]
    print("\nposition-weighted half (it counts the line unit again):")
    for name in ("slots", "weighted", "mix"):
        print(f"   {name:10s} {score(H, H[name], 'y'):6.3f} {score(H, H[name], 'ynext'):6.3f}")
    gap = max(abs(t10.SLOT_WEIGHTS[g] - schemes["fitted + equal, half and half"][g]) for g in GROUPS)
    print(f"\nlargest gap between a shipped weight and the half-and-half mix: {gap:.3f}")
    return 1 if gap > 0.02 else 0


if __name__ == "__main__":
    sys.exit(main())
