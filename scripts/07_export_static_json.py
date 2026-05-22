"""Export pipeline data → static JSON files for cfb-analytics-app/data/.

All frontend data is served as static JSON — no live Supabase queries at runtime.

Exports:
  players.json              — all rated players (ratings, SHAP, recruiting, team)
  teams.json                — all teams with avg rating, player count
  team_ratings.json         — team OVR + sub-score splits for current season
  ratings_by_position.json  — top 50 per position group
  similar_players.json      — precomputed cosine similarity (OVR >= 55)
  rosters.json              — all team rosters by season  {team_id: {season: [...]}}
  schedules.json            — all games by team/season    {team_id: {season: [...]}}
  transfers.json            — all portal moves by team    {team_id: [...]}
  team_history.json         — year-by-year progression    {team_id: [{season,...}]}
  research/index.json       — published findings index
  research/{key}.json       — individual finding payloads

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

    # Convert Decimal to float; parse sub_ratings JSON and hoist split keys to top level
    import json as _json
    for r in rows:
        for k in ("overall_rating", "offense_rating", "defense_rating",
                  "sp_overall", "sp_offense", "sp_defense",
                  "recruiting_score", "avg_starter_rating"):
            if r.get(k) is not None:
                r[k] = float(r[k])
        sub = r.get("sub_ratings")
        if sub and isinstance(sub, str):
            sub = _json.loads(sub)
        if isinstance(sub, dict):
            # Hoist split ratings to top-level keys for frontend consumption
            for split_key in ("pass_off", "run_off", "pass_def", "run_def", "special_teams"):
                if split_key in sub:
                    r[split_key] = sub[split_key]
            # Also fix key names for frontend (sp_overall_scaled → sp_plus, recruiting_scaled → recruit_score)
            r["sp_plus"] = sub.get("sp_offense_scaled") or sub.get("sp_overall_scaled")
            r["recruit_score"] = sub.get("recruiting_scaled")
        r["sub_ratings"] = sub

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
                r.trajectory
            FROM ratings r
            JOIN player_seasons ps ON ps.id = r.player_season_id
            JOIN players p ON p.id = ps.player_id
            LEFT JOIN teams t ON t.id = ps.team_id
            WHERE r.overall_rating >= 55
              AND r.engine = 'edge'
            ORDER BY ps.position_group, r.overall_rating DESC
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    print(f"  {len(rows)} player-seasons eligible for similarity")

    CONF_TIER = {"SEC": 1.0, "Big Ten": 1.0, "ACC": 0.9, "Big 12": 0.9,
                 "Pac-12": 0.85, "Sun Belt": 0.5, "MAC": 0.5, "C-USA": 0.5,
                 "Mountain West": 0.55, "American": 0.55}

    def make_vector(row: dict) -> list[float]:
        """6-dim vector: ovr, edge, composite, trajectory, conf_tier, season_recency."""
        ovr        = float(row["ovr"] or 50)
        edge       = float(row["edge_score"] or 0)
        composite  = float(row.get("composite_score") or 0)
        trajectory = float(row.get("trajectory") or 0)
        conf       = CONF_TIER.get(row.get("team") or "", 0.6)
        recency    = max(0.0, (int(row["season"]) - 2008) / 17)  # normalise 2008–2025 to 0–1
        return [ovr, edge, composite, trajectory, conf, recency]

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
# New static exports (Phase A — replaces live Supabase queries in frontend)
# ---------------------------------------------------------------------------

def export_rosters(output_dir: Path) -> None:
    """Export all team rosters across all seasons → rosters.json.

    Shape: {team_id: {season: [player, ...]}}
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT ps.team_id, ps.season, ps.id AS player_season_id,
                   ps.position_group, ps.year,
                   p.id AS player_id, p.name,
                   r.overall_rating, r.trajectory_score, r.breakout_probability,
                   r.shap_values,
                   rec.stars, rec.composite_score
            FROM player_seasons ps
            JOIN players p ON p.id = ps.player_id
            LEFT JOIN ratings r ON r.player_season_id = ps.id AND r.engine = 'edge'
            LEFT JOIN LATERAL (
                SELECT stars, composite_score FROM recruiting
                WHERE player_id = ps.player_id
                ORDER BY composite_score DESC NULLS LAST LIMIT 1
            ) rec ON true
            WHERE ps.team_id IS NOT NULL
            ORDER BY ps.team_id, ps.season, r.overall_rating DESC NULLS LAST
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    rosters: dict = {}
    for row in rows:
        tid = str(row["team_id"])
        sea = str(row["season"])
        rosters.setdefault(tid, {}).setdefault(sea, []).append({
            "player_id":        row["player_id"],
            "player_season_id": row["player_season_id"],
            "name":             row["name"],
            "position_group":   row["position_group"],
            "year":             row["year"],
            "overall_rating":   float(row["overall_rating"]) if row["overall_rating"] else None,
            "trajectory":       float(row["trajectory_score"]) if row["trajectory_score"] else None,
            "breakout_prob":    float(row["breakout_probability"]) if row["breakout_probability"] else None,
            "shap":             _parse_shap(row["shap_values"]),
            "stars":            row["stars"],
            "composite_score":  float(row["composite_score"]) if row["composite_score"] else None,
        })

    write_json(output_dir / "rosters.json", rosters)


def export_schedules(output_dir: Path) -> None:
    """Export all games by team and season → schedules.json.

    Shape: {team_id: {season: [game, ...]}}
    Each game row includes is_home, opponent, result (W/L/null).
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT g.id, g.season, g.week, g.game_date, g.season_type,
                   g.home_team_id, g.away_team_id,
                   g.home_score, g.away_score, g.neutral_site,
                   ht.school AS home_team, at.school AS away_team
            FROM games g
            JOIN teams ht ON ht.id = g.home_team_id
            JOIN teams at ON at.id = g.away_team_id
            ORDER BY g.season, g.week NULLS LAST, g.id
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    schedules: dict = {}

    def _result(is_home, home_score, away_score):
        if home_score is None or away_score is None:
            return None
        team_score = home_score if is_home else away_score
        opp_score  = away_score if is_home else home_score
        if team_score > opp_score:
            return "W"
        if team_score < opp_score:
            return "L"
        return "T"

    for g in rows:
        game_date = str(g["game_date"]) if g["game_date"] else None
        home_score = int(g["home_score"]) if g["home_score"] is not None else None
        away_score = int(g["away_score"]) if g["away_score"] is not None else None

        for is_home in (True, False):
            tid = str(g["home_team_id"] if is_home else g["away_team_id"])
            sea = str(g["season"])
            opponent   = g["away_team"] if is_home else g["home_team"]
            team_score = home_score if is_home else away_score
            opp_score  = away_score if is_home else home_score
            schedules.setdefault(tid, {}).setdefault(sea, []).append({
                "game_id":     g["id"],
                "season":      g["season"],
                "week":        g["week"],
                "game_date":   game_date,
                "season_type": g["season_type"],
                "is_home":     is_home,
                "neutral_site": g["neutral_site"],
                "opponent":    opponent,
                "team_score":  team_score,
                "opp_score":   opp_score,
                "result":      _result(is_home, home_score, away_score),
            })

    write_json(output_dir / "schedules.json", schedules)


def export_transfers(output_dir: Path) -> None:
    """Export all transfer portal moves by team → transfers.json.

    Shape: {team_id: [{direction: 'in'|'out', ...}]}
    Each player appears under both their from_team and to_team.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.player_id, t.from_team_id, t.to_team_id,
                   t.transfer_year, t.portal_date,
                   p.name,
                   ps.position_group,
                   ft.school AS from_school,
                   tt.school AS to_school
            FROM transfers t
            JOIN players p ON p.id = t.player_id
            LEFT JOIN teams ft ON ft.id = t.from_team_id
            LEFT JOIN teams tt ON tt.id = t.to_team_id
            LEFT JOIN player_seasons ps
                   ON ps.player_id = t.player_id AND ps.season = t.transfer_year
            ORDER BY t.transfer_year DESC, p.name
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    transfers: dict = {}
    for row in rows:
        portal_date = str(row["portal_date"]) if row["portal_date"] else None
        entry = {
            "player_id":      row["player_id"],
            "name":           row["name"],
            "position_group": row["position_group"],
            "transfer_year":  row["transfer_year"],
            "portal_date":    portal_date,
            "from_school":    row["from_school"],
            "to_school":      row["to_school"],
        }
        if row["from_team_id"]:
            out = {**entry, "direction": "out"}
            transfers.setdefault(str(row["from_team_id"]), []).append(out)
        if row["to_team_id"]:
            inn = {**entry, "direction": "in"}
            transfers.setdefault(str(row["to_team_id"]), []).append(inn)

    write_json(output_dir / "transfers.json", transfers)


def export_team_history(output_dir: Path) -> None:
    """Export year-by-year team progression → team_history.json.

    Shape: {team_id: [{season, wins, losses, conference, sp_overall, overall_rating, ...}]}
    Rows sorted newest-first.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                tr.team_id,
                tr.season,
                tr.overall_rating,
                tr.sp_overall,
                tr.offense_rating,
                tr.defense_rating,
                tr.recruiting_score,
                t.conference,
                COUNT(CASE
                    WHEN g.home_team_id = tr.team_id AND g.home_score > g.away_score THEN 1
                    WHEN g.away_team_id = tr.team_id AND g.away_score > g.home_score THEN 1
                END) AS wins,
                COUNT(CASE
                    WHEN g.home_team_id = tr.team_id AND g.home_score < g.away_score THEN 1
                    WHEN g.away_team_id = tr.team_id AND g.away_score < g.home_score THEN 1
                END) AS losses
            FROM team_ratings tr
            JOIN teams t ON t.id = tr.team_id
            LEFT JOIN games g
                   ON g.season = tr.season
                  AND (g.home_team_id = tr.team_id OR g.away_team_id = tr.team_id)
                  AND g.home_score IS NOT NULL
            GROUP BY tr.team_id, tr.season, tr.overall_rating, tr.sp_overall,
                     tr.offense_rating, tr.defense_rating, tr.recruiting_score, t.conference
            ORDER BY tr.team_id, tr.season DESC
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    history: dict = {}
    for row in rows:
        tid = str(row["team_id"])
        history.setdefault(tid, []).append({
            "season":           row["season"],
            "wins":             int(row["wins"]) if row["wins"] is not None else None,
            "losses":           int(row["losses"]) if row["losses"] is not None else None,
            "conference":       row["conference"],
            "sp_overall":       float(row["sp_overall"]) if row["sp_overall"] else None,
            "overall_rating":   float(row["overall_rating"]) if row["overall_rating"] else None,
            "offense_rating":   float(row["offense_rating"]) if row["offense_rating"] else None,
            "defense_rating":   float(row["defense_rating"]) if row["defense_rating"] else None,
            "recruiting_score": float(row["recruiting_score"]) if row["recruiting_score"] else None,
        })

    write_json(output_dir / "team_history.json", history)


def export_research_index(output_dir: Path) -> None:
    """Write data/research/index.json — an array of published finding metadata.

    Each entry: {key, title, category, summary, headline_stat}.
    Populated from research_cache rows that include a '_meta' key in their data.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT research_key, data FROM research_cache")
        rows = cur.fetchall()

    index = []
    for key, data in rows:
        payload = data if isinstance(data, dict) else json.loads(data)
        meta = payload.get("_meta", {})
        if meta:
            index.append({
                "key":           key,
                "title":         meta.get("title", key),
                "category":      meta.get("category", ""),
                "summary":       meta.get("summary", ""),
                "headline_stat": meta.get("headline_stat", ""),
            })

    research_dir = output_dir / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    write_json(research_dir / "index.json", index)
    if not index:
        print("  No research findings with _meta yet — index.json written as empty array")


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

    print("rosters.json...")
    export_rosters(output_dir)

    print("schedules.json...")
    export_schedules(output_dir)

    print("transfers.json...")
    export_transfers(output_dir)

    print("team_history.json...")
    export_team_history(output_dir)

    print("research/index.json + research/*.json...")
    export_research(output_dir)
    export_research_index(output_dir)

    print("\nDone.")


if __name__ == "__main__":
    main()
