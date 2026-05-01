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

from utils.supabase_client import get_client

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
    print(f"  Wrote {path.name} ({size_kb:.1f} KB, {len(data) if isinstance(data, list) else len(data)} items)")


def export_players(output_dir: Path, season: int) -> None:
    """Export all players with their ratings, recruiting info, and team."""
    client = get_client()

    result = (
        client.table("ratings")
        .select(
            "player_id, season, overall_rating, position_rating, trajectory_score, "
            "breakout_probability, shap_values, "
            "players(id, name, position, position_group, year, height_in, weight_lbs, "
            "hometown_state, teams(id, school, abbreviation, conference, color, logo_url))"
        )
        .eq("season", season)
        .order("overall_rating", desc=True)
        .execute()
    )

    # Build recruiting lookup
    rec_res = client.table("recruiting").select("player_id, stars, composite_score, recruit_year").execute()
    rec_map = {}
    for r in rec_res.data or []:
        pid = r["player_id"]
        if pid not in rec_map or (r.get("composite_score") or 0) > (rec_map[pid].get("composite_score") or 0):
            rec_map[pid] = r

    players = []
    for row in result.data or []:
        p = row.get("players") or {}
        team = p.get("teams") or {}
        rec = rec_map.get(row["player_id"], {})
        shap = row.get("shap_values")
        if isinstance(shap, str):
            try:
                shap = json.loads(shap)
            except Exception:
                shap = {}

        players.append({
            "id":                row["player_id"],
            "name":              p.get("name"),
            "position":          p.get("position"),
            "position_group":    p.get("position_group"),
            "year":              p.get("year"),
            "height_in":         p.get("height_in"),
            "weight_lbs":        p.get("weight_lbs"),
            "hometown_state":    p.get("hometown_state"),
            "team_id":           team.get("id"),
            "team":              team.get("school"),
            "team_abbr":         team.get("abbreviation"),
            "conference":        team.get("conference"),
            "team_color":        team.get("color"),
            "logo_url":          team.get("logo_url"),
            "overall_rating":    row.get("overall_rating"),
            "position_rating":   row.get("position_rating"),
            "trajectory":        row.get("trajectory_score"),
            "breakout_prob":     row.get("breakout_probability"),
            "shap":              shap,
            "stars":             rec.get("stars"),
            "composite_score":   rec.get("composite_score"),
            "recruit_year":      rec.get("recruit_year"),
            "season":            season,
        })

    write_json(output_dir / "players.json", players)


def export_teams(output_dir: Path, season: int) -> None:
    """Export teams with average rating and player counts."""
    client = get_client()

    teams_res = client.table("teams").select("id, school, abbreviation, conference, color, alt_color, logo_url, stadium_name, city, state, capacity").execute()

    # Aggregate ratings per team
    ratings_res = (
        client.table("ratings")
        .select("overall_rating, players(team_id)")
        .eq("season", season)
        .execute()
    )
    team_ratings: dict = {}
    for r in ratings_res.data or []:
        p = r.get("players") or {}
        tid = p.get("team_id")
        if tid:
            team_ratings.setdefault(tid, []).append(r.get("overall_rating") or 0)

    teams = []
    for t in teams_res.data or []:
        tid = t["id"]
        ratings_list = team_ratings.get(tid, [])
        avg_rating = round(sum(ratings_list) / len(ratings_list), 2) if ratings_list else None
        teams.append({
            **t,
            "avg_rating":    avg_rating,
            "player_count":  len(ratings_list),
            "season":        season,
        })

    teams.sort(key=lambda x: x.get("avg_rating") or 0, reverse=True)
    write_json(output_dir / "teams.json", teams)


def export_ratings_by_position(output_dir: Path, season: int) -> None:
    """Export top-N players per position group for the ratings dashboard."""
    client = get_client()

    result = (
        client.table("ratings")
        .select(
            "player_id, overall_rating, position_rating, trajectory_score, breakout_probability, shap_values, "
            "players(name, position_group, year, teams(school, abbreviation, conference, color))"
        )
        .eq("season", season)
        .order("overall_rating", desc=True)
        .execute()
    )

    # Build recruiting lookup for stars display
    rec_res = client.table("recruiting").select("player_id, stars, composite_score").execute()
    rec_map = {}
    for r in rec_res.data or []:
        pid = r["player_id"]
        if pid not in rec_map or (r.get("composite_score") or 0) > (rec_map[pid].get("composite_score") or 0):
            rec_map[pid] = r

    by_position: dict = {}
    for row in result.data or []:
        p = row.get("players") or {}
        pg = p.get("position_group") or "ATH"
        team = p.get("teams") or {}
        rec = rec_map.get(row["player_id"], {})
        shap = row.get("shap_values")
        if isinstance(shap, str):
            try:
                shap = json.loads(shap)
            except Exception:
                shap = {}

        entry = {
            "id":             row["player_id"],
            "name":           p.get("name"),
            "year":           p.get("year"),
            "team":           team.get("school"),
            "team_abbr":      team.get("abbreviation"),
            "conference":     team.get("conference"),
            "team_color":     team.get("color"),
            "overall":        row.get("overall_rating"),
            "position_rating": row.get("position_rating"),
            "trajectory":     row.get("trajectory_score"),
            "breakout_prob":  row.get("breakout_probability"),
            "shap":           shap,
            "stars":          rec.get("stars"),
            "composite":      rec.get("composite_score"),
        }
        by_position.setdefault(pg, []).append(entry)

    # Trim to top N per position
    trimmed = {pg: players[:TOP_N_PER_POSITION] for pg, players in by_position.items()}
    write_json(output_dir / "ratings_by_position.json", trimmed)


def export_research(output_dir: Path) -> None:
    """Export any precomputed research findings from research_cache table."""
    client = get_client()
    result = client.table("research_cache").select("research_key, data, generated_at").execute()

    research_dir = output_dir / "research"
    count = 0
    for row in result.data or []:
        key = row["research_key"]
        data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
        data["_generated_at"] = row["generated_at"]
        write_json(research_dir / f"{key}.json", data)
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

    print("\nDone. Copy data/ folder to cfb-analytics-app/data/ if not writing there directly.")


if __name__ == "__main__":
    main()
