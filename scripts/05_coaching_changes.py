"""Load coaching changes from seed CSV → upsert to Supabase coaching_changes table.

The seed CSV lives at data/coaching_changes_seed.csv (not gitignored).
Add rows manually as coaching changes occur, then re-run this script.

CSV columns: team,coach_name,role,start_season,end_season,prior_team
  role: HC | OC | DC

Usage:
    python scripts/05_coaching_changes.py
    python scripts/05_coaching_changes.py --csv path/to/custom.csv
"""

import argparse
import csv
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import bulk_upsert, get_connection

DEFAULT_CSV = Path(__file__).parent.parent / "data" / "coaching_changes_seed.csv"


def load_team_index() -> dict:
    """Build {school_lower: team_id} via psycopg2."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, school FROM teams")
        return {school.lower(): tid for tid, school in cur.fetchall()}


def load_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def upsert_coaching_changes(csv_rows: list[dict], team_index: dict) -> None:
    rows = []
    for r in csv_rows:
        team = r.get("team", "").lower().strip()
        team_id = team_index.get(team)
        if not team_id:
            print(f"  WARNING: team not found in DB: '{r.get('team')}' — skipping")
            continue

        end_season = r.get("end_season", "").strip()
        rows.append({
            "team_id":      team_id,
            "coach_name":   r.get("coach_name", "").strip(),
            "role":         r.get("role", "HC").strip().upper(),
            "start_season": int(r["start_season"]) if r.get("start_season") else None,
            "end_season":   int(end_season) if end_season else None,
            "prior_team":   r.get("prior_team", "").strip() or None,
        })

    if rows:
        bulk_upsert("coaching_changes", rows, ["team_id", "coach_name", "role", "start_season"])
    print(f"Upserted {len(rows)} coaching change rows")


def main():
    parser = argparse.ArgumentParser(description="Load coaching changes CSV → Supabase")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"CSV not found: {args.csv}")
        print("Create data/coaching_changes_seed.csv with columns: team,coach_name,role,start_season,end_season,prior_team")
        sys.exit(1)

    team_index = load_team_index()
    csv_rows = load_csv(args.csv)
    print(f"Loaded {len(csv_rows)} rows from {args.csv}")
    upsert_coaching_changes(csv_rows, team_index)
    print("Done.")


if __name__ == "__main__":
    main()
