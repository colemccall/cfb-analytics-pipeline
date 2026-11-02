"""Reassess the frozen defensive EDGE_OVR_ANCHORS after the v4.24 decimal sack / TFL fix. READ-ONLY: changes no constant.

The anchors were set (07, "v4.2 calibration") as the edge score of the Nth-best player at a position in a typical 2021-2025
season. This script keeps that definition and asks what the fix did to it:

  1. how the edge-score distribution moved, by position
  2. how many players clear each tier today (new scores, old anchors) against what shipped (old scores, old anchors)
  3. the anchors that would hold the shipped tier counts under the new scores (rank-preserving, derived once, not per season)
  4. what each choice does to the 85+/90+/96+ counts, season by season
  5. whether the new scores are the better measure of the same thing (year-over-year persistence), because moving an anchor
     is only justified if the score underneath got better, not merely larger

  python scripts/reassess_defensive_anchors.py --before ../_snapshots/v4.24-pre/raw/player_edge.json
"""
import argparse
import importlib.util
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SEASONS = range(2021, 2026)
POSITIONS = ("EDGE", "DL", "LB", "CB", "S", "DB")
TIERS = (85, 90, 96)


def load_07():
    spec = importlib.util.spec_from_file_location("s07", ROOT / "scripts" / "07_compute_player_ratings.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_edge(path, pos_of):
    by = defaultdict(lambda: defaultdict(dict))
    for r in json.load(open(path)):
        if r["season"] in SEASONS and r["edge_score"] is not None:
            pg = pos_of.get(r["player_season_id"])
            if pg in POSITIONS:
                by[pg][r["season"]][r["player_id"]] = (r["edge_score"], r.get("games_played") or 0)
    return by


def to_ovr(score, anchors):
    xs, ys = zip(*anchors)
    return float(np.interp(score, xs, ys))


def count_at(scores, anchors, tier):
    return sum(1 for s in scores if to_ovr(s, anchors) >= tier)


def kth(scores, k):
    s = sorted(scores, reverse=True)
    return s[min(max(k, 1), len(s)) - 1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True, type=Path)
    ap.add_argument("--after", type=Path, default=ROOT / "data" / "raw" / "player_edge.json")
    args = ap.parse_args()

    anchors = load_07().EDGE_OVR_ANCHORS
    pos_of = {r["id"]: r["position_group"] for r in json.load(open(ROOT / "data" / "raw" / "player_seasons.json"))}
    old, new = load_edge(args.before, pos_of), load_edge(args.after, pos_of)

    print("(1) EDGE SCORE DISTRIBUTION, 2021-2025 pooled: before -> after")
    print("    pos       n   players changed   median ratio (changed)   p90 before -> after     p99 before -> after")
    for pg in POSITIONS:
        o = {(s, p): v[0] for s in SEASONS for p, v in old[pg][s].items()}
        n = {(s, p): v[0] for s in SEASONS for p, v in new[pg][s].items()}
        both = [k for k in o if k in n]
        ch = [k for k in both if abs(n[k] - o[k]) > 1e-9]
        ratio = [n[k] / o[k] for k in ch if o[k] > 1]
        ov, nv = np.array([o[k] for k in both]), np.array([n[k] for k in both])
        print(f"    {pg:5} {len(both):6}   {len(ch)/len(both):7.1%}          {st.median(ratio) if ratio else float('nan'):8.3f}"
              f"            {np.percentile(ov,90):7.2f} -> {np.percentile(nv,90):7.2f}   {np.percentile(ov,99):7.2f} -> {np.percentile(nv,99):7.2f}")

    print("\n(2)+(3) ANCHORS: today's x, the rank each one sat at under the old scores, and the x that holds that rank under the new")
    proposed = {}
    for pg in POSITIONS:
        rows = []
        for x, y in anchors[pg][1:]:
            ranks = [sum(1 for v in old[pg][s].values() if v[0] >= x) for s in SEASONS]
            k = int(round(st.median(ranks)))
            if k < 1:       # a tip nobody reached in a median season: keep its distance above the previous anchor in score ratio
                rows.append((y, x, k, None))
                continue
            nx = st.median(kth([v[0] for v in new[pg][s].values()], k) for s in SEASONS)
            rows.append((y, x, k, nx))
        proposed[pg] = rows
        print(f"    {pg}")
        prev_ratio = None
        for y, x, k, nx in rows:
            tail = "" if nx is None else f"  new x {nx:8.2f}  ({nx / x - 1:+.1%})"
            print(f"      OVR {y:>3.0f}: x {x:7.1f}  = rank {k:>4} in a median season{tail if nx is not None else '  (rank < 1: tip nobody reached; scale by the ratio of the rank-1 scores)'}")

    print("\n(4) PLAYERS AT OR ABOVE EACH TIER PER SEASON (median of 2021-2025; range)  shipped | new scores, old anchors | new scores, rank-preserved anchors")
    for pg in POSITIONS:
        rows = proposed[pg]
        k1_ratio = st.median(
            kth([v[0] for v in new[pg][s].values()], 1) / kth([v[0] for v in old[pg][s].values()], 1) for s in SEASONS)
        keep = []
        for y, x, k, nx in rows:
            keep.append((nx if nx is not None else x * k1_ratio, y))
        prop = [(0, 30)] + [(a, b) for a, b in keep]
        for tier in TIERS:
            a = [count_at([v[0] for v in old[pg][s].values()], anchors[pg], tier) for s in SEASONS]
            b = [count_at([v[0] for v in new[pg][s].values()], anchors[pg], tier) for s in SEASONS]
            c = [count_at([v[0] for v in new[pg][s].values()], prop, tier) for s in SEASONS]
            f = lambda z: f"{st.median(z):5.0f} ({min(z)}-{max(z)})"
            print(f"    {pg:5} {tier}+   {f(a):>14} | {f(b):>14} | {f(c):>14}")

    print("\n(5) PERSISTENCE: Spearman of a player's score in season t against t+1 (both seasons 8+ games), pooled 2021-2025")
    print("    pos        pairs   before   after")
    for pg in POSITIONS:
        xs_o, xs_n, ys_o, ys_n = [], [], [], []
        for s in list(SEASONS)[:-1]:
            for p, (sc, g) in new[pg][s].items():
                nxt = new[pg][s + 1].get(p)
                if nxt and g >= 8 and nxt[1] >= 8 and p in old[pg][s] and p in old[pg][s + 1]:
                    xs_n.append(sc); ys_n.append(nxt[0])
                    xs_o.append(old[pg][s][p][0]); ys_o.append(old[pg][s + 1][p][0])
        if len(xs_n) > 30:
            print(f"    {pg:5} {len(xs_n):8}   {spearmanr(xs_o, ys_o)[0]:.4f}  {spearmanr(xs_n, ys_n)[0]:.4f}")


if __name__ == "__main__":
    main()
