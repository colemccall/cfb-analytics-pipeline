"""The earned order: where its two constants came from, and what it costs. Read-only (v4.27).

The owner's objection was not that power predicts badly. It was that a ranking should not put a two-loss team that lost head to head
in the top 5, or a three-loss team in the top 15. So this does two things and keeps them apart.

  (1) What the record and the head-to-head add to prediction, at equal points. Nothing here is used to choose the constants.
  (2) A sweep of margin cap x win bonus on power_components with a flat rating part, and which settings meet the owner's four conditions
      for 2026 through week 10: Alabama outside the top 5, LSU outside the top 15, Liberty and New Mexico outside the top 15.
      THE SETTINGS WERE SEARCHED FOR THAT ANSWER, so the fit to 2026 is evidence of nothing. What can be read is the backtest on 2016-2025
      (no 2020): cut after weeks 8 and 10, every later regular-season FBS game, and how the order behaves in other seasons.

Dumb baselines are printed beside it: always the home team, and the better record. The committee's field is a reference only, never an
input. Runs in about 8 minutes, 0 requests.

    python scripts/validate_earned_ranking.py
"""

import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils import results_rating as rr  # noqa: E402
from utils.store import read_raw  # noqa: E402

_s = importlib.util.spec_from_file_location("vos", ROOT / "scripts" / "validate_opponent_strength.py")
vos = importlib.util.module_from_spec(_s)
_s.loader.exec_module(vos)

CAPS = [0, 3, 5, 7, 10, 14, 17, 21, 28]
BONUSES = [4, 6, 8, 10, 12, 14, 17, 20, 24, 28, 35, 50]
CONDITIONS = ("Alabama", "LSU", "Liberty", "New Mexico")

by, conf = vos.load()
SEASONS = vos.SEASONS
_teams = read_raw("teams")
NAMES = dict(zip(_teams["id"], _teams["school"]))
SID = {v: k for k, v in NAMES.items()}
BIG = {"SEC", "Big Ten", "Big 12", "ACC"}


def fbs(season):
    return [t for (t, s) in conf if s == season]


def reg(season):
    return [x for x in by.get(season, []) if x["season_type"] == "regular"]


def margin(x):
    return float(x["home_score"]) - float(x["away_score"])


def hv(x):
    return 0.0 if x.get("neutral_site") is True else 1.0


def power(games, season, cap, bonus):
    parts, _ = rr.power_components(games, {t: 0.0 for t in fbs(season)}, lambda t: conf.get((int(t), season)), cap=float(cap), bonus=float(bonus))
    return {t: p["power"] for t, p in parts.items()}


def record(games):
    """(wins, losses, games) by team over `games`, Counters."""
    W, L, G = Counter(), Counter(), Counter()
    for x in games:
        m = margin(x)
        for key, s in (("home_team_id", 1), ("away_team_id", -1)):
            t = x.get(key)
            if t is None or t != t:
                continue
            t = int(t)
            G[t] += 1
            W[t] += int(s * m > 0)
            L[t] += int(s * m < 0)
    return W, L, G


def meetings(games):
    """(winner, loser) for every game that was not a tie."""
    out = []
    for x in games:
        h, a = x.get("home_team_id"), x.get("away_team_id")
        if h is None or a is None or h != h or a != a:
            continue
        m = margin(x)
        if m:
            out.append((int(h), int(a)) if m > 0 else (int(a), int(h)))
    return out


def inversions(order, games, top=25):
    """Pairs inside the top N where the team ranked higher lost the only meeting (a split series does not count)."""
    rk = {t: i for i, t in enumerate(order)}
    head = set(order[:top])
    wins = Counter(meetings(games))
    return [(w, l) for (w, l), n in wins.items() if w in head and l in head and rk[l] < rk[w] and wins.get((l, w), 0) < n]


def later_rows(S, late, season, G):
    """One row per later FBS game: the strength gap, home edge and margin."""
    rows = []
    for x in late:
        p = vos.fbs_pair(x, conf, season)
        if p and p[0] in S and p[1] in S and G[p[0]] and G[p[1]]:
            rows.append({"season": season, "h": p[0], "a": p[1], "gap": S[p[0]] - S[p[1]], "home": hv(x), "y": margin(x)})
    return rows


def loso(rows, feats):
    """(winner share, mean miss, full-sample coefficients) of margin on `feats`, leave one season out."""
    y = np.array([r["y"] for r in rows])
    X = np.array([[r[f] for f in feats] for r in rows])
    ss = np.array([r["season"] for r in rows])
    pred = np.zeros(len(y))
    for s in SEASONS:
        tr, te = ss != s, ss == s
        if te.sum():
            pred[te] = X[te] @ np.linalg.lstsq(X[tr], y[tr], rcond=None)[0]
    nz = y != 0
    return float(np.mean(np.sign(pred[nz]) == np.sign(y[nz]))), float(np.mean(np.abs(y - pred))), np.linalg.lstsq(X, y, rcond=None)[0]


def losses_beyond_points(rng):
    """Print what the loss difference adds to the power gap on later games, with a placebo that shuffles losses across teams."""
    print("(1a) Does the loss column predict later games beyond points? margin ~ gap + home + (loss difference), leave one season out")
    for cut in (8, 10):
        rows, L_by = [], {}
        for season in SEASONS:
            g = reg(season)
            early = [x for x in g if x["week"] <= cut]
            S = power(early, season, rr.CAP, rr.BONUS)
            _, L, G = record(early)
            L_by[season] = L
            for r in later_rows(S, [x for x in g if x["week"] > cut], season, G):
                r["ld"] = float(L[r["h"]] - L[r["a"]])
                rows.append(r)
        a0, m0, _ = loso(rows, ["gap", "home"])
        a1, m1, c = loso(rows, ["gap", "home", "ld"])
        y = np.array([r["y"] for r in rows])
        base = np.array([[r["gap"], r["home"]] for r in rows])
        sims = []
        for _ in range(400):
            maps = {}
            for season, L in L_by.items():
                teams = sorted({r["h"] for r in rows if r["season"] == season} | {r["a"] for r in rows if r["season"] == season})
                vals = np.array([L[t] for t in teams], float)
                rng.shuffle(vals)
                maps[season] = dict(zip(teams, vals))
            ld = np.array([maps[r["season"]][r["h"]] - maps[r["season"]][r["a"]] for r in rows])
            sims.append(np.linalg.lstsq(np.c_[base, ld], y, rcond=None)[0][2])
        sims = np.array(sims)
        print(f"  cut week {cut:>2}: {len(rows)} games. points only {a0:.4f} / {m0:.3f}; with losses {a1:.4f} / {m1:.3f}; points per extra loss {c[2]:+.2f} "
              f"(positive: power already counts a loss for more than it predicts); shuffled {sims.mean():+.2f}, sd {sims.std():.2f}")


def head_to_head(rng):
    """Print how the head-to-head winner did afterwards against a team ranked above it, with a sign-flip placebo."""
    print("\n(1b) A team ranked above one that beat it, top 25 on power: which did better afterwards? d = the winner's later error minus the loser's")
    for cut in (8, 10):
        ds = []
        allrows = []
        cache = {}
        for season in SEASONS:
            g = reg(season)
            early = [x for x in g if x["week"] <= cut]
            S = power(early, season, rr.CAP, rr.BONUS)
            _, _, G = record(early)
            cache[season] = (early, [x for x in g if x["week"] > cut], S, G)
            allrows += later_rows(S, cache[season][1], season, G)
        for season, (early, late, S, G) in cache.items():
            tr = [r for r in allrows if r["season"] != season]
            w = np.linalg.lstsq(np.array([[r["gap"], r["home"]] for r in tr]), np.array([r["y"] for r in tr]), rcond=None)[0]
            res = defaultdict(list)
            for r in later_rows(S, late, season, G):
                e = r["y"] - (w[0] * r["gap"] + w[1] * r["home"])
                res[r["h"]].append(e)
                res[r["a"]].append(-e)
            for wn, ls in inversions(sorted(S, key=lambda t: -S[t]), early):
                if res[wn] and res[ls]:
                    ds.append(float(np.mean(res[wn]) - np.mean(res[ls])))
        x = np.array(ds)
        flips = np.array([np.mean(x * rng.choice([-1, 1], len(x))) for _ in range(4000)])
        print(f"  cut week {cut:>2}: {len(x)} pairs, mean d {x.mean():+.2f} (s.e. {x.std(ddof=1) / np.sqrt(len(x)):.2f}), sign-flip placebo p = {np.mean(np.abs(flips) >= abs(x.mean())):.3f}")


def group_tests(rng):
    """Did teams like the ones the owner objected to play below their number afterwards? Week 10, 2016-2025, later regular-season FBS games."""
    from utils import opponent_strength as os_

    cut = 10

    def later_errors(ranker):
        """{season: (teams' later errors, the ranker's strengths)}; each error is on the team's side, slope and home edge from the other seasons."""
        cache, allrows = {}, []
        for season in SEASONS:
            g = reg(season)
            early, late = [x for x in g if x["week"] <= cut], [x for x in g if x["week"] > cut]
            S = ranker(early, season)
            G = record(early)[2]
            cache[season] = (early, late, S, G)
            allrows += later_rows(S, late, season, G)
        out = {}
        for season, (early, late, S, G) in cache.items():
            tr = [r for r in allrows if r["season"] != season]
            w = np.linalg.lstsq(np.array([[r["gap"], r["home"]] for r in tr]), np.array([r["y"] for r in tr]), rcond=None)[0]
            res = defaultdict(list)
            for r in later_rows(S, late, season, G):
                e = r["y"] - (w[0] * r["gap"] + w[1] * r["home"])
                res[r["h"]].append(e)
                res[r["a"]].append(-e)
            out[season] = (early, S, res)
        return out

    def show(label, recs):
        if not recs:
            print(f"    {label:<46} none")
            return
        n = np.array([r["n"] for r in recs])
        v = np.array([r["res"] for r in recs])
        print(f"    {label:<46} {len(recs):>3} team-seasons {int(n.sum()):>4} games: {float(np.average(v, weights=n)):+.2f} (s.e. about {16 / np.sqrt(n.sum()):.2f})")

    print("\n(1c) Later-game error by loss count at week 10, on power (positive: played better than rated)")
    recs = []
    for season, (early, S, res) in later_errors(lambda g, s: power(g, s, rr.CAP, rr.BONUS)).items():
        L = record(early)[1]
        rk = {t: i + 1 for i, t in enumerate(sorted(S, key=lambda t: -S[t]))}
        recs += [{"rk": rk[t], "L": L[t], "n": len(v), "res": float(np.mean(v))} for t, v in res.items() if rk[t] <= 15 and v]
    show("top 5, two losses", [r for r in recs if r["rk"] <= 5 and r["L"] == 2])
    show("top 15, three or more losses", [r for r in recs if r["L"] >= 3])
    show("top 5, zero or one loss", [r for r in recs if r["rk"] <= 5 and r["L"] <= 1])

    def strength(games, season, **kw):
        conf_of = lambda t, s=season: conf.get((int(t), s)) if t != os_.FCS else None   # noqa: E731
        o = os_.solve(games, conf_of, fbs(season), **kw)
        return {t: o[t]["off"] + o[t]["dfn"] for t in fbs(season) if t in o}

    print("\n(1d) A top-25 team on the results reading that had played a top-30 team (no conference level) and beaten none. The label was chosen after seeing LSU's schedule")
    team = []
    for season, (early, S, res) in later_errors(strength).items():
        neutral = strength(early, season, lam_conf=1e9)
        good = set(sorted(neutral, key=lambda t: -neutral[t])[:30])
        top = sorted(S, key=lambda t: -S[t])[:25]
        info = {t: {"gw": 0, "gg": 0} for t in top}
        for w, l in meetings(early):
            for t, o, won in ((w, l, True), (l, w, False)):
                if t in info and o in good:
                    info[t]["gg"] += 1
                    info[t]["gw"] += int(won)
        team += [{"season": season, "n": len(res[t]), "res": float(np.mean(res[t])), **info[t]} for t in top if res[t]]
    flags0 = [r["gw"] == 0 and r["gg"] >= 1 for r in team]
    show("played one, beat none", [r for r, f in zip(team, flags0) if f])
    show("the rest", [r for r, f in zip(team, flags0) if not f])

    def diff(flags):
        a = [(r["res"], r["n"]) for r, f in zip(team, flags) if f]
        b = [(r["res"], r["n"]) for r, f in zip(team, flags) if not f]
        return float(np.average([x for x, _ in a], weights=[n for _, n in a]) - np.average([x for x, _ in b], weights=[n for _, n in b]))

    d0 = diff(flags0)
    by_s = defaultdict(list)
    for i, r in enumerate(team):
        by_s[r["season"]].append(i)
    sims = []
    for _ in range(4000):
        f = list(flags0)
        for ix in by_s.values():
            vals = [flags0[i] for i in ix]
            rng.shuffle(vals)
            for i, v in zip(ix, vals):
                f[i] = bool(v)
        sims.append(diff(f))
    sims = np.array(sims)
    print(f"    the difference {d0:+.2f}; label shuffled within season {sims.mean():+.2f}, p = {np.mean(np.abs(sims - sims.mean()) >= abs(d0 - sims.mean())):.3f}")


CFP = json.loads((ROOT / "data" / "raw" / "cfp_participants.json").read_text(encoding="utf-8"))


def score(cap, bonus, cuts=(8, 10)):
    """Backtest one (cap, bonus): picks, miss and how the order behaves at each cut; the committee's field as a reference."""
    out = {}
    for cut in cuts:
        rows, right, n, two, three, small, inv = [], 0, 0, 0, 0, 0, 0
        for season in SEASONS:
            g = reg(season)
            early = [x for x in g if x["week"] <= cut]
            S = power(early, season, cap, bonus)
            _, L, G = record(early)
            order = sorted(S, key=lambda t: -S[t])
            two += sum(L[t] >= 2 for t in order[:5])
            three += sum(L[t] >= 3 for t in order[:15])
            small += sum(not (conf.get((t, season)) in BIG or (conf.get((t, season)) == "Pac-12" and season <= 2023) or NAMES.get(t) == "Notre Dame") for t in order[:15])
            inv += len(inversions(order, early))
            new = later_rows(S, [x for x in g if x["week"] > cut], season, G)
            rows += new
            for r in new:
                if r["y"]:
                    n += 1
                    right += int((r["gap"] > 0) == (r["y"] > 0))
        a, m, _ = loso(rows, ["gap", "home"])
        k = len(SEASONS)
        out[cut] = {"picks": right / n, "fitted": a, "miss": m, "two": two / k, "three": three / k, "small": small / k, "inv": inv / k}
    hits = {}
    for season in (2024, 2025):
        S = power(reg(season), season, cap, bonus)
        top = set(sorted(S, key=lambda t: -S[t])[:12])
        hits[season] = sum(int(r["team_id"]) in top for r in CFP if r["season"] == season)
    return out, hits


def show(label, cap, bonus):
    o, h = score(cap, bonus)
    a, z = o[8], o[10]
    print(f"  {label:<26} wk8 {a['picks']:.4f} miss {a['miss']:.3f} | wk10 {z['picks']:.4f} miss {z['miss']:.3f} | a season at wk10: 2+L in top 5 {z['two']:.1f}, 3+L in top 15 {z['three']:.1f}, "
          f"small-conference in top 15 {z['small']:.1f}, head-to-head inversions in top 25 {z['inv']:.1f} | committee's 12 in top 12: {h[2024]}, {h[2025]}")


def main() -> None:
    """Run every section in order and print it."""
    rng = np.random.default_rng(17)
    losses_beyond_points(rng)
    head_to_head(rng)
    group_tests(rng)

    print("\n(2) 2026 through its newest complete week: ranks of " + " / ".join(CONDITIONS) + ", by cap (rows) and bonus (columns). * meets all four")
    g26 = reg(2026)
    print("cap\\bonus " + "".join(f"{b:>15}" for b in BONUSES))
    feasible = []
    for c in CAPS:
        line = f"{c:>8} "
        for b in BONUSES:
            S = power(g26, 2026, c, b)
            order = sorted(S, key=lambda t: -S[t])
            rk = {t: i + 1 for i, t in enumerate(order)}
            r = [rk[SID[n]] for n in CONDITIONS]
            ok = r[0] > 5 and r[1] > 15 and r[2] > 15 and r[3] > 15
            if ok:
                feasible.append((c, b))
            line += f"{'*' if ok else ' '}{r[0]}/{r[1]}/{r[2]}/{r[3]}".rjust(15)
        print(line)
    print(f"\n{len(feasible)} of {len(CAPS) * len(BONUSES)} settings meet all four; the largest cap among them is {max(c for c, _ in feasible)}.")

    print("\nBacktest. 'wk8' and 'wk10' are the share of later games won by the higher-ranked team, and the mean miss of a fitted margin")
    for cut in (8, 10):
        n = home = 0
        rows, rec = [], []
        for season in SEASONS:
            g = reg(season)
            early = [x for x in g if x["week"] <= cut]
            W, _, G = record(early)
            S = power(early, season, rr.CAP, rr.BONUS)
            for r in later_rows(S, [x for x in g if x["week"] > cut], season, G):
                n += 1
                home += int(r["y"] > 0)
                rec.append({**r, "gap": W[r["h"]] / G[r["h"]] - W[r["a"]] / G[r["a"]]})
        a, m, _ = loso(rec, ["gap", "home"])
        print(f"  dumb baselines, cut week {cut}: always the home team {home / n:.4f} of {n}; better record {a:.4f} / {m:.3f}")
    show(f"power ({rr.CAP:.0f} / {rr.BONUS:.0f})", rr.CAP, rr.BONUS)
    show(f"EARNED ({rr.EARNED_CAP:.0f} / {rr.EARNED_BONUS:.0f})", rr.EARNED_CAP, rr.EARNED_BONUS)
    show("win bonus 14", 28, 14)
    show("cap 14, bonus 14", 14, 14)
    show("wins and losses only", 0, 8)
    ranked = sorted(feasible, key=lambda cb: -score(*cb, cuts=(10,))[0][10]["picks"])
    print("  best of the settings that meet all four, by wk10 picks: " + ", ".join(f"cap {c} bonus {b}" for c, b in ranked[:5]))
    what_it_moves(g26)
    results_reading_variants(g26)


def what_it_moves(g26) -> None:
    """Power against earned for the newest complete week, and the same constants a week earlier."""
    newest = max(int(x["week"]) for x in g26)
    live = json.loads((ROOT / "data" / "computed" / "live_2026.json").read_text(encoding="utf-8"))
    published = {t["team_id"] for t in live["teams"] if t.get("power_rating") is not None}      # the teams the site ranks
    def order(games, cap, bonus):
        S = {t: v for t, v in power(games, 2026, cap, bonus).items() if t in published}
        o = sorted(S, key=lambda t: -S[t])
        return S, o, {t: i + 1 for i, t in enumerate(o)}
    Sp, op, rp = order(g26, rr.CAP, rr.BONUS)
    Se, oe, re = order(g26, rr.EARNED_CAP, rr.EARNED_BONUS)
    ids = list(rp)
    L = record(g26)[1]
    print(f"\n(3) 2026 through week {newest}, power to earned: {sum(abs(rp[t] - re[t]) >= 10 for t in ids)} of {len(ids)} teams move 10 or more places; "
          f"{len(set(op[:25]) & set(oe[:25]))} of the top 25 are shared; head-to-head inversions in the top 25: {len(inversions(op, g26))} and {len(inversions(oe, g26))}")
    v = sorted(Se.values(), reverse=True)
    print(f"    Alabama {Se[SID['Alabama']]:.2f} against #5 at {v[4]:.2f}; 3-loss teams in the earned top 15: {[NAMES[t] for t in oe[:15] if L[t] >= 3]}")
    prev = [x for x in g26 if x["week"] <= newest - 1]
    _, _, rk = order(prev, rr.EARNED_CAP, rr.EARNED_BONUS)
    print(f"    the same constants through week {newest - 1}: Alabama #{rk[SID['Alabama']]}, LSU #{rk[SID['LSU']]}")


def results_reading_variants(g26) -> None:
    """What was tried on the results reading (2/3 of the headline) and did not make the cut, and the SEC's level on games it was not fitted to."""
    from utils import opponent_strength as os_

    _spec = importlib.util.spec_from_file_location("t10e", ROOT / "scripts" / "10_compute_team_ratings.py")
    t10 = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(t10)

    def capped(games, cap):
        if cap is None:
            return games
        out = []
        for g in games:
            m = margin(g)
            if abs(m) > cap:
                g = dict(g)
                if m > 0:
                    g["home_score"] = float(g["away_score"]) + cap
                else:
                    g["away_score"] = float(g["home_score"]) + cap
            out.append(g)
        return out

    def solve(games, season, cap=None, **kw):
        conf_of = lambda t, s=season: conf.get((int(t), s)) if t != os_.FCS else None   # noqa: E731
        return os_.solve(capped(games, cap), conf_of, fbs(season), **kw)

    live = json.loads((ROOT / "data" / "computed" / "live_2026.json").read_text(encoding="utf-8"))
    T = {t["team_id"]: t for t in live["teams"]}
    print("\n(4) Variants of the results reading: 2026 headline rank of LSU and Alabama, then every later FBS game (winner / mean miss)")
    variants = {"shipped": {}, "cap 28": {"cap": 28}, "cap 21": {"cap": 21}, "no conference level": {"lam_conf": 1e9}}
    for name, kw in variants.items():
        out = solve(by[2026], 2026, **kw)
        head = {t: (x["roster_half"] + x["stats_half"] + 4 * t10.results_to_ovr(out[t]["off"] + out[t]["dfn"])) / 6
                for t, x in T.items() if t in out and x["roster_half"] is not None and x["stats_half"] is not None}
        rk = {t: i + 1 for i, t in enumerate(sorted(head, key=lambda t: -head[t]))}
        cells = []
        for cut in (8, 10):
            rows = []
            for season in SEASONS:
                g = reg(season)
                o = solve([x for x in g if x["week"] <= cut], season, **kw)
                for x in g:
                    if x["week"] <= cut:
                        continue
                    p = vos.fbs_pair(x, conf, season)
                    gap = os_.strength_gap(o, *p) if p else None
                    if gap is not None:
                        rows.append({"season": season, "gap": gap, "home": hv(x), "y": margin(x)})
            a, m, _ = vos.score(rows)
            cells.append(f"wk{cut} {a:.4f} / {m:.3f}")
        print(f"  {name:<22} LSU #{rk[SID['LSU']]}, Alabama #{rk[SID['Alabama']]} | " + " | ".join(cells))
    print("\n    the league level on cross-conference postseason games (regular season fitted, leave one season out), mean error by conference, team's side:")
    rows = []
    for season in SEASONS:
        o = solve(reg(season), season)
        for x in by.get(season, []):
            if x["season_type"] == "regular":
                continue
            p = vos.fbs_pair(x, conf, season)
            if not p or conf.get((p[0], season)) == conf.get((p[1], season)):
                continue
            gap = os_.strength_gap(o, *p)
            if gap is not None:
                rows.append({"season": season, "gap": gap, "y": margin(x), "a": conf.get((p[0], season)), "b": conf.get((p[1], season))})
    res = defaultdict(list)
    for season in SEASONS:
        tr = [r for r in rows if r["season"] != season]
        k = sum(r["gap"] * r["y"] for r in tr) / sum(r["gap"] ** 2 for r in tr)
        for r in rows:
            if r["season"] == season:
                e = r["y"] - k * r["gap"]
                res[r["a"]].append(e)
                res[r["b"]].append(-e)
    print(f"      {len(rows)} games. " + "; ".join(f"{c} {np.mean(res[c]):+.2f} (s.e. {np.std(res[c]) / np.sqrt(len(res[c])):.2f}, n {len(res[c])})" for c in ("SEC", "Big Ten", "Big 12", "ACC")))
    w = l = 0
    for x in by[2026]:
        p = vos.fbs_pair(x, conf, 2026)
        if p and (conf.get((p[0], 2026)) == "SEC") != (conf.get((p[1], 2026)) == "SEC"):
            s = 1 if conf.get((p[0], 2026)) == "SEC" else -1
            w += int(s * margin(x) > 0)
            l += int(s * margin(x) < 0)
    print(f"      the SEC outside the league in 2026: {w}-{l}")


if __name__ == "__main__":
    main()
