import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv; load_dotenv()
from utils.db import get_connection

with get_connection() as conn:
    cur = conn.cursor()
    cur.execute("""
        SELECT pe.season, p.position_group,
               COUNT(*) FILTER (WHERE pe.edge_scaled IS NOT NULL) as has_edge,
               COUNT(*) FILTER (WHERE pe.edge_scaled IS NULL) as no_edge
        FROM player_edge pe
        JOIN players p ON p.id = pe.player_id
        WHERE p.position_group IN ('WR', 'DB', 'DL', 'LB', 'QB', 'RB')
        GROUP BY pe.season, p.position_group
        ORDER BY pe.season, p.position_group
    """)
    for row in cur.fetchall():
        print(row)
