"""Local JSON data store — replaces psycopg2/Supabase for all pipeline reads/writes.

Raw source data lives in data/raw/{table}.json (populated by 00_dump_supabase.py
or by API harvest scripts).

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


def write_computed(table: str, df: pd.DataFrame) -> None:
    """Write DataFrame → data/computed/{table}.json."""
    COMPUTED_DIR.mkdir(parents=True, exist_ok=True)
    path = COMPUTED_DIR / f"{table}.json"
    rows = df.where(pd.notna(df), other=None).to_dict(orient="records")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"), cls=_Encoder)
    size_kb = path.stat().st_size / 1024
    print(f"  Wrote data/computed/{table}.json ({len(rows)} rows, {size_kb:.1f} KB)")
