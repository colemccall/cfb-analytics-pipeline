"""Who left for the NFL, and how selected the players who stayed therefore are.

The cohort development curves in script 15 are built from players who have a
next season — which silently means "players who stayed in college". The ones who
left are missing from exactly the top of the distribution, and the model doc has
carried that as a disclosed limitation (§4d survivorship) because there was no
departure data. There is now: `/draft/picks` covers 2008–2026 and joins to our
players at 83.7%.

This does two things with it.

**Departure rate as a feature.** For each (position, class year, production
decile), what share of players left for the NFL rather than returning. A
top-decile junior sits in a cell where a quarter of his peers leave; a
mid-pack sophomore sits in one where nobody does. That number tells the model
how selected the surviving sample it was trained on actually is, which is
information it previously had no way to see.

**Departure risk as a published fact.** A projection for a 92-rated junior is
conditional on him being on the roster, and saying so is more useful than
quietly assuming it.

What this does NOT do is impute a rating for players who left. They did not play
a college season; there is nothing to predict.

One thing to be careful about, because it is easy to get backwards: a player
drafted in April 2026 played the 2025 season. The departure is attributed to
season `draft_year - 1`, which is the last season he appears in our data.
"""

from __future__ import annotations

import pandas as pd

from utils.store import read_raw

# Cells thinner than this fall back to the coarser key. Matches MIN_COHORT_N in
# script 15 so the two lookups have the same idea of "enough".
MIN_CELL_N = 20


def drafted_by_player() -> dict:
    """{player_id: last_college_season} for every drafted player we can identify.

    Keyed on our player_id via players.cfb_api_id, which is the same id space as
    the API's `collegeAthleteId`.
    """
    picks = read_raw("draft_picks")
    players = read_raw("players")
    if picks.empty or players.empty or "cfb_api_id" not in players.columns:
        return {}

    ids = players[["id", "cfb_api_id"]].dropna().copy()
    ids["cfb_api_id"] = pd.to_numeric(ids["cfb_api_id"], errors="coerce")
    ids = ids.dropna()
    cfb_to_pid = dict(zip(ids["cfb_api_id"].astype("int64"), ids["id"].astype("int64")))

    out: dict = {}
    col = "college_athlete_id" if "college_athlete_id" in picks.columns else "collegeAthleteId"
    for cfb_id, year in zip(pd.to_numeric(picks[col], errors="coerce"),
                            pd.to_numeric(picks["year"], errors="coerce")):
        if pd.isna(cfb_id) or pd.isna(year):
            continue
        pid = cfb_to_pid.get(int(cfb_id))
        if pid is None:
            continue
        last_season = int(year) - 1
        # A player can appear once; if somehow twice, the earlier college season
        # is the one his college career ended in.
        out[pid] = min(out.get(pid, last_season), last_season)
    return out


def departure_rates(C: pd.DataFrame, drafted: dict, train_mask) -> tuple[dict, float]:
    """({(position, class_year, decile): rate}, global_rate).

    A row "departed" when the player was drafted out of that exact season. The
    denominator is every player-season in the training window, so the rate is
    "share of players at this stage who left", not "share of leavers".

    **`train_mask` must NOT be conditioned on having a next season.** That is the
    obvious mistake and it is silent: every other cohort statistic here is built
    from rows with a target, but a player who left for the NFL has no next season
    by definition, so filtering on one measures departure among the players who
    did not depart. It returned exactly 0.0% the first time, which is at least a
    loud kind of wrong. Pass a season-window mask only.
    """
    if C.empty or not drafted:
        return {}, 0.0

    T = C[train_mask].copy()
    if T.empty:
        return {}, 0.0

    T["departed"] = [
        1 if drafted.get(int(pid)) == int(season) else 0
        for pid, season in zip(T["player_id"], T["season"])
    ]
    global_rate = float(T["departed"].mean())

    lookup: dict = {}
    if "pct_bucket" in T.columns:
        g = T.groupby(["position_group", "class_year", "pct_bucket"])["departed"]
        for key, s in g:
            if len(s) >= MIN_CELL_N:
                lookup[key] = float(s.mean())

    g2 = T.groupby(["position_group", "class_year"])["departed"]
    coarse = {k: float(s.mean()) for k, s in g2 if len(s) >= MIN_CELL_N}
    return {"full": lookup, "coarse": coarse}, global_rate


def apply_departure_rate(C: pd.DataFrame, lookup: dict, global_rate: float) -> pd.Series:
    """Per-row departure rate for the cohort cell each player sits in."""
    if not lookup:
        return pd.Series([global_rate] * len(C), index=C.index)
    full, coarse = lookup.get("full", {}), lookup.get("coarse", {})
    bucket = C["pct_bucket"] if "pct_bucket" in C.columns else pd.Series([0] * len(C), index=C.index)
    out = []
    for pg, cy, pb in zip(C["position_group"], C["class_year"], bucket):
        v = full.get((pg, cy, pb))
        if v is None:
            v = coarse.get((pg, cy), global_rate)
        out.append(v)
    return pd.Series(out, index=C.index)
