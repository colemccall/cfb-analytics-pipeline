"""Who was the head coach, and when did that change.

The coaching table used to be a 20-row hand-seeded CSV, and every finding that
wanted to say something about coaching was written down as blocked because of it.
It was not blocked. `/coaches` returns full tenure with per-season record, SRS and
SP+ splits, matching our schools at 100%, and script 09 now harvests it —
**2,584 coach-seasons covering 2008–2026**, against 20 seeded rows.

This module is the single reader. It exists because three consumers need the same
two questions answered identically:

    head_coach_by_team_season()   who was in charge
    coaching_changes(season)      which teams changed coach going into it

and because "changed coach" is a definition, not a lookup. A team can appear with
two coaches in one season (a midseason firing), and an interim is a different
event from a hire. The rule here: the coach of record for a team-season is the one
who coached the most games, and a change is when that person differs from the
previous season's. Interim stints that never became the plurality are invisible,
which is the correct default — they are noise for a season-level analysis.
"""

from __future__ import annotations

import pandas as pd

from utils.store import read_raw


def _coach_rows() -> pd.DataFrame:
    df = read_raw("coaches")
    if df.empty or "team_id" not in df.columns:
        return pd.DataFrame()
    return df[df["team_id"].notna() & df["season"].notna()].copy()


def head_coach_by_team_season() -> dict:
    """{(team_id, season): {coach_id, coach_name, games, wins, losses, sp_overall}}.

    Plurality of games decides who "the" coach was, so a two-game interim does not
    displace the man who coached the other ten.
    """
    df = _coach_rows()
    if df.empty:
        return {}
    df["games"] = pd.to_numeric(df.get("games"), errors="coerce").fillna(0)
    df = df.sort_values("games", ascending=False)

    out: dict = {}
    for _, r in df.iterrows():
        key = (int(r["team_id"]), int(r["season"]))
        if key in out:
            continue        # already have the plurality coach for this team-season
        out[key] = {
            "coach_id":   r.get("coach_id"),
            "coach_name": r.get("coach_name"),
            "games":      float(r.get("games") or 0),
            "wins":       r.get("wins"),
            "losses":     r.get("losses"),
            "sp_overall": r.get("sp_overall"),
        }
    return out


def coaching_changes(season: int, hc_map: dict | None = None) -> set:
    """{team_id} that has a different head coach than the season before.

    Falls back to the legacy hand-seeded `coaching_changes` table when the
    harvest is absent, so a fresh clone without script 09 still runs — it just
    knows about twenty changes instead of hundreds.
    """
    hc = hc_map if hc_map is not None else head_coach_by_team_season()
    if hc:
        changed = set()
        for (tid, s), cur in hc.items():
            if s != season:
                continue
            prev = hc.get((tid, season - 1))
            # No prior season is not a change — it is the start of our record, and
            # calling it a coaching change would flag every team in 2008.
            if prev is None:
                continue
            if _name(prev) and _name(cur) and _name(prev) != _name(cur):
                changed.add(int(tid))
        return changed

    legacy = read_raw("coaching_changes")
    if legacy.empty:
        return set()
    legacy = legacy[(legacy["start_season"] == season)
                    & (legacy["role"].isin(["HC", "OC", "DC"]))
                    & legacy["team_id"].notna()]
    return set(legacy["team_id"].astype(int))


def _name(entry: dict) -> str:
    return str(entry.get("coach_name") or "").strip().lower()


def coach_tenures(hc_map: dict | None = None) -> list[dict]:
    """One row per continuous (coach, team) stint: first season, last, seasons.

    Continuity matters — a coach who returns to a school after ten years away is
    two stints, and averaging them would blur the very transition an event study
    is trying to see.
    """
    hc = hc_map if hc_map is not None else head_coach_by_team_season()
    by_team: dict = {}
    for (tid, season), entry in hc.items():
        by_team.setdefault(tid, []).append((season, _name(entry), entry))

    out: list[dict] = []
    for tid, rows in by_team.items():
        rows.sort()
        stint: dict | None = None
        prev_season = None
        for season, name, entry in rows:
            broken = (stint is None or name != stint["coach"]
                      or (prev_season is not None and season != prev_season + 1))
            if broken:
                if stint:
                    out.append(stint)
                stint = {"team_id": int(tid), "coach": name,
                         "coach_name": entry.get("coach_name"),
                         "first_season": season, "last_season": season, "seasons": 0}
            stint["last_season"] = season
            stint["seasons"] += 1
            prev_season = season
        if stint:
            out.append(stint)
    return out
