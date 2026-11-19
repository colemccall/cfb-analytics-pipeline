"""The earned order under variants: what each would do to a season's ranking, and what it costs on the backtest. Read-only, 0 requests.

Written for the owner's question of 2026-11-19 (why Boise State sits above one-loss Texas Tech, Wake Forest and Indiana, and whether
the SEC is being treated fairly) and kept so a proposed tweak is measured here before it is made (v4.27.4). Runs in about 6 minutes.

    python scripts/validate_earned_variants.py

(1) Reproduce 2026 earned (through week 11) and check it against the live file.
(2) Per-game breakdown: value of each result, opponent's earned, home edge, and the 'game score' each game contributes.
(3) Variants on 2026: margin cap, win bonus, ridge, FCS games pooled in, conference term on. Ranks of the teams in question.
(4) Each variant's backtest, 2016-2025: rate on weeks <= 10, predict winners of later regular-season FBS games (leave one season out).
"""
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
APP = ROOT.parent / "cfb-analytics-app" / "data"
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("ver", ROOT / "scripts" / "validate_earned_ranking.py")
ver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ver)
rr, conf, NAMES = ver.rr, ver.conf, ver.NAMES
SID = {v: k for k, v in NAMES.items()}
FCS = -1


def with_fcs(games, season):
    """Games where one side is not FBS get that side as one pooled team FCS (-1); games with no FBS side dropped."""
    out = []
    for x in games:
        h, a = x.get("home_team_id"), x.get("away_team_id")
        hf = h is not None and h == h and (int(h), season) in conf
        af = a is not None and a == a and (int(a), season) in conf
        if hf or af:
            y = dict(x)
            if not hf:
                y["home_team_id"] = FCS
            if not af:
                y["away_team_id"] = FCS
            out.append(y)
    return out


def earned(games, season, cap=rr.EARNED_CAP, bonus=rr.EARNED_BONUS, lam=rr.LAM, lam_conf=rr.LAM_CONF, fcs=False):
    """({team: earned number}, solve info) for one variant of the earned solve; fcs=True adds one pooled FCS team."""
    ids = ver.fbs(season) + ([FCS] if fcs else [])
    g = with_fcs(games, season) if fcs else games
    cf = lambda t: (None if t == FCS else conf.get((int(t), season)))  # noqa: E731
    parts, info = rr.power_components(g, {t: 0.0 for t in ids}, cf, cap=float(cap), bonus=float(bonus), lam=lam, lam_conf=lam_conf)
    return {t: p["power"] for t, p in parts.items() if p["games"] and t != FCS}, info


VARIANTS = [
    ("shipped (cap 7, bonus 12, ridge 2)", {}),
    ("cap 0: wins and losses only", dict(cap=0)),
    ("cap 14: margin counts to two scores", dict(cap=14)),
    ("cap 21", dict(cap=21)),
    ("bonus 8", dict(bonus=8)),
    ("bonus 16", dict(bonus=16)),
    ("ridge 4 (more shrink)", dict(lam=4.0)),
    ("ridge 1 (less shrink)", dict(lam=1.0)),
    ("FCS games counted (one pooled FCS team)", dict(fcs=True)),
    ("conference term on (ridge 8)", dict(lam_conf=8.0)),
    ("conference term on (ridge 3)", dict(lam_conf=3.0)),
    ("cap 14 + conference term (ridge 8)", dict(cap=14, lam_conf=8.0)),
]
WATCH = ["Boise State", "Texas Tech", "Wake Forest", "Indiana", "Oregon", "Memphis", "Utah", "Georgia", "Miami", "LSU", "Oklahoma",
         "South Carolina", "Florida", "Ole Miss", "Alabama", "Texas"]

season = 2026
g26 = [x for x in ver.reg(season) if x.get("week") is not None and x["week"] <= 11]
E, info = earned(g26, season)
live = {t["team_id"]: t["earned_rating"] for t in json.load(open(APP / "live_2026.json"))["teams"] if t.get("earned_rating") is not None}
d = [abs(E[t] - live[t]) for t in E if t in live]
print(f"(1) 2026 through week 11: {len(E)} teams; largest gap to the live file {max(d):.3f} (mean {np.mean(d):.4f}); home edge {info['home_edge']:.2f}\n")


def rank(S):
    o = sorted(S, key=lambda t: -S[t])
    return {t: i + 1 for i, t in enumerate(o)}, o


R0, O0 = rank(E)
W, L, _ = ver.record(g26)

print("(2) per game: value = capped margin + 12 for a win / - 12 for a loss; game score = value + opponent's earned - home edge (+ if away)")
print("    a team's earned is about the sum of its game scores over (games + 2), the 2 being the ridge's pull to 0\n")
for name in ("Boise State", "Texas Tech", "Wake Forest", "Indiana"):
    t = SID[name]
    rows = []
    for x in g26:
        p = ver.vos.fbs_pair(x, conf, season)
        if not p or t not in p:
            continue
        home = p[0] == t
        opp = p[1] if home else p[0]
        m = ver.margin(x) * (1 if home else -1)
        val = rr.game_value(m, rr.EARNED_CAP, rr.EARNED_BONUS)
        he = 0.0 if x.get("neutral_site") is True else info["home_edge"]
        gs = val + E.get(opp, 0) - (he if home else -he)
        rows.append((x["week"], ("vs " if home else "@ ") + NAMES[opp], m, val, E.get(opp, 0), R0.get(opp), gs))
    tot = sum(r[6] for r in rows)
    print(f"  {name}: earned {E[t]:+.1f} (#{R0[t]}), {W[t]}-{L[t]} in FBS games; sum of game scores {tot:+.1f} over {len(rows)} + 2 = {tot / (len(rows) + 2):+.1f}")
    for wk, opp, m, val, oe, orank, gs in sorted(rows):
        print(f"     wk{wk:>2} {opp:<22} {m:+4.0f}  value {val:+5.1f}  opp earned {oe:+5.1f} (#{orank:>3})  game score {gs:+6.1f}")
    print()

def backtest(kw, cut=10):
    """(winner share, mean miss, games) of a variant: rate on weeks <= cut, later regular-season FBS games, 2016-2025, leave one season out."""
    rows = []
    for s in ver.SEASONS:
        g = ver.reg(s)
        early = [x for x in g if x["week"] <= cut]
        S, _ = earned(early, s, **kw)
        _, _, G = ver.record(early)
        rows += ver.later_rows(S, [x for x in g if x["week"] > cut], s, G)
    acc, mae, _ = ver.loso(rows, ["gap", "home"])
    return acc, mae, len(rows)


print("(3)+(4) variants: 2026 ranks through week 11, and the backtest (rate weeks 1-10, later FBS games, 2016-2025, leave one season out)")
hdr = "".join(f"{n[:10]:>11}" for n in WATCH[:8])
print(f"  {'variant':<42}{'winners':>8}{'miss':>7}  {hdr}")
for label, kw in VARIANTS:
    S, _ = earned(g26, season, **kw)
    R, O = rank(S)
    acc, mae, n = backtest(kw)
    print(f"  {label:<42}{acc:8.2%}{mae:7.2f}  " + "".join(f"{R.get(SID[n]):>11}" for n in WATCH[:8]))
print(f"  ({n} later games in the backtest)")
print("\n  the SEC teams and the rest, same variants:")
print(f"  {'variant':<42}" + "".join(f"{n[:10]:>11}" for n in WATCH[8:]))
for label, kw in VARIANTS:
    S, _ = earned(g26, season, **kw)
    R, _ = rank(S)
    print(f"  {label:<42}" + "".join(f"{R.get(SID[n]):>11}" for n in WATCH[8:]))
print("\n  shipped top 25:", ", ".join(f"{i + 1} {NAMES[t]} {W[t]}-{L[t]}" for i, t in enumerate(O0[:25])))


# (5) what the conference term would estimate: each conference's level and its record against other FBS conferences
from collections import Counter  # noqa: E402

cf = lambda t: conf.get((int(t), season))  # noqa: E731
_, info8 = rr.power_components(g26, {t: 0.0 for t in ver.fbs(season)}, cf, cap=rr.EARNED_CAP, bonus=rr.EARNED_BONUS, lam_conf=8.0)
ooc, oocw = Counter(), Counter()
for x in g26:
    p = ver.vos.fbs_pair(x, conf, season)
    if p and cf(p[0]) != cf(p[1]):
        m = ver.margin(x)
        for c, won in ((cf(p[0]), m > 0), (cf(p[1]), m < 0)):
            ooc[c] += 1
            oocw[c] += int(won)
print()
print("(5) conference levels the term would estimate (ridge 8), with each conference's record against other FBS conferences:")
for c, v in sorted(info8["conference_levels"].items(), key=lambda kv: -kv[1]):
    print(f"   {c:<22} {v:+5.1f}   {oocw[c]}-{ooc[c] - oocw[c]} out of conference")
