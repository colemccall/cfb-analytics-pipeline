"""Dump every Supabase table to local JSON.

Writes one file per table to data/raw/{table}.json.
Uses psycopg2 (direct connection) so there is no row-count limit.
Run once to migrate off Supabase; future data comes from API harvest scripts.

Usage:
    python scripts/00_dump_supabase.py
    python scripts/00_dump_supabase.py --tables players teams ratings
    python scripts/00_dump_supabase.py --output /custom/path
"""

import argparse
import json
import sys
from decimal import Decimal
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import get_connection

DEFAULT_OUTPUT = Path(__file__).parent.parent / "data" / "raw"

ALL_TABLES = [
    "teams",
    "players",
    "player_seasons",
    "games",
    "stats",
    "recruiting",
    "transfers",
    "nil_valuations",
    "coaching_changes",
    "ratings",
    "plays",
    "player_edge",
    "ea_ratings",
    "research_cache",
    "team_ratings",
]

# plays is huge — dump last and warn about size
LARGE_TABLES = {"plays", "stats"}


class _Encoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        return super().default(o)


def dump_table(conn, table: str, output_dir: Path) -> int:
    path = output_dir / f"{table}.json"
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table}")  # noqa: S608
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"), cls=_Encoder)

    size_kb = path.stat().st_size / 1024
    print(f"  {table:<25} {len(rows):>8} rows  {size_kb:>9.1f} KB  → {path}")
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Dump Supabase tables to local JSON")
    parser.add_argument("--tables", nargs="+", default=ALL_TABLES,
                        help="Tables to dump (default: all)")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help="Output directory (default: data/raw/)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    tables = args.tables

    # Warn about large tables upfront
    large = [t for t in tables if t in LARGE_TABLES]
    if large:
        print(f"Note: {', '.join(large)} can be large (100k+ rows). This may take a few minutes.")

    print(f"\nDumping {len(tables)} table(s) → {output_dir}\n")

    conn = get_connection()
    total_rows = 0
    failed = []

    for table in tables:
        try:
            total_rows += dump_table(conn, table, output_dir)
        except Exception as e:
            print(f"  ERROR dumping {table}: {e}")
            failed.append(table)

    conn.close()

    print(f"\nDone. {total_rows:,} total rows across {len(tables) - len(failed)} tables.")
    if failed:
        print(f"Failed tables: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
