"""Engine D — next-season projection, per position family.

v3.3 splits this into two models plus an exclusion, because the three families
have fundamentally different evidence behind them and one model over all of them
was averaging good inputs with bad ones.

OFFENSIVE SKILL (QB/RB/WR/TE) — the full model. Career EDGE curve + cohort
  development curves + OPPORTUNITY. Opportunity is the part that was missing:
  what a player did last season only tells you what he will do next once you
  know whether he will get the ball. Features: his share of his position room's
  production, his depth-chart rank on NEXT season's roster (computed from who is
  actually returning), and how much production is departing ahead of him.

  Measured on 2023-24: players with more than 35% of the production ahead of
  them departing went 599 -> 820 yards, while players whose room returned intact
  went 1,080 -> 1,022. That 280-yard swing is invisible to a career curve.
  Holdout: naive 9.11, model 8.23.

  A breakout label additionally requires a PATH TO THE BALL — top-2 on the new
  depth chart, or a quarter of the work ahead departing, or 300+ yards of his
  own. Regression toward the mean makes any regressor optimistic about players
  near the rating floor, and without this gate the breakout list filled with
  fourth-string receivers who had nobody to displace.

DEFENSE (EDGE/DL/LB/CB/S/DB) — career curve and cohort only. There is no
  meaningful notion of touches or a depth chart, and tackle counts partly
  measure how bad your defense is rather than how good you are. Holdout: naive
  9.41, model 8.28 — but every one of these is published as LOW CONFIDENCE
  until the underlying ratings are reworked.

OL — NOT PROJECTED AT ALL. No individual blocking data exists in any public
  source, so an OL rating is 77% recruiting composite (measured) and
  anti-correlates with the only independent assessment available. Carrying that
  forward would publish a recruiting ranking wearing the word "projection".
  See docs/RATING_AND_PROJECTION_MODEL.md §4c.

Retained from v3.2: EDGE percentiles within (season, position) so career curves
are comparable; cohort development curves as both feature and explanation
backbone; 50% variance inflation so the spread does not collapse to the mean;
cohort-relative labels; 80% intervals from a validation split; and a per-player
explanation, signed drivers and historical comparables on every projection.

Inputs  : player_seasons, ratings (computed, engine=edge), player_edge, recruiting, players
Outputs : data/models/engine_d.json          (model artifact)
          cfb-analytics-app/data/trajectory.json

Usage:
    python scripts/15_predict_trajectories.py
    python scripts/15_predict_trajectories.py --retrain
    python scripts/15_predict_trajectories.py --predict-season 2024
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.store import read_raw, read_ratings
from utils.json_utils import write_json

MODEL_PATH = Path(__file__).parent.parent / "data" / "models" / "engine_d.json"
_APP_DATA = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"
# Split deliberately: the list file is loaded by the home page and the research
# table, so it carries only what a row renders. Explanations, drivers and
# comparables are ~5 MB of per-player prose that only the modal ever shows, so
# they live in a second file fetched on demand.
OUTPUT_PATH        = _APP_DATA / "trajectory.json"
OUTPUT_DETAIL_PATH = _APP_DATA / "trajectory_detail.json"
COHORT_PATH = Path(__file__).parent.parent / "data" / "computed" / "cohort_curves.json"

TRAIN_SEASONS = (2008, 2020)   # inclusive fit window
VALID_SEASONS = (2021, 2022)   # residual quantiles for intervals come from here,
                               # never from train -- train residuals are
                               # optimistically small and under-cover.
TEST_SEASONS  = (2023, 2024)   # held out for the published metrics

# ── Position families ──────────────────────────────────────────────────────
# These are not cosmetic groupings. Each family has fundamentally different
# input quality, and pretending otherwise is what produced projections nobody
# believed.
#
# OFFENSIVE SKILL — real per-player counting stats (yards, touches, TDs) plus a
#   knowable depth chart. This is the only family where we can model both what a
#   player did AND the opportunity he is walking into, so it gets the full
#   opportunity model.
#
# DEFENSE — stats exist but describe production far less completely (tackles are
#   partly a function of how bad your defense is), and there is no clean notion
#   of "touches". Keeps the career-curve model, and is explicitly marked lower
#   confidence until the underlying ratings are reworked.
#
# SPECIALISTS — tiny samples, high variance, mostly binary outcomes. Same
#   treatment as defense, same caveat.
#
# OL — EXCLUDED ENTIRELY. No individual blocking data exists in any public
#   source, so an OL "rating" is 77% recruiting composite (measured) and
#   anti-correlates with the one independent assessment available. Projecting
#   from it would be projecting from recruiting rankings while calling it
#   production. We publish nothing rather than something we cannot defend.
#   See docs/RATING_AND_PROJECTION_MODEL.md §4c.
OFFENSIVE_SKILL = {"QB", "RB", "WR", "TE"}
DEFENSE         = {"EDGE", "DL", "LB", "CB", "S", "DB"}
SPECIALISTS     = {"K", "P"}
EXCLUDED        = {"OL"}

MODELED_POSITIONS = OFFENSIVE_SKILL | DEFENSE

FAMILY_CONFIDENCE = {
    **{p: "high" for p in OFFENSIVE_SKILL},
    **{p: "low" for p in DEFENSE},
    **{p: "low" for p in SPECIALISTS},
}

# Retained for the career-curve path shared with defense.
SKILL_POSITIONS = MODELED_POSITIONS

MIN_OVR       = 40
MIN_COHORT_N  = 20      # below this a cohort cell is noise; fall back a level
VARIANCE_LAMBDA = 0.5   # 50% inflation toward realised spread -- see header
OVR_FLOOR, OVR_CEIL = 40.0, 99.0

BREAKOUT_VS_COHORT =  3.0
DECLINE_VS_COHORT  = -3.0

# Career-shape features — every modelled position uses these.
FEATURE_COLS = [
    "ovr", "pct_last", "pct_slope", "pct_peak", "pct_from_peak", "pct_mean",
    "pct_sd", "pct_accel", "n_seasons", "games_last", "games_mean",
    "opp_sp_last", "opp_sp_trend", "class_year", "stars", "composite_score",
    "cohort_delta", "cohort_next", "pos_enc",
]

# Opportunity features — offensive skill only, because they require per-player
# counting stats and a meaningful depth chart. Both are absent everywhere else.
OPPORTUNITY_COLS = [
    "yds", "prev_yds", "career_yds", "yds_growth", "touches", "tds", "eff",
    "prod_share", "depth_rank", "next_depth_rank",
    "vacated_share", "vacated_ahead_share", "team_pos_yds",
]

SKILL_FEATURE_COLS = FEATURE_COLS + OPPORTUNITY_COLS

DRIVER_LABELS = {
    "ovr":             "current rating",
    "pct_last":        "production level",
    "pct_slope":       "career trend",
    "pct_peak":        "career-best production",
    "pct_from_peak":   "distance from peak",
    "pct_mean":        "career average production",
    "pct_sd":          "consistency",
    "pct_accel":       "recent acceleration",
    "n_seasons":       "experience",
    "games_last":      "availability",
    "games_mean":      "durability",
    "opp_sp_last":     "opponent strength",
    "opp_sp_trend":    "strengthening schedule",
    "class_year":      "class year",
    "stars":           "recruiting stars",
    "composite_score": "recruiting grade",
    "cohort_delta":    "typical development at this stage",
    "cohort_next":     "cohort baseline",
    "pos_enc":         "position",
    # Opportunity
    "yds":                 "production last season",
    "prev_yds":            "production the year before",
    "career_yds":          "career production",
    "yds_growth":          "year-over-year production change",
    "touches":             "workload",
    "tds":                 "touchdowns",
    "eff":                 "efficiency per touch",
    "prod_share":          "share of his position room's production",
    "depth_rank":          "depth-chart rank last season",
    "next_depth_rank":     "depth-chart rank on the new roster",
    "vacated_share":       "production leaving his position room",
    "vacated_ahead_share": "production departing ahead of him",
    "team_pos_yds":        "how much his position room produces",
}

_ORDINAL = {1: "true freshman", 2: "sophomore", 3: "junior", 4: "senior", 5: "fifth-year senior"}


# ---------------------------------------------------------------------------
# Career curve construction
# ---------------------------------------------------------------------------

def _recency_weighted_slope(seasons, values) -> float:
    """Slope of production over time, weighting recent seasons more heavily.

    A player who climbed 40 -> 60 -> 85 and one who fell 85 -> 60 -> 40 have
    identical means; only the slope separates them, and it is the single most
    interpretable thing about a career.
    """
    if len(values) < 2:
        return 0.0
    x = np.asarray(seasons, dtype=float)
    y = np.asarray(values, dtype=float)
    x = x - x.mean()
    w = np.power(1.6, np.arange(len(values)))
    w = w / w.sum()
    xm = np.average(x, weights=w)
    ym = np.average(y, weights=w)
    den = np.average((x - xm) ** 2, weights=w)
    if den <= 1e-9:
        return 0.0
    return float(np.average((x - xm) * (y - ym), weights=w) / den)


def build_career_frame(ratings_df, player_seasons_df, player_edge_df,
                       recruiting_df, players_df) -> pd.DataFrame:
    """One row per player-season carrying the shape of that player's career so far."""
    ps = player_seasons_df[["id", "player_id", "season", "position_group", "year", "team_id"]] \
        .rename(columns={"id": "ps_id", "year": "class_year"})
    pe = player_edge_df[["player_season_id", "edge_score", "games_played", "opponent_avg_sp"]] \
        .rename(columns={"player_season_id": "ps_id"})
    rt = ratings_df[["player_season_id", "overall_rating"]] \
        .rename(columns={"player_season_id": "ps_id", "overall_rating": "ovr"})

    df = ps.merge(pe, on="ps_id", how="inner").merge(rt, on="ps_id", how="inner")
    df = df[df["ovr"].notna() & df["edge_score"].notna() & (df["ovr"] >= MIN_OVR)]
    if df.empty:
        return df
    print(f"  {len(df)} player-seasons with both EDGE and a rating")

    # Percentile within (season, position group) -- the only form in which EDGE
    # is comparable across eras and across positions.
    df["edge_pct"] = df.groupby(["season", "position_group"])["edge_score"].rank(pct=True) * 100

    df = df.sort_values(["player_id", "season"])
    career = defaultdict(list)
    for r in df.itertuples(index=False):
        career[r.player_id].append(
            (r.season, r.edge_pct, r.games_played or 0, r.opponent_avg_sp or 0.0)
        )

    rows = []
    for pid, hist in career.items():
        for i in range(len(hist)):
            prior = hist[: i + 1]                       # career through this season
            seasons = [p[0] for p in prior]
            pcts    = [p[1] for p in prior]
            games   = [p[2] for p in prior]
            opps    = [p[3] for p in prior]
            rows.append({
                "player_id":     pid,
                "season":        seasons[-1],
                "n_seasons":     len(prior),
                "pct_last":      pcts[-1],
                "pct_slope":     _recency_weighted_slope(seasons, pcts),
                "pct_peak":      max(pcts),
                "pct_from_peak": pcts[-1] - max(pcts),
                "pct_mean":      float(np.mean(pcts)),
                "pct_sd":        float(np.std(pcts)) if len(pcts) > 1 else 0.0,
                "pct_accel":     ((pcts[-1] - pcts[-2]) - (pcts[-2] - pcts[-3])) if len(pcts) >= 3 else 0.0,
                "games_last":    float(games[-1]),
                "games_mean":    float(np.mean(games)),
                "opp_sp_last":   float(opps[-1]),
                "opp_sp_trend":  float(opps[-1] - np.mean(opps[:-1])) if len(opps) > 1 else 0.0,
                # kept for the explanation, not fed to the model
                "_pct_path":     [round(p, 1) for p in pcts[-4:]],
                "_seasons_path": seasons[-4:],
            })

    C = pd.DataFrame(rows)
    C = C.merge(df[["player_id", "season", "ps_id", "position_group", "class_year", "ovr", "team_id"]],
                on=["player_id", "season"], how="left")

    if not recruiting_df.empty:
        rb = (recruiting_df.sort_values("composite_score", ascending=False)
                           .drop_duplicates("player_id")[["player_id", "stars", "composite_score"]])
        C = C.merge(rb, on="player_id", how="left")
    else:
        C["stars"] = np.nan
        C["composite_score"] = np.nan
    C["stars"] = C["stars"].fillna(2.5)
    C["composite_score"] = C["composite_score"].fillna(0.85)

    if not players_df.empty:
        C = C.merge(players_df[["id", "name"]].rename(columns={"id": "player_id"}),
                    on="player_id", how="left")
    else:
        C["name"] = ""

    # Target: the same player's OVR one season later.
    nxt = C[["player_id", "season", "ovr"]].copy()
    nxt["season"] -= 1
    nxt = nxt.rename(columns={"ovr": "next_ovr"})
    C = C.merge(nxt, on=["player_id", "season"], how="left")
    return C


# ---------------------------------------------------------------------------
# Opportunity — offensive skill only
# ---------------------------------------------------------------------------

def _primary_yards(pos, pass_y, rush_y, rec_y):
    """The yardage that defines production at this position."""
    if pos == "QB": return pass_y + rush_y
    if pos == "RB": return rush_y + rec_y
    return rec_y                                    # WR / TE


def build_opportunity(C: pd.DataFrame, stats_df: pd.DataFrame,
                      player_seasons_df: pd.DataFrame) -> pd.DataFrame:
    """What a player produced, how much of his room's work he got, and — the part
    that actually moves next season — what is about to open up in front of him.

    A 300-yard receiver behind three returning starters and a 300-yard receiver
    whose entire room graduated are the same player to a career-curve model and
    completely different bets in reality. Measured on 2023–24: when more than
    35% of the production ahead of a player departs, his yardage goes 599 → 820,
    against 1,080 → 1,022 for players whose room returns intact.
    """
    FIELDS = [("pass_yds", "passingYDS"), ("pass_att", "passingATT"), ("pass_td", "passingTD"),
              ("rush_yds", "rushingYDS"), ("rush_car", "rushingCAR"), ("rush_td", "rushingTD"),
              ("rec_yds", "receivingYDS"), ("rec", "receivingREC"), ("rec_td", "receivingTD")]

    def _parse(d):
        if isinstance(d, str):
            try: d = json.loads(d)
            except Exception: return None
        return d if isinstance(d, dict) else None

    stat_rows = []
    agg = stats_df[(stats_df["stat_type"] == "season_aggregate") & stats_df["game_id"].isna()]
    for r in agg[["player_season_id", "data"]].itertuples(index=False):
        d = _parse(r.data)
        if d is None: continue
        row = {"ps_id": r.player_season_id}
        row.update({k: float(d.get(src) or 0) for k, src in FIELDS})
        stat_rows.append(row)
    S = pd.DataFrame(stat_rows)

    # Some players have EDGE scores (computed per game) but an empty season
    # aggregate — 176 offensive skill players in 2025, all of whom DO have
    # game-level rows. Left unfilled they count as zero production, which
    # silently corrupts their whole position room's shares and vacancy: the
    # top 2026 breakout call was a running back whose own yards read 0.
    have = set(S["ps_id"]) if not S.empty else set()
    zero = set(S.loc[S[[k for k, _ in FIELDS]].sum(axis=1) <= 0, "ps_id"]) if not S.empty else set()
    games = stats_df[stats_df["game_id"].notna()]
    if not games.empty:
        need = zero | (set(games["player_season_id"]) - have)
        g = games[games["player_season_id"].isin(need)][["player_season_id", "data"]]
        summed = {}
        for r in g.itertuples(index=False):
            d = _parse(r.data)
            if d is None: continue
            acc = summed.setdefault(r.player_season_id, {k: 0.0 for k, _ in FIELDS})
            for k, src in FIELDS:
                acc[k] += float(d.get(src) or 0)
        if summed:
            G = pd.DataFrame([{"ps_id": k, **v} for k, v in summed.items()])
            S = pd.concat([S[~S["ps_id"].isin(G["ps_id"])], G], ignore_index=True)
            print(f"    filled {len(G)} player-seasons from game-level rows "
                  f"(missing or empty season aggregate)")
    if S.empty:
        for c in ["yds", "touches", "tds", "eff", "prod_share", "depth_rank",
                  "next_depth_rank", "vacated_share", "vacated_ahead_share", "team_pos_yds"]:
            C[c] = np.nan
        return C

    C = C.merge(S, on="ps_id", how="left")
    for c in ["pass_yds", "pass_att", "pass_td", "rush_yds", "rush_car",
              "rush_td", "rec_yds", "rec", "rec_td"]:
        C[c] = C[c].fillna(0.0)

    C["yds"] = [_primary_yards(p, a, b, c) for p, a, b, c in
                zip(C["position_group"], C["pass_yds"], C["rush_yds"], C["rec_yds"])]
    C["touches"] = C["pass_att"] + C["rush_car"] + C["rec"]
    C["tds"] = C["pass_td"] + C["rush_td"] + C["rec_td"]
    C["eff"] = np.where(C["touches"] > 0, C["yds"] / C["touches"], 0.0)

    # Share of his own position room's production.
    key = ["team_id", "season", "position_group"]
    grp = C.groupby(key)["yds"]
    C["team_pos_yds"] = grp.transform("sum")
    C["prod_share"] = np.where(C["team_pos_yds"] > 0, C["yds"] / C["team_pos_yds"], 0.0)
    C["depth_rank"] = grp.rank(ascending=False, method="min")

    # Returning is a ROSTER fact, not a "was rated again" fact. Using ratings
    # here counts every returning backup as a departure and inflates vacancy.
    roster = set(zip(player_seasons_df["player_id"], player_seasons_df["season"]))
    C["returns"] = [(pid, s + 1) in roster for pid, s in zip(C["player_id"], C["season"])]

    C["_yds_ret"] = C["yds"] * C["returns"]
    C["returning_yds"] = C.groupby(key)["_yds_ret"].transform("sum")
    C["vacated_share"] = np.where(C["team_pos_yds"] > 0,
                                  1 - C["returning_yds"] / C["team_pos_yds"], 0.0)

    # His depth-chart slot next season, among players who actually return.
    C["_rk"] = np.where(C["returns"], C["yds"], -1.0)
    C["next_depth_rank"] = C.groupby(key)["_rk"].rank(ascending=False, method="min")
    C.loc[~C["returns"].astype(bool), "next_depth_rank"] = np.nan

    # The specific opening: production ahead of him, by players who are leaving.
    C = C.sort_values(key + ["yds"], ascending=[True, True, True, False])
    leaving = C["yds"] * (~C["returns"].astype(bool))
    C["_vac_ahead"] = leaving.groupby([C[k] for k in key]).cumsum() - leaving
    C["vacated_ahead_share"] = np.where(C["team_pos_yds"] > 0,
                                        C["_vac_ahead"] / C["team_pos_yds"], 0.0)

    # Career volume, so a one-year flash and a three-year workhorse differ.
    C = C.sort_values(["player_id", "season"])
    C["career_yds"] = C.groupby("player_id")["yds"].cumsum()
    C["prev_yds"] = C.groupby("player_id")["yds"].shift(1).fillna(0.0)
    C["yds_growth"] = C["yds"] - C["prev_yds"]

    # Opportunity is only meaningful where per-player counting stats exist.
    off = C["position_group"].isin(OFFENSIVE_SKILL)
    for c in OPPORTUNITY_COLS:
        C.loc[~off, c] = np.nan
    return C


def build_cohort_curves(C: pd.DataFrame, train_mask) -> tuple:
    """What players at each (position, class year, production decile) did next.

    Returns (lookup, global_delta). Cells thinner than MIN_COHORT_N are dropped
    and resolved by falling back to (position, class year), then to global.
    """
    T = C[train_mask]
    global_delta = float((T["next_ovr"] - T["ovr"]).mean())

    T = T.assign(pct_bucket=(T["pct_last"] // 10).clip(0, 9))
    g = T.groupby(["position_group", "class_year", "pct_bucket"])
    full = g.apply(lambda d: pd.Series({
        "delta": float((d["next_ovr"] - d["ovr"]).mean()),
        "n":     int(len(d)),
    }), include_groups=False).reset_index()
    full = full[full["n"] >= MIN_COHORT_N]

    g2 = T.groupby(["position_group", "class_year"])
    coarse = g2.apply(lambda d: pd.Series({
        "delta": float((d["next_ovr"] - d["ovr"]).mean()),
        "n":     int(len(d)),
    }), include_groups=False).reset_index()
    coarse = coarse[coarse["n"] >= MIN_COHORT_N]

    lookup = {
        "full":   {(r.position_group, r.class_year, r.pct_bucket): (r.delta, int(r.n))
                   for r in full.itertuples(index=False)},
        "coarse": {(r.position_group, r.class_year): (r.delta, int(r.n))
                   for r in coarse.itertuples(index=False)},
    }
    print(f"  cohort cells: {len(lookup['full'])} full, {len(lookup['coarse'])} coarse "
          f"(global delta {global_delta:+.2f})")
    return lookup, global_delta


def apply_cohort(C: pd.DataFrame, lookup: dict, global_delta: float) -> pd.DataFrame:
    bucket = (C["pct_last"] // 10).clip(0, 9)
    deltas, ns = [], []
    for pg, cy, pb in zip(C["position_group"], C["class_year"], bucket):
        hit = lookup["full"].get((pg, cy, pb)) or lookup["coarse"].get((pg, cy))
        if hit:
            deltas.append(hit[0]); ns.append(hit[1])
        else:
            deltas.append(global_delta); ns.append(0)
    C = C.copy()
    C["pct_bucket"]   = bucket
    C["cohort_delta"] = deltas
    C["cohort_n"]     = ns
    C["cohort_next"]  = C["ovr"] + C["cohort_delta"]
    return C


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------

def _describe_path(path, seasons) -> str:
    if len(path) < 2:
        return ""
    arrows = " → ".join(f"{p:.0f}" for p in path)
    direction = "climbed" if path[-1] > path[0] else "slipped" if path[-1] < path[0] else "held"
    return f"His production percentile has {direction} across {len(path)} seasons ({arrows})."


def build_explanation(row, drivers, comparables) -> str:
    """Plain English for why the number moved, built from the same values the
    model used -- never a generic template with numbers dropped in."""
    cls = _ORDINAL.get(int(row["class_year"] or 0), "player")
    pos = row["position_group"] or "player"
    parts = []

    path = row.get("_pct_path") or []
    seasons = row.get("_seasons_path") or []
    trend = _describe_path(path, seasons)
    if trend:
        parts.append(f"A {cls} {pos}. {trend}")
    else:
        parts.append(f"A {cls} {pos} with one measured season.")

    cd, cn = row["cohort_delta"], int(row.get("cohort_n") or 0)
    if cn >= MIN_COHORT_N:
        verb = "add" if cd >= 0 else "lose"
        parts.append(
            f"{_ORDINAL.get(int(row['class_year'] or 0), 'Players').capitalize()}s at this "
            f"production level historically {verb} {abs(cd):.1f} OVR the next season "
            f"(n={cn})."
        )

    vs = row["vs_cohort"]
    if abs(vs) >= 1.0:
        side = "above" if vs > 0 else "below"
        top = [d for d in drivers if d["label"] != "cohort baseline"][:2]
        because = ", ".join(f"{d['label']}" for d in top)
        parts.append(
            f"We project {abs(vs):.1f} {side} that baseline"
            + (f", driven mainly by {because}." if because else ".")
        )
    else:
        parts.append("We project him right at that baseline.")

    # Opportunity, stated the way a fan would state it. This is often the whole
    # story for a skill player and is invisible in a career curve.
    parts.append(_opportunity_sentence(row))

    if comparables:
        names = ", ".join(c["name"] for c in comparables if c.get("name"))
        avg = np.mean([c["actual_delta"] for c in comparables])
        if names:
            parts.append(f"Closest historical career shapes: {names} — they averaged {avg:+.1f}.")

    return " ".join(p for p in parts if p)


_RANK_WORD = {1: "the clear number one", 2: "second in line", 3: "third in line"}

# Thresholds for "he can realistically get the ball next season".
PATH_TOP_DEPTH   = 2      # first or second at his position on the new roster
PATH_VACATED     = 0.25   # a quarter of the work ahead of him is leaving
PATH_OWN_VOLUME  = 300    # or he already carries a real workload himself


def _has_path_to_the_ball(row) -> bool:
    """Does this player have a plausible route to more production?

    Three ways in, any one of which is enough: he is already near the top of the
    depth chart, the players ahead of him are leaving, or he has enough of his
    own volume that his role does not depend on someone else's departure.
    """
    def num(v):
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)

    rank = num(row.get("next_depth_rank"))
    vac  = num(row.get("vacated_ahead_share"))
    yds  = num(row.get("yds"))
    if rank is not None and rank <= PATH_TOP_DEPTH:
        return True
    if vac is not None and vac >= PATH_VACATED:
        return True
    if yds is not None and yds >= PATH_OWN_VOLUME:
        return True
    # No opportunity data at all — do not block on missing information.
    return rank is None and vac is None and yds is None


def _opportunity_sentence(row) -> str:
    """Depth chart and vacancy in plain English, for offensive skill players."""
    if row.get("position_group") not in OFFENSIVE_SKILL:
        return ""
    rank = row.get("next_depth_rank")
    yds = row.get("yds")
    vac = row.get("vacated_ahead_share")
    share = row.get("prod_share")
    if rank is None or (isinstance(rank, float) and np.isnan(rank)):
        return ""

    bits = []
    has_yards = yds is not None and not (isinstance(yds, float) and np.isnan(yds)) and yds > 0
    r = int(rank)
    where = _RANK_WORD.get(r, f"number {r}")
    if has_yards:
        pct = f" ({share * 100:.0f}% of his position room)" if share and share > 0 else ""
        bits.append(f"He produced {yds:,.0f} yards{pct}")
        bits.append(f"and returns as {where} at his position")
    else:
        bits.append(f"He returns as {where} at his position")

    if vac is not None and not (isinstance(vac, float) and np.isnan(vac)):
        if vac >= 0.35:
            bits.append(f"with {vac * 100:.0f}% of the production ahead of him gone — "
                        f"the job is open, which is the single strongest breakout signal we have")
        elif vac >= 0.15:
            bits.append(f"with {vac * 100:.0f}% of the production ahead of him departing")
        elif vac >= 0.02:
            bits.append(f"with only {vac * 100:.0f}% of the work ahead of him opening up")
        else:
            bits.append("with the players ahead of him all returning, which caps how much "
                        "more he can realistically take on")
    return " ".join(bits) + "."


def find_comparables(row, pool: pd.DataFrame, k: int = 3) -> list:
    """Historical players whose career curve looked like this one at the same stage."""
    cand = pool[(pool["position_group"] == row["position_group"]) &
                (pool["class_year"] == row["class_year"])]
    if len(cand) < k:
        cand = pool[pool["position_group"] == row["position_group"]]
    if len(cand) < k:
        return []
    d = (((cand["pct_last"]  - row["pct_last"])  / 25.0) ** 2 +
         ((cand["pct_slope"] - row["pct_slope"]) / 10.0) ** 2 +
         ((cand["ovr"]       - row["ovr"])       / 10.0) ** 2)
    near = cand.assign(_d=d).nsmallest(k, "_d")
    return [{
        "player_id":    int(c.player_id),
        "name":         c.name if isinstance(c.name, str) else "",
        "season":       int(c.season),
        "ovr":          round(float(c.ovr), 1),
        "actual_next":  round(float(c.next_ovr), 1),
        "actual_delta": round(float(c.next_ovr - c.ovr), 1),
    } for c in near.itertuples(index=False)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _between(s, rng):
    return s.between(rng[0], rng[1])


def main() -> None:
    parser = argparse.ArgumentParser(description="Engine D — career-curve next-season projection")
    parser.add_argument("--retrain", action="store_true")
    parser.add_argument("--predict-season", type=int, default=None,
                        help="Season to predict FROM (default: latest in the data)")
    args = parser.parse_args()

    print("Loading data...")
    ratings_df = read_ratings("edge")
    ps_df      = read_raw("player_seasons")
    edge_df    = read_raw("player_edge")
    rec_df     = read_raw("recruiting")
    players_df = read_raw("players")
    stats_df   = read_raw("stats")

    if ratings_df.empty or ps_df.empty:
        print("ERROR: ratings.json or player_seasons.json empty — run scripts 01/06/07 first")
        return

    print("Building career curves...")
    C = build_career_frame(ratings_df, ps_df, edge_df, rec_df, players_df)
    if C.empty:
        print("ERROR: no career rows built")
        return

    print("Building opportunity features (offensive skill)...")
    C = build_opportunity(C, stats_df, ps_df)

    predict_season = args.predict_season or int(C["season"].max())
    has_target = C["next_ovr"].notna()
    train_mask = has_target & _between(C["season"], TRAIN_SEASONS)

    print("Building cohort development curves...")
    lookup, global_delta = build_cohort_curves(C, train_mask)
    C = apply_cohort(C, lookup, global_delta)

    # Published so script 16 can carry players forward on the same curves rather
    # than recomputing (and drifting from) them.
    write_json(COHORT_PATH, {
        "global_delta": round(global_delta, 3),
        "min_n": MIN_COHORT_N,
        "full":   [{"position_group": k[0], "class_year": k[1], "pct_bucket": k[2],
                    "delta": round(v[0], 3), "n": v[1]} for k, v in lookup["full"].items()],
        "coarse": [{"position_group": k[0], "class_year": k[1],
                    "delta": round(v[0], 3), "n": v[1]} for k, v in lookup["coarse"].items()],
    })

    C["pos_enc"] = C["position_group"].astype("category").cat.codes.astype(float)

    # OL never enters the pipeline at all — not trained on, not predicted.
    n_ol = int(C["position_group"].isin(EXCLUDED).sum())
    C = C[~C["position_group"].isin(EXCLUDED)]
    print(f"  excluded {n_ol} OL player-seasons — no individual blocking data exists, "
          f"so an OL projection would be a recruiting ranking in disguise")

    is_modeled = C["position_group"].isin(MODELED_POSITIONS)
    D  = C[C["next_ovr"].notna() & is_modeled]
    tr = D[_between(D["season"], TRAIN_SEASONS)]
    va = D[_between(D["season"], VALID_SEASONS)]
    te = D[_between(D["season"], TEST_SEASONS)]
    print(f"  train {len(tr)}  valid {len(va)}  test {len(te)}")
    if len(tr) < 500:
        print("ERROR: insufficient training data")
        return

    from xgboost import XGBRegressor

    # ── One model per position family ──────────────────────────────────────
    # Offensive skill gets the opportunity features; defense cannot (no touches,
    # no meaningful depth chart) and keeps the career-curve set. Training them
    # together would force one feature space on two very different problems and
    # let defense's noise wash out the opportunity signal.
    FAMILIES = [
        ("offense", OFFENSIVE_SKILL, SKILL_FEATURE_COLS),
        ("defense", DEFENSE,         FEATURE_COLS),
    ]

    fitted = {}
    for fam, positions, feats in FAMILIES:
        f_tr = tr[tr["position_group"].isin(positions)]
        f_va = va[va["position_group"].isin(positions)]
        f_te = te[te["position_group"].isin(positions)]
        if len(f_tr) < 300:
            print(f"  {fam}: only {len(f_tr)} training rows — skipping family")
            continue

        path = MODEL_PATH.parent / f"engine_d_{fam}.json"
        if path.exists() and not args.retrain:
            m = XGBRegressor(); m.load_model(str(path))
        else:
            m = XGBRegressor(n_estimators=400, max_depth=5, learning_rate=0.05,
                             subsample=0.8, colsample_bytree=0.8,
                             random_state=42, n_jobs=-1)
            m.fit(f_tr[feats].values.astype(float), f_tr["next_ovr"].values.astype(float))
            path.parent.mkdir(parents=True, exist_ok=True)
            m.save_model(str(path))

        # Calibration and intervals are per family — their error distributions
        # are not the same shape, so sharing them would mis-cover both.
        tr_pred = m.predict(f_tr[feats].values.astype(float))
        tr_act  = f_tr["next_ovr"].values.astype(float)
        mu = float(tr_pred.mean())
        k  = 1.0 + VARIANCE_LAMBDA * (float(np.std(tr_act)) / float(np.std(tr_pred)) - 1.0)
        cal = lambda p, mu=mu, k=k: np.clip(mu + k * (p - mu), OVR_FLOOR, OVR_CEIL)

        va_pred = cal(m.predict(f_va[feats].values.astype(float))) if len(f_va) else np.array([])
        lo_q, hi_q = {}, {}
        if len(va_pred):
            va_res = f_va["next_ovr"].values.astype(float) - va_pred
            va_bkt = np.clip((va_pred // 10).astype(int), 4, 9)
            for b in range(4, 10):
                r = va_res[va_bkt == b]
                if len(r) < 50: r = va_res
                lo_q[b], hi_q[b] = float(np.percentile(r, 10)), float(np.percentile(r, 90))
        else:
            for b in range(4, 10): lo_q[b], hi_q[b] = -9.0, 9.0

        metrics = {}
        if len(f_te):
            te_cal = cal(m.predict(f_te[feats].values.astype(float)))
            te_act = f_te["next_ovr"].values.astype(float)
            te_cur = f_te["ovr"].values.astype(float)
            mae = lambda p: float(np.mean(np.abs(p - te_act)))
            b = np.clip((te_cal // 10).astype(int), 4, 9)
            lo = te_cal + np.array([lo_q[i] for i in b])
            hi = te_cal + np.array([hi_q[i] for i in b])
            metrics = {
                "n": int(len(f_te)),
                "naive_mae": round(mae(te_cur), 2),
                "model_mae": round(mae(te_cal), 2),
                "coverage": round(float(((te_act >= lo) & (te_act <= hi)).mean() * 100), 1),
                "sd_ratio": round(float(np.std(te_cal)) / float(np.std(te_act)), 3),
            }
            print(f"\n  {fam.upper()} holdout (n={metrics['n']}): "
                  f"naive {metrics['naive_mae']}  model {metrics['model_mae']}  "
                  f"coverage {metrics['coverage']}%  spread {metrics['sd_ratio']:.0%}")
            if metrics["model_mae"] >= metrics["naive_mae"]:
                print(f"  GATE FAILED ({fam}): {metrics['model_mae']} does not beat "
                      f"naive {metrics['naive_mae']}")
                sys.exit(1)

        fitted[fam] = {"model": m, "feats": feats, "cal": cal,
                       "lo": lo_q, "hi": hi_q, "metrics": metrics,
                       "positions": positions}

    if not fitted:
        print("ERROR: no family could be trained")
        return

    off_m = fitted.get("offense", {}).get("metrics", {})
    def_m = fitted.get("defense", {}).get("metrics", {})
    naive_mae = off_m.get("naive_mae")
    model_mae = off_m.get("model_mae")
    coverage  = off_m.get("coverage")

    # ── Predict ────────────────────────────────────────────────────────────
    P = C[(C["season"] == predict_season) & C["position_group"].isin(MODELED_POSITIONS)].copy()
    if P.empty:
        print(f"WARNING: no season-{predict_season} rows to predict")
        return
    print(f"\nProjecting {predict_season + 1} from {predict_season} careers ({len(P)} players)...")


    P["predicted_ovr"] = np.nan
    P["_family"] = ""
    shap_by_idx = {}
    import shap
    for fam, f in fitted.items():
        mask = P["position_group"].isin(f["positions"])
        if not mask.any(): continue
        X = P.loc[mask, f["feats"]].values.astype(float)
        P.loc[mask, "predicted_ovr"] = f["cal"](f["model"].predict(X))
        P.loc[mask, "_family"] = fam
        sv = shap.TreeExplainer(f["model"]).shap_values(X)
        for k, idx in enumerate(P.index[mask]):
            shap_by_idx[idx] = (sv[k], f["feats"])
        print(f"  {fam}: {int(mask.sum())} players")

    P = P[P["predicted_ovr"].notna()]
    P["vs_cohort"] = P["predicted_ovr"].values - P["cohort_next"].values

    # Drivers were computed per family during prediction — each family has its
    # own feature space, so one explainer cannot serve both.
    comp_pool = D[_between(D["season"], (TRAIN_SEASONS[0], VALID_SEASONS[1]))]
    print("Finding comparables and writing explanations...")

    records, details, blocked_breakouts = [], {}, []
    for idx, row in P.iterrows():
        sv_row, feats = shap_by_idx.get(idx, (None, FEATURE_COLS))
        if sv_row is None:
            drivers = []
        else:
            order = np.argsort(-np.abs(sv_row))[:4]
            drivers = [{
                "feature": feats[j],
                "label":   DRIVER_LABELS.get(feats[j], feats[j]),
                "effect":  round(float(sv_row[j]), 2),
            } for j in order]

        comparables = find_comparables(row, comp_pool)
        vs = float(row["vs_cohort"])
        label = ("breakout" if vs >= BREAKOUT_VS_COHORT
                 else "decline" if vs <= DECLINE_VS_COHORT else "steady")

        # A breakout needs a path to the ball. Regression toward the mean makes
        # the model optimistic about anyone rated near the floor, so without
        # this gate the list fills with fourth-string receivers: one 58-yard WR
        # sat third on his depth chart behind players who were ALL returning and
        # still scored +18.9 against his cohort. If nobody ahead of him is
        # leaving and he has no workload of his own, he is not breaking out,
        # whatever the regressor says. Offensive skill only — defense has no
        # depth chart to reason about.
        if label == "breakout" and row["position_group"] in OFFENSIVE_SKILL:
            if not _has_path_to_the_ball(row):
                label = "steady"
                blocked_breakouts.append(row["name"])
        fam = row["_family"] or "defense"
        fq = fitted.get(fam) or next(iter(fitted.values()))
        b = int(np.clip(row["predicted_ovr"] // 10, 4, 9))
        pred = float(row["predicted_ovr"])

        records.append({
            "player_season_id": int(row["ps_id"]),
            "player_id":        int(row["player_id"]),
            "name":             row["name"] if isinstance(row["name"], str) else "",
            "season":           int(row["season"]),
            "team_id":          int(row["team_id"]) if pd.notna(row["team_id"]) else None,
            "position_group":   row["position_group"],
            "class_year":       int(row["class_year"]) if pd.notna(row["class_year"]) else None,
            "current_ovr":      round(float(row["ovr"]), 1),
            "predicted_ovr":    round(pred, 1),
            "proj_low":         round(max(OVR_FLOOR, pred + fq["lo"][b]), 1),
            "proj_high":        round(min(OVR_CEIL, pred + fq["hi"][b]), 1),
            "delta":            round(pred - float(row["ovr"]), 1),
            "cohort_expected":  round(float(row["cohort_next"]), 1),
            "cohort_n":         int(row["cohort_n"]),
            "vs_cohort":        round(vs, 1),
            "trajectory_label": label,
            "shap_top_feature": drivers[0]["label"] if drivers else None,
            # Confidence is a property of the position family, not the player.
            # Offensive skill has real per-player stats and a knowable depth
            # chart; defense has neither, and says so rather than implying the
            # same rigour.
            "confidence":       FAMILY_CONFIDENCE.get(row["position_group"], "low"),
            "family":           fam,
            "engine":           "engine_d",
        })

        details[str(int(row["ps_id"]))] = {
            "explanation":   build_explanation(row, drivers, comparables),
            "drivers":       drivers,
            "comparables":   comparables,
            "edge_pct_path": row["_pct_path"],
            "seasons_path":  [int(s) for s in row["_seasons_path"]],
        }

    counts = pd.Series([r["trajectory_label"] for r in records]).value_counts().to_dict()
    print(f"  {counts}")
    if blocked_breakouts:
        print(f"  {len(blocked_breakouts)} breakout calls demoted to steady — no path to the "
              f"ball (blocked depth chart, nothing departing ahead, no workload of their own)")
    records.sort(key=lambda r: -r["vs_cohort"])

    write_json(OUTPUT_PATH, {
        "_meta": {
            "engine": "engine_d",
            "predicts_season": predict_season + 1,
            "from_season": predict_season,
            "method": ("offensive skill: career EDGE curve + cohort development + opportunity "
                       "(depth chart, production share, vacated production ahead); "
                       "defense: career curve + cohort only. OL excluded — no individual "
                       "blocking data exists. 50% variance-inflated."),
            "families": {k: v["metrics"] for k, v in fitted.items()},
            "excluded_positions": sorted(EXCLUDED),
            "test_seasons": list(TEST_SEASONS),
            "naive_mae": round(naive_mae, 2) if naive_mae else None,
            "model_mae": round(model_mae, 2) if model_mae else None,
            "interval_coverage_pct": round(coverage, 1) if coverage else None,
            "label_counts": counts,
            "n": len(records),
        },
        "predictions": records,
    })
    write_json(OUTPUT_DETAIL_PATH, details)
    print(f"Done. {len(records)} projections written to {OUTPUT_PATH.name} "
          f"(+ per-player detail in {OUTPUT_DETAIL_PATH.name})")


if __name__ == "__main__":
    main()
