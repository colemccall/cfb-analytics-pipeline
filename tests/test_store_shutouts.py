"""A game with one score and one gap is a shutout, not an unknown (utils/store.fill_shutouts)."""

import numpy as np
import pandas as pd

from utils.store import fill_shutouts


def _g(h, a):
    return pd.DataFrame([{"home_score": h, "away_score": a}])


def test_a_missing_score_beside_a_real_one_is_zero():
    out = fill_shutouts(_g(np.nan, 31.0))
    assert out.loc[0, "home_score"] == 0.0 and out.loc[0, "away_score"] == 31.0
    out = fill_shutouts(_g(27.0, np.nan))
    assert out.loc[0, "away_score"] == 0.0


def test_a_game_with_no_scores_is_not_played_and_stays_empty():
    out = fill_shutouts(_g(np.nan, np.nan))
    assert out["home_score"].isna().all() and out["away_score"].isna().all()


def test_a_complete_game_is_unchanged():
    out = fill_shutouts(_g(24.0, 17.0))
    assert (out.loc[0, "home_score"], out.loc[0, "away_score"]) == (24.0, 17.0)


def test_the_input_is_not_modified():
    df = _g(np.nan, 10.0)
    fill_shutouts(df)
    assert np.isnan(df.loc[0, "home_score"])
