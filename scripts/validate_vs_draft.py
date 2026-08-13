"""Do the players we rate highly get drafted?

This is the first independent, historical, backtestable check this project has
ever had on its ratings, and it exists because the API survey found
`/draft/picks` — 4,858 picks 2008–2026, of which 4,010 (83.7%) join to a player
we hold.

Why it matters more than the EA comparison it sits beside: we have exactly one
season of EA CFB 27, so that check is a cross-section against the present and can
never be a backtest. The draft is eighteen years deep, it is a decision made by
people spending real money, and it was made *without seeing our ratings*. If a
rating orders players usefully, it should show up here.

What it can support:
  · calibration — of players we rated 90+, what share were drafted
  · discrimination — within a position, does our ordering track draft order
  · coverage — how many of the drafted players we rated at all

What it cannot support:
  · a claim that the draft is ground truth. It is another opinion, with its own
    biases (measurables, position scarcity, injury history, the combine). A
    receiver we rate 92 who goes undrafted may be right about the player.
  · anything about linemen individually. We do not rate them — see
    utils/line_unit.py — so the OL section validates the LINE-UNIT rating against
    how many linemen a program put into the draft instead.

Read-only apart from one export for the site.

Usage:
    python scripts/validate_vs_draft.py
    python scripts/validate_vs_draft.py --no-export
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
from utils.store import read_computed, read_raw, read_ratings

OUTPUT_PATH = (
    Path(__file__).parent.parent.parent
    / "cfb-analytics-app" / "data" / "draft_validation.json"
)

POSITION_ORDER = ["QB", "RB", "WR", "TE", "OL", "EDGE", "DL", "LB", "CB", "S", "DB", "K", "P"]

# Rating bands for the calibration table. Chosen to match the tier thresholds the
# UI already uses, so a reader can carry the number back to a player card.
BANDS = [(90, 100, "90+"), (85, 90, "85-89"), (80, 85, "80-84"),
         (75, 80, "75-79"), (70, 75, "70-74"), (0, 70, "under 70")]


def peak_ratings() -> pd.DataFrame:
    """One row per player: best earned OVR, the position it came at, last season."""
    r = read_ratings("edge")
    ps = read_raw("player_seasons")[["id", "player_id", "season", "position_group"]] \
        .rename(columns={"id": "player_season_id"})
    if r.empty or ps.empty:
        return pd.DataFrame()
    m = r[["player_season_id", "overall_rating"]].merge(ps, on="player_season_id", how="inner")
    m = m[m["overall_rating"].notna()]
    m = m.sort_values("overall_rating", ascending=False) \
         .drop_duplicates(subset=["player_id"], keep="first")
    return m.rename(columns={"overall_rating": "peak_ovr"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-export", action="store_true")
    args = ap.parse_args()

    draft = read_raw("draft_picks")
    if draft.empty:
        print("No draft data. Run: python scripts/09_harvest_supplemental.py "
              "--dataset draft_picks")
        sys.exit(1)

    players = read_raw("players")
    peaks = peak_ratings()
    if peaks.empty:
        print("No earned ratings on file — run scripts 06 and 07 first.")
        sys.exit(1)

    # cfb_api_id is the CFBD athlete id, which is what collegeAthleteId also is.
    ids = players[["id", "cfb_api_id"]].dropna()
    ids["cfb_api_id"] = pd.to_numeric(ids["cfb_api_id"], errors="coerce")
    ids = ids.dropna().astype({"cfb_api_id": "int64", "id": "int64"})
    cfb_to_pid = dict(zip(ids["cfb_api_id"], ids["id"]))

    draft = draft.copy()
    draft["cfb_id"] = pd.to_numeric(draft["college_athlete_id"], errors="coerce")
    draft = draft[draft["cfb_id"].notna()]
    draft["player_id"] = draft["cfb_id"].astype("int64").map(cfb_to_pid)

    matched = draft[draft["player_id"].notna()].copy()
    matched["player_id"] = matched["player_id"].astype("int64")
    print(f"\n{len(draft)} draft picks with an athlete id; "
          f"{len(matched)} join to one of our players ({100*len(matched)/len(draft):.1f}%)")

    d = matched.merge(peaks, on="player_id", how="left")
    rated = d[d["peak_ovr"].notna()]
    print(f"{len(rated)} of those we ever rated ({100*len(rated)/max(len(d),1):.1f}%) — "
          f"the rest are almost all offensive linemen, whom we deliberately do not rate")

    # ── 1. Do we rate drafted players higher, and does the round track it? ────
    print("\n=== Peak OVR by draft round ===")
    by_round = []
    for rd in sorted(rated["round"].dropna().unique()):
        v = rated[rated["round"] == rd]["peak_ovr"]
        if len(v) < 10:
            continue
        by_round.append({"round": int(rd), "n": len(v),
                         "mean_peak_ovr": round(float(v.mean()), 1),
                         "median": round(float(v.median()), 1)})
    print(pd.DataFrame(by_round).to_string(index=False))

    all_peaks = peaks["peak_ovr"]
    drafted_ids = set(rated["player_id"])
    undrafted = peaks[~peaks["player_id"].isin(drafted_ids)]["peak_ovr"]
    print(f"\n  drafted   n={len(rated):5d}  mean peak OVR {rated['peak_ovr'].mean():.1f}")
    print(f"  undrafted n={len(undrafted):5d}  mean peak OVR {undrafted.mean():.1f}")
    print(f"  gap {rated['peak_ovr'].mean() - undrafted.mean():+.1f} points")

    # Rank correlation with draft position. Negated because pick 1 is the best
    # player and 257 is the worst, so a GOOD rating correlates negatively with
    # `overall` and positively with its negation. Reported per position because
    # a pooled figure is inflated by quarterbacks going early.
    print("\n=== Within-position agreement with draft order ===")
    print("Spearman of our peak OVR against draft position (higher = we agree "
          "with NFL teams about who goes first)\n")
    agree = []
    for pg in POSITION_ORDER:
        g = rated[rated["position_group"] == pg]
        if len(g) < 20:
            continue
        rho = float(g["peak_ovr"].corr(-g["overall"], method="spearman"))
        agree.append({"pos": pg, "n": len(g), "spearman_vs_draft_order": round(rho, 4),
                      "mean_peak_ovr": round(float(g["peak_ovr"].mean()), 1)})
    agree_df = pd.DataFrame(agree)
    print(agree_df.to_string(index=False))

    # ── 2. Calibration: P(drafted | rating band) ─────────────────────────────
    # Restricted to players whose last rated season is old enough to have been
    # draft-eligible; a 2025 sophomore has not had the chance yet and counting him
    # as undrafted would make every band look worse than it is.
    print("\n=== Calibration: share of players in each rating band who were drafted ===")
    eligible = peaks[peaks["season"] <= 2024]
    calib = []
    for lo, hi, label in BANDS:
        g = eligible[(eligible["peak_ovr"] >= lo) & (eligible["peak_ovr"] < hi)]
        if g.empty:
            continue
        n_drafted = int(g["player_id"].isin(drafted_ids).sum())
        calib.append({"band": label, "n": len(g), "drafted": n_drafted,
                      "pct_drafted": round(100 * n_drafted / len(g), 1)})
    calib_df = pd.DataFrame(calib)
    print(calib_df.to_string(index=False))
    print("\nThis should be monotone. A band that is not is a real defect in the "
          "rating, not a quirk of the draft.")

    # ── 3. The line-unit rating, which has no other external check ───────────
    print("\n=== Line-unit rating vs offensive linemen drafted ===")
    line_check = line_unit_vs_draft(matched)

    if not args.no_export:
        write_json(OUTPUT_PATH, {
            "generated_for": "draft validation",
            "n_picks_matched": len(matched),
            "n_picks_rated": len(rated),
            "drafted_mean_peak_ovr": round(float(rated["peak_ovr"].mean()), 2),
            "undrafted_mean_peak_ovr": round(float(undrafted.mean()), 2),
            "by_round": by_round,
            "by_position": agree,
            "calibration": calib,
            "line_unit": line_check,
        })


def line_unit_vs_draft(matched: pd.DataFrame) -> dict:
    """Does a team's line-unit rating predict how many linemen it puts in the draft?

    The only external validation available for a rating whose whole justification
    is that no individual measurement exists. It is a weak test — draft counts are
    small integers over a season and confounded by program size and recruiting —
    but weak and real beats the −0.274 the withdrawn player rating scored.
    """
    tr = read_computed("team_ratings")
    if tr.empty or "sub_ratings" not in tr.columns:
        print("  No team ratings yet — run script 10.")
        return {}

    rows = []
    for _, r in tr.iterrows():
        sub = r.get("sub_ratings")
        if isinstance(sub, str):
            try:
                sub = json.loads(sub)
            except (ValueError, TypeError):
                continue
        if not isinstance(sub, dict) or sub.get("line_unit") is None:
            continue
        rows.append({"team_id": int(r["team_id"]), "season": int(r["season"]),
                     "line_unit": float(sub["line_unit"])})
    if len(rows) < 100:
        print("  Not enough line-unit ratings yet — run script 10 across all seasons.")
        return {}
    lines = pd.DataFrame(rows)

    # OL picks are credited to the season BEFORE the draft — a player drafted in
    # April 2025 played the 2024 season. Getting this off by one would compare a
    # line to a draft class it never fielded.
    # The draft feed spells positions out in full ("Offensive Tackle"), not as
    # the abbreviations our position groups use. Matching on abbreviations
    # attributed zero picks and produced a silent NaN correlation.
    ol_positions = {"Offensive Tackle", "Offensive Guard", "Center",
                    "Offensive Lineman", "Guard", "Tackle"}
    ol = matched[matched["position"].isin(ol_positions)].copy()
    counts: dict = defaultdict(int)
    for _, p in ol.iterrows():
        tid = p.get("team_id")
        yr = p.get("year")
        if tid is None or pd.isna(tid) or yr is None or pd.isna(yr):
            continue
        counts[(int(tid), int(yr) - 1)] += 1

    lines["ol_drafted"] = [counts.get((t, s), 0) for t, s in
                           zip(lines["team_id"], lines["season"])]
    rho = float(lines["line_unit"].corr(lines["ol_drafted"], method="spearman"))
    print(f"  {len(lines)} team-seasons with a line rating; "
          f"{int(lines['ol_drafted'].sum())} OL draft picks attributed")
    print(f"  Spearman(line-unit rating, linemen drafted off that season) = {rho:+.4f}")
    print(f"  For comparison, the WITHDRAWN per-lineman rating scored -0.2742 "
          f"against EA — it disagreed with external opinion.")

    means = lines.groupby("ol_drafted")["line_unit"].agg(["mean", "size"])
    print("\n  mean line rating by number of linemen drafted:")
    for k, row in means.iterrows():
        if row["size"] >= 20:
            print(f"    {int(k)} drafted: {row['mean']:.1f}  (n={int(row['size'])})")

    return {"n_team_seasons": len(lines),
            "ol_picks": int(lines["ol_drafted"].sum()),
            "spearman_line_vs_picks": round(rho, 4)}


if __name__ == "__main__":
    main()
