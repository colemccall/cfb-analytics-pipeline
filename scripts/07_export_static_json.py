"""Export Supabase data → static JSON files for cfb-analytics-app/data/.

Writes three files that GitHub Pages serves as static assets:
  - players.json          — all rated players with stats, ratings, SHAP, team, recruiting
  - teams.json            — all teams with avg rating, player count, conference
  - ratings_by_position.json — top 50 per position group

Also exports any research findings cached in research_cache table:
  - data/research/{research_key}.json

The path ../cfb-analytics-app/data/ assumes both repos sit in the same
CFB-Analytics-Portfolio/ workspace folder.

Usage:
    python scripts/07_export_static_json.py
    python scripts/07_export_static_json.py --season 2024
    python scripts/07_export_static_json.py --output /custom/path
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.db import get_connection

DEFAULT_OUTPUT = Path(__file__).parent.parent.parent / "cfb-analytics-app" / "data"
CURRENT_SEASON = 2025
TOP_N_PER_POSITION = 50


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    size_kb = path.stat().st_size / 1024
    n = len(data) if isinstance(data, (list, dict)) else "?"
    print(f"  Wrote {path.name} ({size_kb:.1f} KB, {n} items)")


def _parse_shap(val) -> dict:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# Export functions (all use psycopg2 — no 1000-row REST limit)
# ---------------------------------------------------------------------------

def export_players(output_dir: Path, season: int) -> None:
    """Export all rated players with ratings, recruiting info, and team."""
    with get_connection() as conn:
        cur = conn.cursor()

        # Ratings + player + team in one join
        cur.execute("""
            SELECT
                r.player_id,
                r.overall_rating,
                r.position_rating,
                r.trajectory_score,
                r.breakout_probability,
                r.shap_values,
                p.name,
                p.position,
                p.position_group,
                p.year,
                p.height_in,
                p.weight_lbs,
                p.hometown_state,
                t.id   AS team_id,
                t.school,
                t.abbreviation,
                t.conference,
                t.color,
                t.logo_url
            FROM ratings r
            JOIN players p ON p.id = r.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            WHERE r.season = %s
            ORDER BY r.overall_rating DESC NULLS LAST
        """, (season,))
        rating_rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

        # Recruiting — best record per player
        cur.execute("""
            SELECT DISTINCT ON (player_id)
                player_id, stars, composite_score, recruit_year
            FROM recruiting
            ORDER BY player_id, composite_score DESC NULLS LAST
        """)
        rec_map = {row[0]: {"stars": row[1], "composite_score": row[2], "recruit_year": row[3]}
                   for row in cur.fetchall()}

    players = []
    for raw in rating_rows:
        row = dict(zip(cols, raw))
        rec = rec_map.get(row["player_id"], {})
        players.append({
            "id":             row["player_id"],
            "name":           row["name"],
            "position":       row["position"],
            "position_group": row["position_group"],
            "year":           row["year"],
            "height_in":      row["height_in"],
            "weight_lbs":     row["weight_lbs"],
            "hometown_state": row["hometown_state"],
            "team_id":        row["team_id"],
            "team":           row["school"],
            "team_abbr":      row["abbreviation"],
            "conference":     row["conference"],
            "team_color":     row["color"],
            "logo_url":       row["logo_url"],
            "overall_rating": row["overall_rating"],
            "position_rating": row["position_rating"],
            "trajectory":     row["trajectory_score"],
            "breakout_prob":  row["breakout_probability"],
            "shap":           _parse_shap(row["shap_values"]),
            "stars":          rec.get("stars"),
            "composite_score": rec.get("composite_score"),
            "recruit_year":   rec.get("recruit_year"),
            "season":         season,
        })

    write_json(output_dir / "players.json", players)


def export_teams(output_dir: Path, season: int) -> None:
    """Export teams with average rating and player counts."""
    with get_connection() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT id, school, abbreviation, conference, color, alt_color,
                   logo_url, stadium_name, city, state, capacity
            FROM teams
            ORDER BY school
        """)
        team_rows = cur.fetchall()
        team_cols = [d[0] for d in cur.description]

        # Per-team rating aggregates for this season
        cur.execute("""
            SELECT p.team_id,
                   COUNT(r.overall_rating) AS player_count,
                   ROUND(AVG(r.overall_rating)::numeric, 2) AS avg_rating
            FROM ratings r
            JOIN players p ON p.id = r.player_id
            WHERE r.season = %s AND p.team_id IS NOT NULL
            GROUP BY p.team_id
        """, (season,))
        team_stats = {row[0]: {"player_count": row[1], "avg_rating": float(row[2]) if row[2] else None}
                      for row in cur.fetchall()}

    teams = []
    for raw in team_rows:
        t = dict(zip(team_cols, raw))
        stats = team_stats.get(t["id"], {"player_count": 0, "avg_rating": None})
        teams.append({**t, **stats, "season": season})

    teams.sort(key=lambda x: x.get("avg_rating") or 0, reverse=True)
    write_json(output_dir / "teams.json", teams)


def export_ratings_by_position(output_dir: Path, season: int) -> None:
    """Export top-N players per position group for the ratings dashboard."""
    with get_connection() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT
                r.player_id,
                r.overall_rating,
                r.position_rating,
                r.trajectory_score,
                r.breakout_probability,
                r.shap_values,
                p.name,
                p.position_group,
                p.year,
                t.school,
                t.abbreviation,
                t.conference,
                t.color
            FROM ratings r
            JOIN players p ON p.id = r.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            WHERE r.season = %s
            ORDER BY r.overall_rating DESC NULLS LAST
        """, (season,))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

        cur.execute("""
            SELECT DISTINCT ON (player_id)
                player_id, stars, composite_score
            FROM recruiting
            ORDER BY player_id, composite_score DESC NULLS LAST
        """)
        rec_map = {row[0]: {"stars": row[1], "composite": row[2]} for row in cur.fetchall()}

    by_position: dict = {}
    for raw in rows:
        row = dict(zip(cols, raw))
        pg = row["position_group"] or "ATH"
        if pg not in by_position:
            by_position[pg] = []
        if len(by_position[pg]) >= TOP_N_PER_POSITION:
            continue
        rec = rec_map.get(row["player_id"], {})
        by_position[pg].append({
            "id":              row["player_id"],
            "name":            row["name"],
            "year":            row["year"],
            "team":            row["school"],
            "team_abbr":       row["abbreviation"],
            "conference":      row["conference"],
            "team_color":      row["color"],
            "overall":         row["overall_rating"],
            "position_rating": row["position_rating"],
            "trajectory":      row["trajectory_score"],
            "breakout_prob":   row["breakout_probability"],
            "shap":            _parse_shap(row["shap_values"]),
            "stars":           rec.get("stars"),
            "composite":       rec.get("composite"),
        })

    write_json(output_dir / "ratings_by_position.json", by_position)


def export_research(output_dir: Path) -> None:
    """Export any precomputed research findings from research_cache table."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT research_key, data, generated_at FROM research_cache")
        rows = cur.fetchall()

    research_dir = output_dir / "research"
    count = 0
    for key, data, generated_at in rows:
        payload = data if isinstance(data, dict) else json.loads(data)
        payload["_generated_at"] = str(generated_at)
        write_json(research_dir / f"{key}.json", payload)
        count += 1

    if count == 0:
        print("  No research_cache entries yet — skipping research export")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Export Supabase → static JSON")
    parser.add_argument("--season", type=int, default=CURRENT_SEASON)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    output_dir: Path = args.output
    season: int = args.season

    print(f"Exporting season {season} → {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("players.json...")
    export_players(output_dir, season)

    print("teams.json...")
    export_teams(output_dir, season)

    print("ratings_by_position.json...")
    export_ratings_by_position(output_dir, season)

    print("research/*.json...")
    export_research(output_dir)

    print("\nDone.")


if __name__ == "__main__":
    main()
