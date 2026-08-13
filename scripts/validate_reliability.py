"""How much of a rating is signal, and how much is one season of luck?

Read-only apart from one export for the site.

Everything this project publishes downstream of a rating is bounded by a number
nobody here had ever measured: how well the rating agrees with *itself*. A
projection cannot track next season better than the rating tracks its own other
half, and a formula whose inputs disagree with themselves cannot be repaired by
re-weighting them — which is exactly what the v4.3 defensive work found the hard
way, three times over.

**The measurement.** Build every player's season composite twice, from his
odd-numbered weeks and his even-numbered weeks, using script 06's own per-game
weights. Correlate the halves within position. Spearman-Brown corrects a
half-length reliability up to what a full season's would be:

    r_full = 2r / (1 + r)

and the square root of that is the **noise ceiling** — the highest correlation
this measurement could possibly have with any perfectly measured truth, including
EA, the draft, or the player's own next season.

**What it separates.** A low number means one of two things and the split matters:

  · the player genuinely varies week to week — real, and the rating should be
    humble about him rather than pretend otherwise;
  · the inputs are too thin to measure anyone at that position reliably.

Both are the same instruction for us — widen the interval, do not add machinery —
but only the second is a defect we could ever fix, and fixing it needs a new
measurement rather than a new formula.

**What it cannot support.** This is the reliability of the *production composite*,
not of the shipped OVR. The OVR additionally passes through anchors, playing-time
tiers and a recruiting blend, all of which shrink noisy players toward a prior and
therefore make the published number more stable than its inputs. Read this as the
ceiling on the production signal, not as the error bar on a player card.

Two halves of a season are also not two independent samples of the same player: an
injury, a coordinator change or a quarterback change lands in one half and not the
other. That inflates nothing — it makes the number conservative.

Usage:
    python scripts/validate_reliability.py
    python scripts/validate_reliability.py --from-season 2016 --min-games 3
    python scripts/validate_reliability.py --no-export
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.json_utils import write_json
from utils.store import read_raw, read_ratings

OUTPUT_PATH = (
    Path(__file__).parent.parent.parent
    / "cfb-analytics-app" / "data" / "reliability.json"
)

# Script 06's composite weights, kept in the same shape as the source so a change
# there is visible as a diff here. If they drift apart this measures a formula we
# no longer ship.
OFF_COMPOSITE = {
    "QB": {"passingYDS": 1.0, "rushingYDS": 0.7, "passingTD": 25.0,
           "rushingTD": 20.0, "passingINT": -20.0},
    "RB": {"rushingYDS": 1.0, "receivingYDS": 0.9, "rushingTD": 20.0, "receivingTD": 20.0},
    "WR": {"receivingYDS": 1.0, "receivingTD": 25.0, "receivingREC": 2.0},
    "TE": {"receivingYDS": 1.0, "receivingTD": 25.0, "receivingREC": 2.5},
}

DEF_COMPOSITE = {
    "EDGE": {"defensiveSACKS": 7.0, "defensiveQB HUR": 2.5, "defensiveTFL": 4.0,
             "interceptionsINT": 4.0, "defensivePD": 1.5, "fumblesREC": 4.0, "_tackle": 0.3},
    "DL":   {"defensiveSACKS": 6.0, "defensiveQB HUR": 2.0, "defensiveTFL": 4.0,
             "interceptionsINT": 3.0, "defensivePD": 0.5, "fumblesREC": 4.0, "_tackle": 0.4},
    "LB":   {"defensiveSACKS": 5.5, "defensiveQB HUR": 1.5, "defensiveTFL": 4.0,
             "interceptionsINT": 7.0, "defensivePD": 2.0, "fumblesREC": 4.5, "_tackle": 0.6},
    "CB":   {"defensiveSACKS": 2.5, "defensiveQB HUR": 1.0, "defensiveTFL": 2.0,
             "interceptionsINT": 12.0, "defensivePD": 2.0, "fumblesREC": 5.0, "_tackle": 0.3},
    "S":    {"defensiveSACKS": 3.0, "defensiveQB HUR": 1.5, "defensiveTFL": 3.0,
             "interceptionsINT": 10.0, "defensivePD": 2.0, "fumblesREC": 5.0, "_tackle": 0.4},
    "DB":   {"defensiveSACKS": 2.5, "defensiveQB HUR": 1.0, "defensiveTFL": 2.0,
             "interceptionsINT": 11.0, "defensivePD": 2.0, "fumblesREC": 5.0, "_tackle": 0.3},
}

SOLO_MULT, ASSIST_MULT = 1.25, 0.65

POSITION_ORDER = ["QB", "RB", "WR", "TE", "EDGE", "DL", "LB", "CB", "S", "DB"]


def _f(d: dict, key: str) -> float:
    """Stat values arrive as strings, and some ('25-38') carry a pair."""
    v = d.get(key)
    if v is None:
        return 0.0
    try:
        return float(str(v).split("-")[0])
    except (TypeError, ValueError):
        return 0.0


def game_composite(data: dict, pg: str) -> float:
    """One game's production composite, exactly as script 06 builds it."""
    if pg in OFF_COMPOSITE:
        return sum(w * _f(data, k) for k, w in OFF_COMPOSITE[pg].items())
    w = DEF_COMPOSITE[pg]
    tot = _f(data, "defensiveTOT")
    solo = min(_f(data, "defensiveSOLO"), tot)
    tackle_credit = solo * SOLO_MULT + max(tot - solo, 0.0) * ASSIST_MULT
    return tackle_credit * w["_tackle"] + sum(
        v * _f(data, k) for k, v in w.items() if k != "_tackle")


def build_halves(from_season: int, min_games: int) -> pd.DataFrame:
    """Per player-season: mean composite over odd weeks and over even weeks.

    Split on the WEEK, not on the row order. Row order follows whatever the harvest
    happened to write, so it can correlate with the team a player faced; the week is
    a property of the schedule and splits opponents evenly by construction.
    """
    ps = read_raw("player_seasons")[["id", "player_id", "season", "position_group"]]
    ps = ps[ps["position_group"].isin(POSITION_ORDER) & (ps["season"] >= from_season)]
    meta = {int(r.id): (int(r.season), r.position_group) for r in ps.itertuples()}

    games = read_raw("games")[["id", "week"]]
    week_of = {int(r.id): int(r.week) for r in games.itertuples() if pd.notna(r.week)}

    stats_path = Path(__file__).parent.parent / "data" / "raw" / "stats.json"
    print(f"  reading {stats_path.name} ({stats_path.stat().st_size / 1e6:.0f} MB)...", flush=True)
    with open(stats_path, encoding="utf-8") as fh:
        rows = json.load(fh)

    acc: dict = defaultdict(lambda: {0: 0.0, 1: 0.0, "n0": 0, "n1": 0})
    for r in rows:
        if r.get("stat_type") != "game_aggregate":
            continue
        psid = r.get("player_season_id")
        m = meta.get(psid)
        if m is None:
            continue
        gid = r.get("game_id")
        if gid is None or pd.isna(gid):
            continue
        week = week_of.get(int(gid))
        if week is None:
            continue
        half = week % 2
        a = acc[psid]
        a[half] += game_composite(r.get("data") or {}, m[1])
        a[f"n{half}"] += 1
    del rows

    out = []
    for psid, a in acc.items():
        if a["n0"] < min_games or a["n1"] < min_games:
            continue
        season, pg = meta[psid]
        out.append({"player_season_id": psid, "season": season, "position_group": pg,
                    "odd": a[1] / a["n1"], "even": a[0] / a["n0"],
                    "games": a["n0"] + a["n1"]})
    return pd.DataFrame(out)


def reliability_table(H: pd.DataFrame, min_n: int = 100) -> pd.DataFrame:
    out = []
    for pg in POSITION_ORDER:
        g = H[H["position_group"] == pg]
        if len(g) < min_n:
            continue
        r_half = float(g["odd"].corr(g["even"], method="spearman"))
        r_full = 2 * r_half / (1 + r_half) if r_half > -1 else float("nan")
        out.append({"pos": pg, "n": len(g),
                    "split_half": round(r_half, 3),
                    "reliability": round(r_full, 3),
                    "noise_ceiling": round(float(np.sqrt(max(r_full, 0.0))), 3)})
    return pd.DataFrame(out)


def persistence_table(reliab: dict) -> pd.DataFrame:
    """Year-over-year rank persistence of the shipped OVR, beside the ceiling on it.

    The ratio is the number to read. A position near 1.0 is already extracting
    everything its measurement allows, so no feature will help it and only a new
    measurement would; a position well below 1.0 has modelling headroom left.
    """
    ps = read_raw("player_seasons")[["id", "player_id", "season", "position_group"]] \
        .rename(columns={"id": "player_season_id"})
    rat = read_ratings("edge")[["player_season_id", "overall_rating"]]
    D = ps.merge(rat, on="player_season_id", how="inner").dropna(subset=["overall_rating"])
    D = D[D["position_group"].isin(POSITION_ORDER)]
    # A player who changed teams mid-season has two rows; his best is his season.
    D = D.sort_values("overall_rating", ascending=False) \
         .drop_duplicates(["player_id", "season"], keep="first") \
         .sort_values(["player_id", "season"])
    D["ovr_next"] = D.groupby("player_id")["overall_rating"].shift(-1)
    D["season_next"] = D.groupby("player_id")["season"].shift(-1)
    D = D[D["season_next"] == D["season"] + 1]

    out = []
    for pg in POSITION_ORDER:
        g = D[D["position_group"] == pg]
        if len(g) < 300 or pg not in reliab:
            continue
        persist = float(g["overall_rating"].corr(g["ovr_next"], method="spearman"))
        ceiling = reliab[pg]
        out.append({"pos": pg, "pairs": len(g),
                    "persistence": round(persist, 3),
                    "reliability": round(ceiling, 3),
                    "share_of_ceiling": round(persist / ceiling, 3) if ceiling > 0 else None})
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Split-half reliability of the rating inputs")
    ap.add_argument("--from-season", type=int, default=2016,
                    help="first season to include (2016+ is when the modern defensive "
                         "stat set is complete; earlier seasons measure a different formula)")
    ap.add_argument("--min-games", type=int, default=3,
                    help="minimum games in EACH half")
    ap.add_argument("--starters-only", action="store_true",
                    help="restrict to 8+ games — the population a rating is actually read for")
    ap.add_argument("--no-export", action="store_true")
    args = ap.parse_args()

    print(f"\n=== split-half reliability, {args.from_season}-present, "
          f">={args.min_games} games per half ===\n")
    H = build_halves(args.from_season, args.min_games)
    if H.empty:
        print("No player-seasons met the criteria.")
        sys.exit(1)
    if args.starters_only:
        H = H[H["games"] >= 8]
    print(f"  {len(H)} player-seasons\n")

    table = reliability_table(H)
    print(table.to_string(index=False))
    print("\n  reliability   = Spearman-Brown, what a full season's agreement would be")
    print("  noise_ceiling = sqrt(reliability); no comparison against ANY external truth")
    print("                  can exceed it, however good that truth is.\n")

    reliab = dict(zip(table["pos"], table["reliability"]))
    pers = persistence_table(reliab)
    if not pers.empty:
        print("=== year-over-year persistence of the shipped OVR, against that ceiling ===\n")
        print(pers.to_string(index=False))
        print("\n  share_of_ceiling near 1.0 = the position is already measured as well as")
        print("  this data allows, and its unpredictability is measurement, not change.\n")

    if args.no_export:
        return
    write_json(OUTPUT_PATH, {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "from_season": args.from_season,
        "min_games_per_half": args.min_games,
        "starters_only": bool(args.starters_only),
        "positions": table.to_dict(orient="records"),
        "persistence": pers.to_dict(orient="records") if not pers.empty else [],
    })
    print(f"  Wrote {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
