"""Class year, derived — because the stored one is not a class year at all.

`player_seasons.year` is whatever the API returned for the player, not a
per-season value. Measured on the data on disk (2026-08-18):

  * Of 29,722 players with three or more seasons and a plausible stored value
    (1-6), it is CONSTANT across the whole career for 24,972 of them — 84.0% —
    and **not one increments correctly**.
  * It holds an outright calendar year for 114,612 of 269,552 rows.
  * Over rated player-seasons with a plausible value, 46,610 of 71,320 say
    "year 4". A senior-heavy spike at one value is not a class-year
    distribution; it is a default.

That silently broke the cohort curves, which are the explainable backbone of
every projection: a cell keyed (position, class year) was not measuring what
juniors do next, it was mixing one player's freshman, sophomore and junior
seasons under whichever label he happened to carry. Jaden Mickey played four
seasons and was labelled a junior in all of them.

Worse, script 15 derived a real class year and keyed `cohort_curves.json` on
it, and script 16 then looked that table up with the RAW value. The curve was
built on one quantity and read with another.

This module is the one place the derivation lives, so scripts 07, 12, 15 and 16
cannot disagree about how old a player is.

Two anchors, in order of trust:

  1. **recruit_year** from the recruiting table — a real enrolment date. Usable
     for 51.7% of all player-seasons and 61.7% of rated ones.
  2. **the first season we observe him in FBS** — a FLOOR, not a truth. Where
     both anchors apply they agree exactly 74.0% of the time and within a year
     91.2%, and the disagreement is one-sided: the floor is too YOUNG 23.9% of
     the time and too old 2.1%. A career that began in junior college, at an FCS
     school, or as a non-participating freshman looks younger than it was.

Anything the two anchors cannot reach is **unknown**, and this module returns
NaN for it rather than 1. That is the whole point of the third branch: a
defaulted class year hands an unmatched transfer the full true-freshman
recruiting prior, which is exactly backwards. 2,995 player-seasons (1.11%),
1,769 of them rated (1.69%), are left-censored at the start of the archive with
no recruiting anchor and are published as unknown.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Class years outside this range are a bad join, not a long career.
MIN_CLASS_YEAR = 1
MAX_CLASS_YEAR = 6

#: The first season in the archive. A career whose first observed season is this
#: one is left-censored: we cannot tell a true freshman from a fifth-year senior
#: who enrolled in 2004, so the floor anchor is not usable.
ARCHIVE_FIRST_SEASON = 2008

#: What produced the value, published beside it. `first_fbs` is a floor and is
#: labelled as one so a consumer can decline to trust it.
BASIS_RECRUITING = "recruiting"
BASIS_FIRST_FBS = "first_fbs"
BASIS_UNKNOWN = "unknown"


def derive_class_year(seasons, recruit_years, first_seasons,
                      archive_first_season: int = ARCHIVE_FIRST_SEASON):
    """(class_year, basis) for each row. Unknown is NaN, never 1.

    Args:
        seasons:        the season each row describes.
        recruit_years:  enrolment year from the recruiting table, or NaN.
        first_seasons:  the earliest season we observe this player in FBS.
        archive_first_season: seasons at or before this are left-censored.

    Returns:
        (np.ndarray[float], np.ndarray[object]) — the class year with NaN where
        neither anchor reaches, and the name of the anchor that produced it.

    The caller decides what to do with an unknown. It must not be filled with a
    default; see the module docstring.
    """
    seasons = np.asarray(pd.to_numeric(pd.Series(seasons).reset_index(drop=True),
                                       errors="coerce"), dtype=float)
    ry = np.asarray(pd.to_numeric(pd.Series(recruit_years).reset_index(drop=True),
                                  errors="coerce"), dtype=float)
    fs = np.asarray(pd.to_numeric(pd.Series(first_seasons).reset_index(drop=True),
                                  errors="coerce"), dtype=float)

    from_recruit = seasons - ry + 1.0
    from_first = seasons - fs + 1.0

    def _plausible(a):
        return np.isfinite(a) & (a >= MIN_CLASS_YEAR) & (a <= MAX_CLASS_YEAR)

    use_recruit = _plausible(from_recruit)
    # The floor is only meaningful when the career actually starts inside the
    # archive. At the boundary the player's first observed season tells us
    # nothing about his first season.
    use_first = (~use_recruit & _plausible(from_first)
                 & np.isfinite(fs) & (fs > archive_first_season))

    out = np.full(len(seasons), np.nan, dtype=float)
    out[use_recruit] = from_recruit[use_recruit]
    out[use_first] = from_first[use_first]

    basis = np.full(len(seasons), BASIS_UNKNOWN, dtype=object)
    basis[use_recruit] = BASIS_RECRUITING
    basis[use_first] = BASIS_FIRST_FBS
    return out, basis


def recruit_year_map(recruiting_df: pd.DataFrame) -> pd.Series:
    """player_id -> earliest recruit_year. Empty Series when there is no table.

    Earliest, not best: a player with two recruiting rows (high school and a
    later re-rate) enrolled once, and the first date is the one that dates him.
    Note this differs from the `stars`/`composite_score` join in script 07, which
    deliberately takes the HIGHEST composite — a rating question, not a dating one.
    """
    if recruiting_df is None or recruiting_df.empty:
        return pd.Series(dtype=float)
    if "recruit_year" not in recruiting_df.columns or "player_id" not in recruiting_df.columns:
        return pd.Series(dtype=float)
    r = recruiting_df[recruiting_df["player_id"].notna()].copy()
    if r.empty:
        return pd.Series(dtype=float)
    r["player_id"] = r["player_id"].astype("int64")
    r = r.sort_values("recruit_year").drop_duplicates("player_id", keep="first")
    return r.set_index("player_id")["recruit_year"].astype(float)


def attach_class_year(df: pd.DataFrame, recruiting_df: pd.DataFrame,
                      first_season_by_pid: pd.Series | dict | None = None,
                      *, player_col: str = "player_id",
                      season_col: str = "season") -> pd.DataFrame:
    """Add `class_year` and `class_year_basis` to a frame, in place-ish.

    `first_season_by_pid` must be computed over EVERY season of a player's
    career, not over the subset being rated. Passing None derives it from `df`,
    which is only correct when `df` already holds the whole archive.
    """
    out = df.copy()
    pid = pd.to_numeric(out[player_col], errors="coerce")
    ry = recruit_year_map(recruiting_df)
    ry_vals = pid.map(ry) if len(ry) else pd.Series(np.nan, index=out.index)

    if first_season_by_pid is None:
        first = out.groupby(player_col)[season_col].transform("min")
    else:
        first = pid.map(first_season_by_pid)

    cy, basis = derive_class_year(out[season_col], ry_vals, first)
    out["class_year"] = cy
    out["class_year_basis"] = basis
    return out
