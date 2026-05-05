import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv; load_dotenv()
from utils.db import get_connection

with get_connection() as conn:
    cur = conn.cursor()
    # Jeanty EDGE
    cur.execute("SELECT season, edge_score, edge_scaled, plays_counted FROM player_edge WHERE player_id = 49870 ORDER BY season")
    print("=== JEANTY EDGE ===")
    for row in cur.fetchall():
        print(row)

    # Top RB EDGE scores 2024 — who is above Jeanty?
    cur.execute("""
        SELECT pe.player_id, p.name, p.position_group, pe.season,
               pe.edge_score, pe.edge_scaled, pe.plays_counted
        FROM player_edge pe
        JOIN players p ON p.id = pe.player_id
        WHERE pe.season = 2024 AND p.position_group = 'RB'
          AND pe.edge_scaled IS NOT NULL
        ORDER BY pe.edge_scaled DESC
        LIMIT 20
    """)
    print("\n=== TOP RB EDGE 2024 ===")
    for row in cur.fetchall():
        print(row)

    # What is Jeanty's edge_scaled percentile vs all 2021-2025 RB starters?
    cur.execute("""
        SELECT pe.edge_scaled,
               PERCENT_RANK() OVER (ORDER BY pe.edge_scaled) as pct_rank
        FROM player_edge pe
        JOIN players p ON p.id = pe.player_id
        WHERE p.position_group = 'RB'
          AND pe.edge_scaled IS NOT NULL
          AND pe.player_id = 49870
        ORDER BY pe.season
    """)
    print("\n=== JEANTY EDGE PERCENTILE ===")
    for row in cur.fetchall():
        print(row)

    # G5 discount — what conference is Boise State?
    cur.execute("SELECT school, conference FROM teams WHERE id = 13")
    print("\n=== BOISE STATE CONF ===")
    for row in cur.fetchall():
        print(row)

    # Jeanty raw_score vs top RBs — what raw composite score does he get?
    # Check the top RB ratings 2024 for context
    cur.execute("""
        SELECT r.overall_rating, p.name, r.shap_values
        FROM ratings r
        JOIN players p ON p.id = r.player_id
        WHERE r.season = 2024 AND p.position_group = 'RB'
        ORDER BY r.overall_rating DESC
        LIMIT 15
    """)
    print("\n=== TOP RB RATINGS 2024 ===")
    for row in cur.fetchall():
        print(row)
