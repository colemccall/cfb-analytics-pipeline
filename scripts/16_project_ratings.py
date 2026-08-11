"""Engine 'projected' — a rating for every player on an unplayed season's roster.

Writes engine="projected" rows into data/computed/ratings.json for the upcoming
season. These are model output and are labeled as such at every layer: each row
carries projection_source, projection_confidence, and an interval, and the
frontend renders them with a PROJ badge that the caller cannot forget to pass.

SOURCE CHAIN — first hit wins, best signal first:

  1. engine_d   Career-curve projection from script 15 (returning players the
                model could project). Highest confidence: it beats naive
                carry-forward by ~1.0 MAE on the held-out 2023-24 seasons.
  2. carry      Last season's earned OVR moved along the cohort development
                curve for that (position, class year, production decile).
                Covers returners script 15 skips -- OL/K/P, who have no
                individual play attribution, and skill players without EDGE.
                Cohort arithmetic alone scores MAE 8.54 vs naive 9.32.
  3. recruiting True freshmen, from the 2026 signing class composite through
                FRESHMAN_OVR_ANCHORS -- anchors calibrated on what true
                freshmen ACTUALLY rate, not on career recruiting value. A .94+
                recruit averages 62.9 as a true freshman; the career recruiting
                anchors would say ~90. Adjusted for position and, as measured,
                a small schedule tilt.
  4. ea_cfb27   EA CFB 27's own overall, for players with no signal of ours
                (JUCO arrivals, unrated walk-ons). Deliberately LAST: it is a
                third party's opinion. Every row carries ea_ovr alongside
                whatever source won, so the side-by-side always renders.

Inputs : player_seasons, ratings (computed, engine=edge), player_edge, recruiting,
         ea_ratings, games, team_ratings, trajectory.json, cohort_curves.json
Output : data/computed/ratings.json  (engine="projected" rows for --season)

Usage:
    python scripts/16_project_ratings.py                # defaults to 2026
    python scripts/16_project_ratings.py --season 2026
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_ratings, read_computed, write_computed

_ROOT = Path(__file__).parent.parent
TRAJECTORY_PATH = _ROOT.parent / "cfb-analytics-app" / "data" / "trajectory.json"
COHORT_PATH     = _ROOT / "data" / "computed" / "cohort_curves.json"

DEFAULT_SEASON = 2026
OVR_FLOOR, OVR_CEIL = 30.0, 99.0

# Calibrated on 3,276 rated true-freshman seasons, 2008-2025. These map a
# recruiting composite to what a true freshman ACTUALLY rates -- measured bucket
# means, not career potential:
#     <.80 -> 51.0   .80-.85 -> 54.5   .85-.88 -> 55.5
#   .88-.91 -> 58.2  .91-.94 -> 61.1      .94+ -> 62.9
# Using the career recruiting anchors here would rate a 5-star freshman ~90,
# which is a claim about his ceiling, not his first season.
FRESHMAN_OVR_ANCHORS = [
    (0.60, 46.0), (0.77, 51.0), (0.825, 54.5), (0.865, 55.5),
    (0.895, 58.2), (0.925, 61.1), (0.960, 62.9), (1.00, 65.0),
]

# Deviation from the all-position freshman mean, for groups with n >= 50 in the
# calibration set. OL/K/P had 16/6/8 rated freshmen -- too few to separate from
# noise, so they get no adjustment rather than a made-up one.
FRESHMAN_POS_ADJ = {
    "QB": +3.7, "EDGE": +1.8, "RB": +1.7, "DB": +1.0, "WR": +0.9,
    "LB": -0.8, "CB": -1.0, "TE": -1.1, "DL": -1.6, "S": -2.6,
}

# Schedule tilt. Measured partial effect, holding recruiting constant:
# ovr = 5.0 + 59.9*composite + 0.024*oppSP, so a 1-SD tougher slate (10.6 SP+)
# is worth +0.26 OVR. Real, but small -- a freshman projection is recruiting
# grade and position first, schedule a long way third. Capped so it can never
# masquerade as a bigger signal than it is.
SOS_COEF = 0.024
SOS_CAP  = 1.5

CONFIDENCE = {"engine_d": "high", "carry": "medium", "recruiting": "low", "ea_cfb27": "low"}

# Positions we refuse to project. OL has no individual blocking data in any
# public source, so its rating is 77% recruiting composite (measured) and
# anti-correlates with the only independent assessment available. Carrying that
# forward would publish a recruiting ranking under the label "projection".
# See docs/RATING_AND_PROJECTION_MODEL.md §4c.
EXCLUDED_POSITIONS = {"OL"}

# Confidence is capped by position family regardless of source. Defensive and
# special-teams ratings are built on inputs that describe production far less
# completely, so even an engine_d projection for them is not "high".
FAMILY_CAP = {
    **{p: "high" for p in ("QB", "RB", "WR", "TE")},
    **{p: "low" for p in ("EDGE", "DL", "LB", "CB", "S", "DB", "K", "P")},
}
_CONF_ORDER = {"high": 3, "medium": 2, "low": 1}

def _confidence(source: str, pos: str) -> str:
    src = CONFIDENCE.get(source, "low")
    cap = FAMILY_CAP.get(pos, "low")
    return src if _CONF_ORDER[src] <= _CONF_ORDER[cap] else cap


def _position_ceilings() -> dict:
    """Per-position OVR ceilings, read from script 07's anchors — not restated.

    Ratings are absolute: a position can only reach what its own anchor mapping
    tops out at. OL caps at 88 because its inputs are team proxies (team rush
    YPA, sack rate allowed) that cannot separate an All-American from an average
    starter, and they saturate — a 99 would assert precision the data lacks.

    Carrying a rating forward along a cohort curve does not create the evidence
    those ceilings encode, so the same caps apply here. Without this, adding a
    cohort delta to an 88 lineman produced a projected 93 that no earned OL
    rating could ever reach.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_s07", Path(__file__).parent / "07_compute_player_ratings.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    ceilings = {pg: max(a[1] for a in anchors)
                for pg, anchors in m.COMPOSITE_OVR_ANCHORS.items()}
    for pg, anchors in getattr(m, "EDGE_OVR_ANCHORS", {}).items():
        ceilings.setdefault(pg, max(a[1] for a in anchors))
    return ceilings

# Interval half-widths by source, from each method's own error on holdout data.
# Freshmen get the widest band: it is the least certain thing we publish.
INTERVAL = {"engine_d": None, "carry": 9.0, "recruiting": 13.0, "ea_cfb27": 13.0}


def _interp(x, anchors):
    xs = [a[0] for a in anchors]
    ys = [a[1] for a in anchors]
    return float(np.interp(float(x), xs, ys))


def load_cohort_curves() -> tuple:
    if not COHORT_PATH.exists():
        print(f"  WARNING: {COHORT_PATH.name} missing — run script 15 first. "
              f"Carry-forward will use a flat global delta.")
        return {}, {}, 0.0
    with open(COHORT_PATH) as f:
        d = json.load(f)
    full = {(r["position_group"], r["class_year"], r["pct_bucket"]): r["delta"] for r in d["full"]}
    coarse = {(r["position_group"], r["class_year"]): r["delta"] for r in d["coarse"]}
    return full, coarse, float(d.get("global_delta", 0.0))


def load_engine_d() -> dict:
    """{player_id: projection row} from script 15."""
    if not TRAJECTORY_PATH.exists():
        print(f"  WARNING: {TRAJECTORY_PATH.name} missing — run script 15 first.")
        return {}
    with open(TRAJECTORY_PATH) as f:
        d = json.load(f)
    rows = d["predictions"] if isinstance(d, dict) else d
    return {int(r["player_id"]): r for r in rows if r.get("player_id") is not None}


def build_sos(season: int, games_df, team_ratings_df) -> dict:
    """{team_id: mean opponent SP+} for the upcoming season's schedule.

    Opponent quality comes from the most recent season with SP+ on file, since
    the upcoming season has none by definition.
    """
    if games_df.empty or team_ratings_df.empty:
        return {}
    tr = team_ratings_df.dropna(subset=["sp_overall"])
    if tr.empty:
        return {}
    latest = int(tr["season"].max())
    sp = {int(r.team_id): float(r.sp_overall)
          for r in tr[tr["season"] == latest].itertuples(index=False)}

    g = games_df[games_df["season"] == season]
    opp = defaultdict(list)
    for r in g.itertuples(index=False):
        h, a = r.home_team_id, r.away_team_id
        if pd.notna(h) and pd.notna(a):
            if int(a) in sp: opp[int(h)].append(sp[int(a)])
            if int(h) in sp: opp[int(a)].append(sp[int(h)])
    return {t: float(np.mean(v)) for t, v in opp.items() if v}


def main() -> None:
    ap = argparse.ArgumentParser(description="Projected ratings for an unplayed season")
    ap.add_argument("--season", type=int, default=DEFAULT_SEASON)
    args = ap.parse_args()
    season = args.season
    prev = season - 1

    print(f"Projecting {season} ratings...")
    ps_df   = read_raw("player_seasons")
    edge_df = read_raw("player_edge")
    rec_df  = read_raw("recruiting")
    ea_df   = read_raw("ea_ratings")
    games   = read_raw("games")
    tr_df   = read_computed("team_ratings")
    earned  = read_ratings("edge")

    roster = ps_df[ps_df["season"] == season]
    if roster.empty:
        print(f"ERROR: no player_seasons for {season} — run script 01 --year {season} first")
        sys.exit(1)
    print(f"  {season} roster: {len(roster)} player-seasons across "
          f"{roster['team_id'].nunique()} teams")

    cohort_full, cohort_coarse, global_delta = load_cohort_curves()
    engine_d = load_engine_d()
    print(f"  engine_d projections available for {len(engine_d)} players")

    # Previous season's earned OVR + production percentile, for the carry path.
    prev_ps = ps_df[ps_df["season"] == prev][["id", "player_id", "position_group", "year"]] \
        .rename(columns={"id": "ps_id", "year": "class_year"})
    prev_rat = earned[["player_season_id", "overall_rating"]] \
        .rename(columns={"player_season_id": "ps_id", "overall_rating": "ovr"})
    prev_edge = edge_df[edge_df["season"] == prev][["player_season_id", "edge_score"]] \
        .rename(columns={"player_season_id": "ps_id"})
    prev_all = prev_ps.merge(prev_rat, on="ps_id").merge(prev_edge, on="ps_id", how="left")
    if not prev_all.empty:
        prev_all["pct"] = prev_all.groupby("position_group")["edge_score"].rank(pct=True) * 100
    prev_by_pid = {int(r.player_id): r for r in prev_all.itertuples(index=False)}
    print(f"  {prev} earned ratings available for {len(prev_by_pid)} players")

    # Recruiting: prefer the class that signed for this season.
    rec_by_pid = {}
    if not rec_df.empty:
        r = rec_df.dropna(subset=["player_id"]).sort_values(
            ["player_id", "recruit_year"], ascending=[True, False])
        for row in r.itertuples(index=False):
            pid = int(row.player_id)
            if pid not in rec_by_pid:
                rec_by_pid[pid] = {"composite": row.composite_score, "stars": row.stars,
                                   "year": row.recruit_year}

    ea_by_pid = {}
    if not ea_df.empty and "player_id" in ea_df.columns:
        for row in ea_df.dropna(subset=["player_id"]).itertuples(index=False):
            ea_by_pid[int(row.player_id)] = float(row.ovr) if pd.notna(row.ovr) else None
    print(f"  EA CFB 27 overalls matched for {len(ea_by_pid)} players")

    sos = build_sos(season, games, tr_df)
    sos_mean = float(np.mean(list(sos.values()))) if sos else 0.0

    ceilings = _position_ceilings()
    print(f"  position ceilings: " +
          "  ".join(f"{k}={v:g}" for k, v in sorted(ceilings.items())))

    def cohort_delta(pg, class_year, pct):
        if pct is not None and not pd.isna(pct):
            hit = cohort_full.get((pg, class_year, float(int(pct // 10))))
            if hit is not None:
                return hit
        hit = cohort_coarse.get((pg, class_year))
        return hit if hit is not None else global_delta

    out, counts = [], defaultdict(int)
    for row in roster.itertuples(index=False):
        pid = int(row.player_id) if pd.notna(row.player_id) else None
        if pid is None:
            continue
        pg = row.position_group
        if pg in EXCLUDED_POSITIONS:
            counts["excluded_ol"] += 1
            continue
        cy = row.year
        tid = int(row.team_id) if pd.notna(row.team_id) else None
        ea_ovr = ea_by_pid.get(pid)

        ovr = source = None
        low = high = None
        note = None

        # 1 — career-curve projection
        d = engine_d.get(pid)
        if d is not None and d.get("predicted_ovr") is not None:
            ovr, source = float(d["predicted_ovr"]), "engine_d"
            low, high = d.get("proj_low"), d.get("proj_high")
            note = d.get("trajectory_label")

        # 2 — carry last season's earned rating along the cohort curve
        if ovr is None:
            p = prev_by_pid.get(pid)
            if p is not None and pd.notna(p.ovr):
                delta = cohort_delta(pg, p.class_year, getattr(p, "pct", None))
                ovr, source = float(p.ovr) + float(delta), "carry"

        # 3 — true freshman from the signing class
        if ovr is None:
            r = rec_by_pid.get(pid)
            comp = r["composite"] if r else None
            if comp is not None and pd.notna(comp) and float(comp) > 0:
                base = _interp(comp, FRESHMAN_OVR_ANCHORS)
                base += FRESHMAN_POS_ADJ.get(pg, 0.0)
                if tid in sos:
                    tilt = SOS_COEF * (sos[tid] - sos_mean)
                    base += float(np.clip(tilt, -SOS_CAP, SOS_CAP))
                ovr, source = base, "recruiting"

        # 4 — EA's opinion, only when we have nothing of our own
        if ovr is None and ea_ovr is not None:
            ovr, source = float(ea_ovr), "ea_cfb27"

        if ovr is None:
            counts["no_signal"] += 1
            continue

        # Absolute ceilings apply to projections exactly as they do to earned
        # ratings — see _position_ceilings().
        ovr = float(np.clip(ovr, OVR_FLOOR, min(OVR_CEIL, ceilings.get(pg, OVR_CEIL))))
        if low is None or high is None:
            half = INTERVAL.get(source) or 9.0
            low, high = ovr - half, ovr + half
        low = round(float(np.clip(low, OVR_FLOOR, OVR_CEIL)), 1)
        high = round(float(np.clip(high, OVR_FLOOR, OVR_CEIL)), 1)

        counts[source] += 1
        out.append({
            "player_season_id":      int(row.id),
            "season":                season,
            "overall_rating":        round(ovr, 1),
            "position_rating":       None,
            "trajectory_score":      None,
            "trajectory":            note,
            "breakout_probability":  None,
            "shap_values":           None,
            "model_version":         "projected-v1",
            "engine":                "projected",
            "provenance":            "projected",
            "projection_source":     source,
            "projection_confidence": _confidence(source, pg),
            "projection_low":        low,
            "projection_high":       high,
            "ea_ovr":                ea_ovr,
        })

    print(f"\n  source mix: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not out:
        print("ERROR: nothing projected")
        sys.exit(1)

    vals = np.array([r["overall_rating"] for r in out])
    print(f"  projected OVR: mean {vals.mean():.1f}  SD {vals.std():.1f}  "
          f"min {vals.min():.1f}  max {vals.max():.1f}")

    # Distribution gate — the standing rule is that a season's ratings must not
    # silently compress. Compare against last season's earned distribution.
    prev_vals = prev_all["ovr"].dropna().values if not prev_all.empty else np.array([])
    if len(prev_vals) > 100:
        ratio = float(vals.std()) / float(prev_vals.std())
        print(f"  {prev} earned: mean {prev_vals.mean():.1f}  SD {prev_vals.std():.1f}  "
              f"(projected spread is {ratio:.0%} of earned)")
        if not (0.60 <= ratio <= 1.40):
            print(f"\n  GATE FAILED: projected spread is {ratio:.0%} of last season's. "
                  f"Refusing to write a distorted season.")
            sys.exit(1)
        print("  distribution gate PASSED")

    # Replace any previous projected rows for this season; leave other engines be.
    existing = read_computed("ratings")
    if not existing.empty:
        keep = existing[~((existing["engine"] == "projected") & (existing["season"] == season))]
        combined = pd.concat([keep, pd.DataFrame(out)], ignore_index=True)
    else:
        combined = pd.DataFrame(out)
    write_computed("ratings", combined)
    print(f"\nDone. {len(out)} projected {season} ratings written "
          f"({len(combined)} rows total in ratings.json)")


if __name__ == "__main__":
    main()
