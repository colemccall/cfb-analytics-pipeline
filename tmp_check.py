import sys
sys.path.insert(0, ".")
from utils.db import get_connection

with get_connection() as conn:
    cur = conn.cursor()
    cur.execute("""
        SELECT ps.position_group, COUNT(*) as cnt
        FROM ratings r
        JOIN player_seasons ps ON ps.id = r.player_season_id
        WHERE r.season = 2025 AND r.engine = 'edge'
        GROUP BY ps.position_group
        ORDER BY cnt DESC
    """)
    total = 0
    for pos, cnt in cur.fetchall():
        print(f"  {pos:<6} {cnt}")
        total += cnt
    print(f"  TOTAL  {total}")
