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

class _Encoder(json.JSONEncoder):
    def default(self, o):
        import decimal
        if isinstance(o, decimal.Decimal):
            return float(o)
        return super().default(o)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), cls=_Encoder)
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

        # Join: ratings → player_seasons → players + teams
        # player_seasons.team_id is authoritative for this season (no COALESCE needed)
        cur.execute("""
            SELECT
                ps.player_id,
                r.overall_rating,
                r.position_rating,
                r.trajectory_score,
                r.breakout_probability,
                r.shap_values,
                p.name,
                ps.position,
                ps.position_group,
                ps.year,
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
            JOIN player_seasons ps ON ps.id = r.player_season_id
            JOIN players p ON p.id = ps.player_id
            LEFT JOIN teams t ON t.id = ps.team_id
            WHERE r.season = %s
            ORDER BY r.overall_rating DESC NULLS LAST
        """, (season,))
        rating_rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

        # Recruiting — best record per player (career-level)
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
            SELECT ps.team_id AS resolved_team_id,
                   COUNT(r.overall_rating) AS player_count,
                   ROUND(AVG(r.overall_rating)::numeric, 2) AS avg_rating
            FROM ratings r
            JOIN player_seasons ps ON ps.id = r.player_season_id
            WHERE r.season = %s AND ps.team_id IS NOT NULL
            GROUP BY ps.team_id
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


def export_team_ratings(output_dir: Path, season: int) -> None:
    """Export team_ratings table rows for the given season to team_ratings.json."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT tr.team_id, tr.season,
                   tr.overall_rating, tr.offense_rating, tr.defense_rating,
                   tr.sp_overall, tr.sp_offense, tr.sp_defense,
                   tr.recruiting_score, tr.avg_starter_rating, tr.sub_ratings,
                   t.school, t.conference, t.color, t.logo_url
            FROM team_ratings tr
            JOIN teams t ON t.id = tr.team_id
            WHERE tr.season = %s
            ORDER BY tr.overall_rating DESC NULLS LAST
        """, (season,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    # Convert Decimal to float; parse sub_ratings JSON string
    import json as _json
    for r in rows:
        for k in ("overall_rating", "offense_rating", "defense_rating",
                  "sp_overall", "sp_offense", "sp_defense",
                  "recruiting_score", "avg_starter_rating"):
            if r.get(k) is not None:
                r[k] = float(r[k])
        if r.get("sub_ratings") and isinstance(r["sub_ratings"], str):
            r["sub_ratings"] = _json.loads(r["sub_ratings"])

    write_json(output_dir / "team_ratings.json", rows)
    print(f"  {len(rows)} team_ratings rows exported")


def export_ratings_by_position(output_dir: Path, season: int) -> None:
    """Export top-N players per position group for the ratings dashboard."""
    with get_connection() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT
                ps.player_id,
                r.overall_rating,
                r.position_rating,
                r.trajectory_score,
                r.breakout_probability,
                r.shap_values,
                p.name,
                ps.position_group,
                ps.year,
                t.school,
                t.abbreviation,
                t.conference,
                t.color
            FROM ratings r
            JOIN player_seasons ps ON ps.id = r.player_season_id
            JOIN players p ON p.id = ps.player_id
            LEFT JOIN teams t ON t.id = ps.team_id
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


def export_similar_players(output_dir: Path) -> None:
    """Precompute cosine similarity between player-seasons and export to similar_players.json.

    For each rated player-season with OVR >= 55, finds the top 5 most similar
    player-seasons across all years using a normalized feature vector per position group.
    Stored as { player_season_id: [{id, name, season, team, ovr, similarity}] }.
    Only players with OVR >= 55 are indexed to keep the file size manageable.
    """
    import math
    from collections import defaultdict

    print("  Fetching rated player-seasons for similarity computation...")

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                ps.id            AS player_season_id,
                ps.player_id,
                ps.season,
                ps.position_group,
                p.name,
                t.school         AS team,
                r.overall_rating AS ovr,
                r.edge_score,
                r.composite_score,
                r.trajectory,
                s.data           AS stat_data
            FROM ratings r
            JOIN player_seasons ps ON ps.id = r.player_season_id
            JOIN players p ON p.id = ps.player_id
            LEFT JOIN teams t ON t.id = ps.team_id
            LEFT JOIN stats s ON s.player_season_id = ps.id
                AND s.game_id IS NULL
                AND s.stat_type = ps.position_group
            WHERE r.overall_rating >= 55
              AND r.engine = 'edge'
            ORDER BY ps.position_group, r.overall_rating DESC
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    print(f"  {len(rows)} player-seasons eligible for similarity")

    def _f(stats, key):
        if not stats:
            return 0.0
        v = stats.get(key)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    CONF_TIER = {"SEC": 1.0, "Big Ten": 1.0, "ACC": 0.9, "Big 12": 0.9,
                 "Pac-12": 0.85, "Sun Belt": 0.5, "MAC": 0.5, "C-USA": 0.5,
                 "Mountain West": 0.55, "American": 0.55}

    def make_vector(row: dict) -> list[float]:
        pg = row["position_group"] or "ATH"
        sd = row["stat_data"] or {}
        ovr = float(row["ovr"] or 50)
        edge = float(row["edge_score"] or 0)
        conf = CONF_TIER.get(row.get("team") or "", 0.6)

        if pg == "QB":
            att = max(_f(sd, "passingATT"), 1)
            return [ovr, edge,
                    _f(sd, "passingYDS") / att,
                    (_f(sd, "passingTD") + 1) / (_f(sd, "passingINT") + 1),
                    _f(sd, "passingCOMPLETIONS") / att,
                    _f(sd, "rushingYDS"),
                    float(row.get("composite_score") or 0),
                    conf]
        if pg == "RB":
            car = max(_f(sd, "rushingCAR"), 1)
            return [ovr, edge, _f(sd, "rushingYDS") / car,
                    _f(sd, "rushingYDS"), _f(sd, "receivingYDS"),
                    float(row.get("composite_score") or 0), conf, 0.0]
        if pg in ("WR", "TE"):
            rec = max(_f(sd, "receivingREC"), 1)
            return [ovr, edge, _f(sd, "receivingYDS") / rec,
                    _f(sd, "receivingYDS"), _f(sd, "receivingTD"),
                    float(row.get("composite_score") or 0), conf, 0.0]
        # Default for defensive / other
        return [ovr, edge, float(row.get("composite_score") or 0),
                float(row.get("trajectory") or 0), conf, 0.0, 0.0, 0.0]

    # Group by position group
    by_pos: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_pos[row["position_group"] or "ATH"].append(row)

    similar: dict[int, list[dict]] = {}

    for pg, group in by_pos.items():
        if len(group) < 2:
            continue

        vectors = [make_vector(r) for r in group]
        dim = max(len(v) for v in vectors)
        # Pad short vectors
        vectors = [v + [0.0] * (dim - len(v)) for v in vectors]
        arr = [v[:] for v in vectors]

        # Normalize each dimension to [0, 1] within position group
        for d in range(dim):
            col = [arr[i][d] for i in range(len(arr))]
            mn, mx = min(col), max(col)
            rng = mx - mn
            for i in range(len(arr)):
                arr[i][d] = (arr[i][d] - mn) / rng if rng > 0 else 0.0

        def cosine(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na  = math.sqrt(sum(x * x for x in a))
            nb  = math.sqrt(sum(x * x for x in b))
            return dot / (na * nb) if na * nb > 0 else 0.0

        for i, row in enumerate(group):
            sims = []
            for j, other in enumerate(group):
                if i == j:
                    continue
                s = cosine(arr[i], arr[j])
                sims.append({
                    "id":         other["player_id"],
                    "ps_id":      other["player_season_id"],
                    "name":       other["name"],
                    "season":     other["season"],
                    "team":       other["team"],
                    "ovr":        round(float(other["ovr"]), 1),
                    "similarity": round(s, 3),
                })
            sims.sort(key=lambda x: x["similarity"], reverse=True)
            similar[row["player_season_id"]] = sims[:5]

    write_json(output_dir / "similar_players.json", similar)
    print(f"  {len(similar)} player-seasons with similarity data")


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

    print(f"Exporting season {season} -> {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("players.json...")
    export_players(output_dir, season)

    print("teams.json...")
    export_teams(output_dir, season)

    print("team_ratings.json...")
    export_team_ratings(output_dir, season)

    print("ratings_by_position.json...")
    export_ratings_by_position(output_dir, season)

    print("similar_players.json...")
    export_similar_players(output_dir)

    print("research/*.json...")
    export_research(output_dir)

    print("\nDone.")


if __name__ == "__main__":
    main()
