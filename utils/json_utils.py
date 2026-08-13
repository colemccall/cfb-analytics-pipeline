"""Shared JSON utilities for pipeline export scripts (12, 13, 14).

Centralises NaN-cleaning and file writing so each script doesn't duplicate logic.
"""

import json
import math
from pathlib import Path


def _default(o):
    import decimal
    if isinstance(o, decimal.Decimal):
        return float(o)
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"Object of type {type(o)} is not JSON serializable")


def clean_nan(o):
    """Recursively replace float NaN/inf with None.

    Literal NaN is invalid JSON and breaks browser fetch().json().
    pandas merges produce NaN for unmatched rows.
    Handles both Python float and numpy float (np.float64) NaN.
    """
    if isinstance(o, dict):
        return {k: clean_nan(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean_nan(v) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if hasattr(o, "item"):
        try:
            v = o.item()
            if isinstance(v, float):
                return v if math.isfinite(v) else None
            return v
        except (ValueError, TypeError):
            return o
    return o


def flatten_keys(obj, prefix: str = "", sep: str = "_") -> dict:
    """Flatten a nested dict into one level: {"offense": {"ppa": 1}} -> {"offense_ppa": 1}.

    The advanced-stats endpoints return three levels of nesting
    (`offense.rushingPlays.successRate`), which is unusable as a DataFrame column
    and awkward to read from. Lists are left whole — a list of heterogeneous
    objects has no sensible flat form, and every list we harvest (betting lines
    per provider, a coach's seasons) is a real one-to-many that wants its own
    table or its own row.
    """
    out: dict = {}
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        key = f"{prefix}{sep}{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(flatten_keys(v, key, sep))
        else:
            out[key] = v
    return out


def write_json(path: Path, data, *, silent: bool = False) -> None:
    """Write data to path as compact JSON, replacing NaN with null.

    Prints a one-line summary unless silent=True.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean_nan(data), f, separators=(",", ":"), default=_default, allow_nan=False)
    if not silent:
        size_kb = path.stat().st_size / 1024
        n = len(data) if isinstance(data, (list, dict)) else "?"
        print(f"  Wrote {path.name} ({size_kb:.1f} KB, {n} items)")
