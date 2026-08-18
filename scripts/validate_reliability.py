"""How much of a rating is signal, and how much is one season of luck?

Read-only apart from one export for the site.

Everything this project publishes downstream of a rating is bounded by a number
nobody here had ever measured: how well the rating agrees with *itself*. A
projection cannot track next season better than the rating tracks its own other
half, and a formula whose inputs disagree with themselves cannot be repaired by
re-weighting them — which is exactly what the v4.3 defensive work found the hard
way, three times over.

**Two quantities, and they answer different questions** (v4.5):

  composite   the per-game production composite script 06 builds. "Can a player at
              this position be measured at all."
  ovr         the same halves carried through script 07's actual mapping —
              anchors, games confidence, the playing-time tier and the recruiting
              blend. "How repeatable is the number printed on the card."

The gap between them is a direct measurement of how much work the tiers and the
recruiting blend are doing. Do not use one where the other belongs: an interval
built on the composite figure over-widens exactly the positions the blend has
already shrunk most.

**The measurement.** Build every player's season twice, from odd-numbered weeks
and even-numbered weeks. Correlate the halves within position. Spearman-Brown
corrects a half-length reliability to what a full season's would be:

    r_full = 2r / (1 + r)

and the square root of that is the **noise ceiling** — the highest correlation
this measurement could possibly have with any perfectly measured truth, including
EA, the draft, or the player's own next season.

**Reliability is repeatability, not truth** (the caveat the OVR figure needs).
Blending a production number toward a constant — a recruiting grade that does not
vary between halves — makes it *more repeatable* without making it more true. A
rating that is 100% recruiting has reliability 1.0 and no information about the
season it claims to describe. That is why the tier mix is reported beside it and
why bench-tier rows, whose OVR is a constant by construction, are excluded from
the OVR figure rather than allowed to inflate it.

**What a low number means.** Either the player genuinely varies week to week —
real, and the rating should be humble about him — or the inputs are too thin to
measure anyone at that position. Both say widen the interval rather than add
machinery, but only the second is a defect we could ever fix, and fixing it needs
a new measurement rather than a new formula. §2 (`--decompose`) separates a third
possibility: that the position is simply observed less often.

Two halves of a season are also not two independent samples of the same player: an
injury, a coordinator change or a quarterback change lands in one half and not the
other. That inflates nothing — it makes the number conservative.

Usage:
    python scripts/validate_reliability.py
    python scripts/validate_reliability.py --decompose      # §2: events, deciles, career blend
    python scripts/validate_reliability.py --from-season 2016 --min-games 3
    python scripts/validate_reliability.py --no-export
"""

from __future__ import annotations

import argparse
import importlib.util
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

# Script 07 is imported rather than copied so the OVR figure is the shipped
# mapping and not a reimplementation of it that can drift.
_S07_PATH = Path(__file__).parent / "07_compute_player_ratings.py"
_spec = importlib.util.spec_from_file_location("_s07", _S07_PATH)
s07 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s07)

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

# What counts as one observation of a player. §2 exists because reliability scales
# with the number of these, and a corner gets an order of magnitude fewer per game
# than a quarterback — so some of the position spread in the table above is a
# property of college football and some is a property of how often we get to look.
EVENT_KEYS = {
    "QB": ["passingATT", "rushingCAR"],
    "RB": ["rushingCAR", "receivingREC"],
    "WR": ["receivingREC", "rushingCAR"],
    "TE": ["receivingREC"],
    "EDGE": ["defensiveTOT", "defensiveSACKS", "defensiveTFL", "defensiveQB HUR",
             "defensivePD", "interceptionsINT", "fumblesREC"],
}
for _pg in ("DL", "LB", "CB", "S", "DB"):
    EVENT_KEYS[_pg] = EVENT_KEYS["EDGE"]

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


def game_events(data: dict, pg: str) -> float:
    return sum(_f(data, k) for k in EVENT_KEYS.get(pg, []))


def build_halves(from_season: int, min_games: int) -> pd.DataFrame:
    """Per player-season: composite and event count over odd and over even weeks.

    Split on the WEEK, not on the row order. Row order follows whatever the harvest
    happened to write, so it can correlate with the team a player faced; the week is
    a property of the schedule and splits opponents evenly by construction.
    """
    ps = read_raw("player_seasons")[["id", "player_id", "season", "position_group"]]
    ps = ps[ps["position_group"].isin(POSITION_ORDER) & (ps["season"] >= from_season)]
    meta = {int(r.id): (int(r.season), r.position_group, int(r.player_id))
            for r in ps.itertuples()}

    games = read_raw("games")[["id", "week"]]
    week_of = {int(r.id): int(r.week) for r in games.itertuples() if pd.notna(r.week)}

    stats_path = Path(__file__).parent.parent / "data" / "raw" / "stats.json"
    print(f"  reading {stats_path.name} ({stats_path.stat().st_size / 1e6:.0f} MB)...", flush=True)
    with open(stats_path, encoding="utf-8") as fh:
        rows = json.load(fh)

    acc: dict = defaultdict(lambda: {0: 0.0, 1: 0.0, "n0": 0, "n1": 0, "events": 0.0})
    season_agg: dict = {}
    for r in rows:
        psid = r.get("player_season_id")
        m = meta.get(psid)
        if m is None:
            continue
        kind = r.get("stat_type")
        if kind == "season_aggregate":
            season_agg[psid] = r.get("data") or {}
            continue
        if kind != "game_aggregate":
            continue
        gid = r.get("game_id")
        if gid is None or pd.isna(gid):
            continue
        week = week_of.get(int(gid))
        if week is None:
            continue
        data = r.get("data") or {}
        half = week % 2
        a = acc[psid]
        a[half] += game_composite(data, m[1])
        a[f"n{half}"] += 1
        a["events"] += game_events(data, m[1])
    del rows

    out = []
    for psid, a in acc.items():
        if a["n0"] < min_games or a["n1"] < min_games:
            continue
        season, pg, pid = meta[psid]
        games_total = a["n0"] + a["n1"]
        agg = season_agg.get(psid, {})
        out.append({
            "player_season_id": psid, "player_id": pid, "season": season,
            "position_group": pg,
            # Mean per game, which is what the composite reliability is about.
            "odd": a[1] / a["n1"], "even": a[0] / a["n0"],
            # Season-scale estimate from each half, for the OVR mapping: an EDGE
            # score is a SUM over games divided by sqrt(games), so a half-season
            # sum has to be rescaled before the anchors mean anything.
            "odd_edge": a[1] * np.sqrt(games_total) / a["n1"],
            "even_edge": a[0] * np.sqrt(games_total) / a["n0"],
            "games": games_total, "n_odd": a["n1"], "n_even": a["n0"],
            "events": a["events"], "events_per_game": a["events"] / games_total,
            "tier": s07.classify_playtime_tier(pg, agg),
        })
    return pd.DataFrame(out)


def attach_ovr_halves(H: pd.DataFrame) -> pd.DataFrame:
    """Carry each half through script 07's actual mapping to a published OVR.

    Anchors, then the games-confidence damping, then the playing-time tier blend
    with the recruiting anchor — the same order rate_position() applies them in.
    Recruiting stars and the per-position average come from the shipped ratings,
    so this is the mapping as configured, not as described.
    """
    rec = read_raw("recruiting")
    if not rec.empty:
        rec = rec.sort_values("composite_score", ascending=False, na_position="last") \
                 .drop_duplicates(subset=["player_id"], keep="first")
        stars_by_pid = dict(zip(rec["player_id"], rec["stars"].fillna(0)))
    else:
        stars_by_pid = {}

    rated = read_ratings("edge")
    ps = read_raw("player_seasons")[["id", "position_group"]] \
        .rename(columns={"id": "player_season_id"})
    rated = rated.merge(ps, on="player_season_id", how="left")
    pos_avg = rated.dropna(subset=["overall_rating"]) \
                   .groupby("position_group")["overall_rating"].mean().to_dict()

    def one(edge_val: float, pg: str, tier: str, stars: int, games: float) -> float:
        avg = pos_avg.get(pg, 65.0)
        ovr = s07.edge_to_ovr(edge_val, pg)
        if games < 8:                       # apply_games_confidence, single row
            conf = max(0.25, games / 8.0)
            ovr = avg + conf * (ovr - avg)
        fb = s07.fallback_rating(int(stars), avg)   # apply_multi_tier_treatment
        if tier == "role":
            return ovr * 0.75 + fb * 0.25
        if tier == "reserve":
            return ovr * 0.40 + fb * 0.60
        if tier == "bench":
            return fb
        return ovr

    H = H.copy()
    H["stars"] = H["player_id"].map(stars_by_pid).fillna(0)
    H["ovr_odd"] = [one(e, pg, t, s, g) for e, pg, t, s, g in
                    zip(H["odd_edge"], H["position_group"], H["tier"], H["stars"], H["games"])]
    H["ovr_even"] = [one(e, pg, t, s, g) for e, pg, t, s, g in
                     zip(H["even_edge"], H["position_group"], H["tier"], H["stars"], H["games"])]
    return H


def _sb(r_half: float) -> float:
    """Spearman-Brown: half-length reliability -> full-length."""
    return 2 * r_half / (1 + r_half) if r_half > -1 else float("nan")


def reliability_table(H: pd.DataFrame, min_n: int = 100) -> pd.DataFrame:
    """Both quantities per position, plus the tier mix that explains the gap."""
    out = []
    for pg in POSITION_ORDER:
        g = H[H["position_group"] == pg]
        if len(g) < min_n:
            continue
        r_c = float(g["odd"].corr(g["even"], method="spearman"))
        row = {"pos": pg, "n": len(g),
               "split_half": round(r_c, 3),
               "reliability": round(_sb(r_c), 3),
               "noise_ceiling": round(float(np.sqrt(max(_sb(r_c), 0.0))), 3)}

        # The OVR figure excludes bench rows: their published number IS the
        # recruiting constant, identical in both halves, so including them would
        # report perfect reliability for rows carrying no seasonal information.
        gv = g[g["tier"] != "bench"] if "tier" in g.columns else g
        if "ovr_odd" in g.columns and len(gv) >= min_n:
            r_o = float(gv["ovr_odd"].corr(gv["ovr_even"], method="spearman"))
            row["ovr_split_half"] = round(r_o, 3)
            row["ovr_reliability"] = round(_sb(r_o), 3)
            row["ovr_noise_ceiling"] = round(float(np.sqrt(max(_sb(r_o), 0.0))), 3)
            row["gap"] = round(_sb(r_o) - _sb(r_c), 3)
            row["pct_bench"] = round(100 * float((g["tier"] == "bench").mean()), 1)
        out.append(row)
    return pd.DataFrame(out)


def event_decile_table(H: pd.DataFrame) -> pd.DataFrame:
    """§2 — reliability against observation count, positions pooled.

    If a curve through these buckets predicts each position's reliability from its
    event count alone, then the position spread is mostly an observation-count
    effect and should be described that way everywhere it currently reads as a
    property of the position.
    """
    H = H.copy()
    H["decile"] = pd.qcut(H["events_per_game"], 10, labels=False, duplicates="drop")
    out = []
    for d, g in H.groupby("decile"):
        if len(g) < 100:
            continue
        r = float(g["odd"].corr(g["even"], method="spearman"))
        out.append({"decile": int(d) + 1, "n": len(g),
                    "events_per_game": round(float(g["events_per_game"].median()), 1),
                    "split_half": round(r, 3), "reliability": round(_sb(r), 3),
                    "top_positions": ", ".join(
                        g["position_group"].value_counts().head(3).index.tolist())})
    return pd.DataFrame(out)


def career_blend_on_composite(H: pd.DataFrame, reliab: dict) -> pd.DataFrame:
    """§2 — re-run the v4.4 career-blend experiment on the COMPOSITE.

    v4.4 tested it on the shipped OVR and found the benefit *negatively*
    correlated with reliability (-0.67), which is the opposite of what the theory
    predicts. The OVR has already been shrunk toward a recruiting prior by the
    tiers, so blending it toward a prior it partly contains is not the experiment
    anyone meant to run. This is that experiment on the unshrunk quantity:
    predict next season's composite percentile from this season's, alone or blended
    with the player's own prior seasons at weight = reliability(position).
    """
    C = H[["player_id", "season", "position_group", "odd", "even"]].copy()
    C["composite"] = (C["odd"] + C["even"]) / 2.0
    # Percentile within position-season, so seasons and positions are comparable.
    C["pct"] = C.groupby(["position_group", "season"])["composite"].rank(pct=True)
    C = C.sort_values(["player_id", "season"])
    C["pct_next"] = C.groupby("player_id")["pct"].shift(-1)
    C["season_next"] = C.groupby("player_id")["season"].shift(-1)
    C = C[C["season_next"] == C["season"] + 1].copy()

    C["cum"] = C.groupby("player_id")["pct"].cumsum() - C["pct"]
    C["n_prior"] = C.groupby("player_id").cumcount()
    C["prior"] = np.where(C["n_prior"] > 0, C["cum"] / C["n_prior"].replace(0, np.nan), np.nan)

    pos_mean = C.groupby("position_group")["pct"].mean().to_dict()
    C["prior_filled"] = np.where(C["prior"].notna(), C["prior"],
                                 C["position_group"].map(pos_mean))
    C["w"] = C["position_group"].map(reliab).fillna(0.7)
    C["blended"] = C["w"] * C["pct"] + (1 - C["w"]) * C["prior_filled"]

    out = []
    for pg, g in C.groupby("position_group"):
        if len(g) < 200:
            continue
        hist = g[g["prior"].notna()]
        row = {"pos": pg, "reliability": round(reliab.get(pg, float("nan")), 3),
               "pairs": len(g), "with_history": len(hist),
               "MAE_current": round(float(np.mean(np.abs(g["pct"] - g["pct_next"]))), 4),
               "MAE_blended": round(float(np.mean(np.abs(g["blended"] - g["pct_next"]))), 4)}
        row["delta"] = round(row["MAE_blended"] - row["MAE_current"], 4)
        if len(hist) > 100:
            row["delta_with_history"] = round(
                float(np.mean(np.abs(hist["blended"] - hist["pct_next"])))
                - float(np.mean(np.abs(hist["pct"] - hist["pct_next"]))), 4)
        out.append(row)
    return pd.DataFrame(out).sort_values("reliability")


def persistence_table(reliab: dict, composite: dict | None = None) -> pd.DataFrame:
    """Year-over-year rank persistence of the shipped OVR, beside the ceiling on it.

    The ratio is the number to read. A position near 1.0 is already extracting
    everything its measurement allows, so no feature will help it and only a new
    measurement would; a position well below 1.0 has modelling headroom left.

    **The ceiling has to be the OVR's, not the composite's** (v4.5). Persistence is
    measured on the published number, so bounding it by the reliability of a
    different quantity overstates how close each position is to its limit — and it
    overstates it most exactly where the tiers shrink most, which is the defensive
    backs the v4.4 write-up drew its headline conclusion about. Both are shown.
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
        row = {"pos": pg, "pairs": len(g),
               "persistence": round(persist, 3),
               "ovr_reliability": round(ceiling, 3),
               "share_of_ceiling": round(persist / ceiling, 3) if ceiling > 0 else None}
        if composite and pg in composite and composite[pg] > 0:
            row["vs_composite_ceiling"] = round(persist / composite[pg], 3)
        out.append(row)
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Split-half reliability of the ratings")
    ap.add_argument("--from-season", type=int, default=2016,
                    help="first season to include (2016+ is when the modern defensive "
                         "stat set is complete; earlier seasons measure a different formula)")
    ap.add_argument("--min-games", type=int, default=3,
                    help="minimum games in EACH half")
    ap.add_argument("--starters-only", action="store_true",
                    help="restrict to 8+ games — the population a rating is actually read for")
    ap.add_argument("--decompose", action="store_true",
                    help="§2: observation counts, reliability by event decile, career blend")
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
    H = attach_ovr_halves(H)
    print(f"  {len(H)} player-seasons\n")

    table = reliability_table(H)
    print(table.to_string(index=False))
    print("\n  reliability     = Spearman-Brown on the PRODUCTION COMPOSITE")
    print("  ovr_reliability = the same halves through script 07's full mapping,")
    print("                    excluding bench rows whose OVR is a constant")
    print("  gap             = how much the tiers and recruiting blend add in")
    print("                    repeatability. Repeatability, not truth: blending")
    print("                    toward a constant raises it and informs nothing.")
    print("  noise_ceiling   = sqrt(reliability); no comparison against ANY external")
    print("                    truth can exceed it, however good that truth is.\n")

    if "ovr_reliability" in table.columns:
        bad = table[table["ovr_reliability"] < table["reliability"]]
        if bad.empty:
            print("  GATE PASS: ovr_reliability >= reliability at every position.\n")
        else:
            print("  GATE FAIL: the blend REDUCES repeatability at "
                  f"{', '.join(bad['pos'])} — that is a finding, not a rounding error.\n")

    reliab = dict(zip(table["pos"], table["reliability"]))
    ovr_reliab = (dict(zip(table["pos"], table["ovr_reliability"]))
                  if "ovr_reliability" in table.columns else reliab)
    pers = persistence_table(ovr_reliab, composite=reliab)
    if not pers.empty:
        print("=== year-over-year persistence of the shipped OVR, against that ceiling ===\n")
        print(pers.to_string(index=False))
        print("\n  share_of_ceiling near 1.0 = the position is already measured as well as")
        print("  this data allows, and its unpredictability is measurement, not change.")
        print("  vs_composite_ceiling is the same ratio against the UNSHRUNK quantity —")
        print("  the number v4.4 reported, kept so the correction is visible.\n")

    deciles = pd.DataFrame()
    blend = pd.DataFrame()
    if args.decompose:
        print("=== §2a observation count, positions pooled into deciles ===\n")
        deciles = event_decile_table(H)
        print(deciles.to_string(index=False))
        ev = H.groupby("position_group")["events_per_game"].median()
        merged = table.set_index("pos").join(ev.rename("events_per_game"))
        r = merged[["reliability", "events_per_game"]].corr(method="spearman").iloc[0, 1]
        print(f"\n  spearman(position reliability, position events per game) = {r:+.3f}")
        print("  Positions, median events per game:")
        print("   " + "  ".join(f"{p} {v:.1f}" for p, v in ev.sort_values(ascending=False).items()))
        print()

        print("=== §2b the v4.4 career blend, re-run on the COMPOSITE ===\n")
        blend = career_blend_on_composite(H, reliab)
        print(blend.to_string(index=False))
        sub = blend.dropna(subset=["delta_with_history"]) if "delta_with_history" in blend else blend
        if len(sub) > 3:
            c = sub[["reliability", "delta_with_history"]].corr().iloc[0, 1]
            print(f"\n  correlation(reliability, blend benefit) = {c:+.3f}")
            print("  v4.4 measured -0.67 on the shipped OVR. A positive sign here means the")
            print("  blend helps most where a single season is least reliable, which is what")
            print("  the theory predicts and what the OVR's own shrinkage was hiding.\n")

    if args.no_export:
        return
    write_json(OUTPUT_PATH, {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "from_season": args.from_season,
        "min_games_per_half": args.min_games,
        "starters_only": bool(args.starters_only),
        "positions": table.to_dict(orient="records"),
        "persistence": pers.to_dict(orient="records") if not pers.empty else [],
        "event_deciles": deciles.to_dict(orient="records") if not deciles.empty else [],
        "career_blend": blend.to_dict(orient="records") if not blend.empty else [],
    })
    print(f"  Wrote {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
