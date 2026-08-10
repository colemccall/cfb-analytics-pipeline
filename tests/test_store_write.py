"""Tests for utils.store write/read round-tripping.

Locks in the NaN fix: write_computed used DataFrame.where(pd.notna(df), other=None),
which cannot put None into a float64 column. Missing numerics survived as NaN and
were written as the bare token `NaN` — accepted by Python's json.load but invalid
JSON that breaks strict parsers, including the browser's fetch().json(). It had
already corrupted team_ratings.json (3,101 occurrences) before being caught.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import store  # noqa: E402


def strict_load(path: Path):
    """Parse like a browser would: NaN/Infinity are errors, not values."""
    def reject(const):
        raise ValueError(f"invalid JSON literal: {const}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "COMPUTED_DIR", tmp_path)
    return tmp_path


class TestNaNIsNeverWritten:
    def test_float_column_with_missing_values(self, tmp_store):
        df = pd.DataFrame({"player_season_id": [1, 2], "overall_rating": [80.5, np.nan]})
        store.write_computed("ratings", df)
        rows = strict_load(tmp_store / "ratings.json")
        assert rows[1]["overall_rating"] is None

    def test_columns_absent_from_some_rows(self, tmp_store):
        """The engine_b case: concat fills unshared columns with NaN."""
        edge = pd.DataFrame([{"player_season_id": 1, "overall_rating": 80.0,
                              "trajectory_score": 1.5, "engine": "edge"}])
        other = pd.DataFrame([{"player_season_id": 2, "overall_rating": 70.0,
                               "engine": "engine_b"}])
        store.write_computed("ratings", pd.concat([edge, other], ignore_index=True))
        rows = strict_load(tmp_store / "ratings.json")
        assert rows[1]["trajectory_score"] is None

    def test_infinity_is_also_scrubbed(self, tmp_store):
        df = pd.DataFrame({"x": [np.inf, -np.inf, 1.0]})
        store.write_computed("t", df)
        rows = strict_load(tmp_store / "t.json")
        assert [r["x"] for r in rows] == [None, None, 1.0]

    def test_no_nan_token_in_raw_text(self, tmp_store):
        df = pd.DataFrame({"a": [1.0, np.nan], "b": ["x", None]})
        store.write_computed("t", df)
        assert "NaN" not in (tmp_store / "t.json").read_text(encoding="utf-8")


class TestRoundTrip:
    def test_values_survive(self, tmp_store):
        df = pd.DataFrame({"id": [1, 2], "name": ["A", "B"], "ovr": [80.5, 65.25]})
        store.write_computed("t", df)
        back = store.read_computed("t")
        assert back["ovr"].tolist() == [80.5, 65.25]
        assert back["name"].tolist() == ["A", "B"]

    def test_missing_file_returns_empty(self, tmp_store):
        assert store.read_computed("does_not_exist").empty


class TestReadRatingsFiltersByEngine:
    """Reading ratings.json unfiltered double-counts player-seasons."""

    def test_only_requested_engine_returned(self, tmp_store):
        df = pd.DataFrame([
            {"player_season_id": 1, "overall_rating": 80.0, "engine": "edge"},
            {"player_season_id": 1, "overall_rating": 55.0, "engine": "engine_b"},
            {"player_season_id": 2, "overall_rating": 70.0, "engine": "edge"},
        ])
        store.write_computed("ratings", df)
        edge = store.read_ratings("edge")
        assert len(edge) == 2
        assert set(edge["engine"]) == {"edge"}

    def test_one_row_per_player_season(self, tmp_store):
        df = pd.DataFrame([
            {"player_season_id": 1, "overall_rating": 80.0, "engine": "edge"},
            {"player_season_id": 1, "overall_rating": 55.0, "engine": "engine_b"},
        ])
        store.write_computed("ratings", df)
        edge = store.read_ratings("edge")
        assert edge["player_season_id"].duplicated().sum() == 0

    def test_empty_file_is_safe(self, tmp_store):
        assert store.read_ratings("edge").empty
