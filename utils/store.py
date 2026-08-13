"""Local JSON data store — the only persistence layer the pipeline uses.

Raw source data lives in data/raw/{table}.json, written by the harvest scripts
(01-05, 08).

Computed outputs live in data/computed/{table}.json (written by rating/export scripts).

Usage:
    from utils.store import read_raw, read_computed, write_computed

    players = read_raw("players")          # → pd.DataFrame
    ratings = read_computed("ratings")     # → pd.DataFrame
    write_computed("ratings", df)          # saves data/computed/ratings.json
"""

import json
from decimal import Decimal
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from utils.json_utils import clean_nan

_ROOT     = Path(__file__).parent.parent
RAW_DIR   = _ROOT / "data" / "raw"
COMPUTED_DIR  = _ROOT / "data" / "computed"


class _Encoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        if hasattr(o, "item"):          # numpy scalar
            return o.item()
        return super().default(o)


def _load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def read_raw(table: str) -> pd.DataFrame:
    """Load data/raw/{table}.json → DataFrame. Empty DataFrame if file missing."""
    rows = _load_json(RAW_DIR / f"{table}.json")
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def read_computed(table: str) -> pd.DataFrame:
    """Load data/computed/{table}.json → DataFrame. Empty DataFrame if file missing."""
    rows = _load_json(COMPUTED_DIR / f"{table}.json")
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def read_ratings(engine: str = "edge") -> pd.DataFrame:
    """Load data/computed/ratings.json for a single engine.

    ratings.json holds every engine's output in one file (edge, engine_b, ...),
    keyed by (player_season_id, season, engine). Reading it unfiltered returns
    several rows per player-season, which silently double-counts players on
    export and contaminates any max()/mean() over overall_rating. Always filter.
    """
    df = read_computed("ratings")
    if df.empty or "engine" not in df.columns:
        return df
    return df[df["engine"] == engine].copy()


def write_raw(table: str, rows: list[dict], *, season_key: str | None = None,
              seasons: list[int] | None = None) -> int:
    """Write data/raw/{table}.json from a list of dicts, scrubbing NaN.

    When `season_key` and `seasons` are given the write is a per-season REPLACE:
    rows for those seasons are swapped out and every other season on disk is kept.
    That is the same rule script 06 uses for player_edge, and it is what makes a
    single-season re-harvest safe.

    Never append blindly. `player_seasons` was appended to for two years and
    accumulated 7,206 (player, season) pairs sitting on two teams at once,
    because a pure append cannot express "the API corrected this".
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{table}.json"

    if season_key and seasons:
        kept = [r for r in _load_json(path) if r.get(season_key) not in set(seasons)]
        rows = kept + list(rows)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean_nan(rows), f, separators=(",", ":"), cls=_Encoder, allow_nan=False)
    size_kb = path.stat().st_size / 1024
    print(f"  Wrote data/raw/{table}.json ({len(rows)} rows, {size_kb:.1f} KB)")
    return len(rows)


def write_computed(table: str, df: pd.DataFrame) -> None:
    """Write DataFrame → data/computed/{table}.json.

    NaN is scrubbed via clean_nan rather than DataFrame.where: `where` cannot put
    None into a float64 column, so missing numerics survived as NaN and were
    written as the literal token `NaN` — invalid JSON that breaks any strict
    parser (including the browser's fetch().json()).
    """
    COMPUTED_DIR.mkdir(parents=True, exist_ok=True)
    path = COMPUTED_DIR / f"{table}.json"
    rows = clean_nan(df.where(pd.notna(df), other=None).to_dict(orient="records"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"), cls=_Encoder, allow_nan=False)
    size_kb = path.stat().st_size / 1024
    print(f"  Wrote data/computed/{table}.json ({len(rows)} rows, {size_kb:.1f} KB)")
