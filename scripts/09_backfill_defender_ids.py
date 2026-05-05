"""Backfill defender_player_id on plays that have NULL attribution.

Reads all sack/INT/TFL plays where defender_player_id IS NULL, re-parses
play_text using the same regex logic as script 01, and updates the rows.
Safe to re-run — only touches plays where defender_player_id is currently NULL.
"""

import re as _re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from utils.db import get_connection
import psycopg2.extras

_DEF_SACK_RE = _re.compile(r'sacked by (.+?)(?:\s+for|\s+at|\s*$)', _re.IGNORECASE)
_DEF_INT_RE  = _re.compile(r'intercepted by (.+?)(?:\s+at|\s+for|\s+return|\s*$)', _re.IGNORECASE)
_DEF_TFL_RE  = _re.compile(r'(?:run|rush) for a loss.*?(?:tackle by|tackled by) (.+?)(?:\s+for|\s*$)', _re.IGNORECASE)

# Strip suffixes and "and <second defender>" from multi-defender plays
_SUFFIX_RE   = _re.compile(r'\b(jr\.?|sr\.?|ii|iii|iv)\s*$', _re.IGNORECASE)
_AND_RE      = _re.compile(r'\s+and\s+.+$', _re.IGNORECASE)


def _clean_name(raw: str) -> str:
    """Normalize a parsed defender name: strip second defender, strip suffixes."""
    name = _AND_RE.sub("", raw).strip()
    # Re-check suffix after stripping — keep it if it's part of the name
    return name.lower()


def _parse_defender(play_text: str, play_type: str) -> str:
    pt    = (play_text or "").strip()
    ptype = (play_type or "").lower()
    if "sack" in ptype:
        m = _DEF_SACK_RE.search(pt)
        if m: return _clean_name(m.group(1).strip())
        # Fallback: "QB Name sacked" at start of text
        m2 = _re.match(r'^.+?\s+sacked\s+by\s+(.+?)(?:\s+for|\s+at|\s*$)', pt, _re.IGNORECASE)
        if m2: return _clean_name(m2.group(1).strip())
    if "interception" in ptype:
        m = _DEF_INT_RE.search(pt)
        if m: return _clean_name(m.group(1).strip())
    if "loss" in pt.lower() or "tfl" in ptype:
        m = _DEF_TFL_RE.search(pt)
        if m: return _clean_name(m.group(1).strip())
    return ""


def _build_name_map(cur, season: int) -> dict[str, int]:
    """Build {name_lower: player_id} from all players in DB.

    Uses the full players table — defenders with 0 counting stats don't have
    stats rows but do exist in the players table from roster upserts.
    """
    cur.execute("SELECT id, name FROM players LIMIT 100000")
    return {name.lower(): db_id for db_id, name in cur.fetchall()}


def main():
    print("Defender backfill: loading plays needing attribution...")
    with get_connection() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT id, season, play_type, play_text, defense_team_id
            FROM plays
            WHERE defender_player_id IS NULL
              AND (
                play_type ILIKE '%sack%'
                OR play_type ILIKE '%interception%'
                OR play_type ILIKE '%fumble%'
                OR (play_type ILIKE '%rush%' AND play_text ILIKE '%for a loss%')
                OR (play_type ILIKE '%run%'  AND play_text ILIKE '%for a loss%')
              )
        """)
        plays = cur.fetchall()
        print(f"  {len(plays)} plays to process")

        if not plays:
            print("Nothing to do.")
            return

        seasons = list({row[1] for row in plays})
        print(f"  Building name maps for seasons: {sorted(seasons)}")
        name_maps: dict[int, dict[str, int]] = {}
        for season in seasons:
            name_maps[season] = _build_name_map(cur, season)
            print(f"    {season}: {len(name_maps[season])} players in map")

        updates = []
        matched = 0
        no_parse = 0
        no_match = 0
        for play_id, season, play_type, play_text, defense_team_id in plays:
            defender_name = _parse_defender(play_text or "", play_type or "")
            if not defender_name:
                no_parse += 1
                continue
            name_map = name_maps.get(season, {})
            db_id = name_map.get(defender_name)
            if db_id:
                updates.append((db_id, play_id))
                matched += 1
            else:
                no_match += 1

        print(f"  Parsed: {matched + no_match}  Matched: {matched}  No parse: {no_parse}  No DB match: {no_match}")

        if not updates:
            print("No matches found.")
            return

        psycopg2.extras.execute_batch(
            cur,
            "UPDATE plays SET defender_player_id = %s WHERE id = %s",
            updates,
            page_size=2000,
        )
        conn.commit()
        print(f"  Updated {len(updates)} plays with defender_player_id")

    print("Done.")


if __name__ == "__main__":
    main()
