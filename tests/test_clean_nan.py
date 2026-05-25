"""Tests for _clean_nan — recursive NaN/inf → None replacement."""

import importlib
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

_mod = importlib.import_module("12_export_frontend_json")
_clean_nan = _mod._clean_nan


class TestCleanNan:
    def test_float_nan_becomes_none(self):
        assert _clean_nan(float("nan")) is None

    def test_float_inf_becomes_none(self):
        assert _clean_nan(float("inf")) is None

    def test_negative_inf_becomes_none(self):
        assert _clean_nan(float("-inf")) is None

    def test_numpy_nan_becomes_none(self):
        assert _clean_nan(np.float64("nan")) is None

    def test_valid_float_unchanged(self):
        assert _clean_nan(3.14) == pytest.approx(3.14)

    def test_int_unchanged(self):
        assert _clean_nan(42) == 42

    def test_string_unchanged(self):
        assert _clean_nan("hello") == "hello"

    def test_none_unchanged(self):
        assert _clean_nan(None) is None

    def test_dict_recursive(self):
        d = {"a": float("nan"), "b": 1.0, "c": "x"}
        result = _clean_nan(d)
        assert result["a"] is None
        assert result["b"] == pytest.approx(1.0)
        assert result["c"] == "x"

    def test_list_recursive(self):
        lst = [1.0, float("nan"), "text", None]
        result = _clean_nan(lst)
        assert result == [1.0, None, "text", None]

    def test_nested_structure(self):
        data = {"players": [{"ovr": float("nan"), "name": "Test"}, {"ovr": 85.0}]}
        result = _clean_nan(data)
        assert result["players"][0]["ovr"] is None
        assert result["players"][1]["ovr"] == pytest.approx(85.0)
