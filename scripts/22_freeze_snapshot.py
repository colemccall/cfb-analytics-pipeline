"""Freeze this week's published numbers into a dated, versioned, write-once folder: cfb-analytics-app/data/snapshots/.

Every week the live file (live_{season}.json) is rebuilt and the week-by-week replay (17b) is rebuilt from scratch with today's
code, so neither can say what the site showed on a given day under the model of that day. A snapshot can: it is the published
team and player numbers of one completed week, stamped with the release, a fingerprint of the rating code
(utils/model_version.py), the run date and the games-played state, and it is never regenerated or overwritten.

    data/snapshots/index.json                      every snapshot, oldest first (append-only)
    data/snapshots/{season}-wk{NN}-{release}/
        manifest.json                              the stamp, what kind of step this was, and each file's sha256
        teams.json                                 {fields, rows}: every team's published ratings, record and ranks
        players.json                               {fields, rows}: every rated player's published rating, basis and games

What kind of step a snapshot is, against the season's previous one:
    first        the season's first snapshot
    data         new games, same model fingerprint            (a week passed)
    model        same week, new fingerprint                   (a release changed the code; the change view's model step)
    data+model   both                                         (the change view must separate them with the replay, docs/SNAPSHOTS.md)
A run that changes neither is refused (the numbers are already frozen), and so is a model change without a new release label
(its folder name would collide: bump CONFIG.VERSION and write the Build Log entry first).

Before writing, every snapshot already listed is re-hashed; if any file differs from its manifest the run stops, because a
frozen snapshot that changed is the one failure this folder exists to prevent.

    python scripts/22_freeze_snapshot.py                         freeze the current season's live file
    python scripts/22_freeze_snapshot.py --dry-run               say what would be written; write nothing
    python scripts/22_freeze_snapshot.py --note "..."            a sentence kept in the manifest (e.g. why it was frozen late)
    python scripts/22_freeze_snapshot.py --verify                re-hash every snapshot and exit
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from utils import model_version  # noqa: E402

APP_DATA = ROOT.parent / "cfb-analytics-app" / "data"
SNAP_ROOT = APP_DATA / "snapshots"
TEAM_FIELDS = ["team_id", "school", "rating", "offense", "defense", "power", "roster", "stats", "results", "wins", "losses",
               "rank_rating", "rank_power"]
PLAYER_FIELDS = ["player_id", "team_id", "position_group", "rating", "basis", "games"]


class Refused(Exception):
    """A snapshot that must not be written (it exists, nothing changed, or the frozen record no longer verifies)."""


def snapshot_id(season: int, week: int, release: str) -> str:
    return f"{season}-wk{int(week):02d}-{release}"


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ranks(rows: list, key: str) -> dict:
    """1 = best; teams without a value get no rank."""
    have = sorted((r for r in rows if r.get(key) is not None), key=lambda r: -r[key])
    return {r["team_id"]: i + 1 for i, r in enumerate(have)}


def slim(live: dict) -> tuple:
    """The published numbers of a live file as two {fields, rows} tables (a missing value stays null)."""
    teams = live["teams"]
    rk_rating, rk_power = _ranks(teams, "overall_rating"), _ranks(teams, "power_rating")
    trows = [[t["team_id"], t.get("school"), t.get("overall_rating"), t.get("offense_rating"), t.get("defense_rating"), t.get("power_rating"),
              t.get("roster_half"), t.get("stats_half"), t.get("results_half"), t.get("wins"), t.get("losses"),
              rk_rating.get(t["team_id"]), rk_power.get(t["team_id"])] for t in sorted(teams, key=lambda t: t["team_id"])]
    players = [p for p in live["players"] if p.get("overall_rating") is not None]
    prows = [[p["player_id"], p.get("team_id"), p.get("position_group"), p.get("overall_rating"), p.get("rating_basis"), p.get("games_played")]
             for p in sorted(players, key=lambda p: p["player_id"])]
    return {"fields": TEAM_FIELDS, "rows": trows}, {"fields": PLAYER_FIELDS, "rows": prows}


def read_index(root: Path = SNAP_ROOT) -> dict:
    path = root / "index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"snapshots": []}


def verify(root: Path = SNAP_ROOT) -> list:
    """Every way the frozen record has drifted: a listed snapshot missing, or a file whose sha256 differs from its manifest."""
    problems = []
    for entry in read_index(root)["snapshots"]:
        folder = root / entry["id"]
        mpath = folder / "manifest.json"
        if not mpath.exists():
            problems.append(f"{entry['id']}: listed in index.json but has no manifest")
            continue
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        for name, digest in manifest["files"].items():
            f = folder / name
            if not f.exists():
                problems.append(f"{entry['id']}/{name}: missing")
            elif _sha(f.read_text(encoding="utf-8")) != digest:
                problems.append(f"{entry['id']}/{name}: changed since it was frozen")
    return problems


def classify(previous: dict | None, week: int, fingerprint: str) -> str:
    """What kind of step the new snapshot is against the season's previous manifest; raises Refused when nothing changed."""
    if previous is None:
        return "first"
    same_week = int(previous["week"]) == int(week)
    same_model = previous["model_fingerprint"] == fingerprint
    if same_week and same_model:
        raise Refused(f"nothing changed since {previous['id']} (same week, same model fingerprint): the numbers are already frozen")
    return "model" if same_week else "data" if same_model else "data+model"


def freeze(live: dict, source_name: str, source_text: str, release: str, fingerprint: str, root: Path = SNAP_ROOT,
           note: str | None = None, dry_run: bool = False, now: str | None = None) -> dict:
    """Write one snapshot (or, with dry_run, return the manifest it would have) and append it to the index."""
    problems = verify(root)
    if problems:
        raise Refused("the frozen record no longer verifies: " + "; ".join(problems[:5]))
    season, week = int(live["season"]), int(live["through_week"])
    sid = snapshot_id(season, week, release)
    folder = root / sid
    index = read_index(root)
    if folder.exists() or any(e["id"] == sid for e in index["snapshots"]):
        raise Refused(f"{sid} already exists; a snapshot is never overwritten (a model change needs a new release label)")
    same_season = [e for e in index["snapshots"] if e["season"] == season]
    previous = json.loads((root / same_season[-1]["id"] / "manifest.json").read_text(encoding="utf-8")) if same_season else None
    kind = classify(previous, week, fingerprint)
    teams, players = slim(live)
    texts = {"teams.json": _dump(teams), "players.json": _dump(players)}
    manifest = {
        "id": sid, "season": season, "week": week, "kind": kind, "previous": previous["id"] if previous else None,
        "release": release, "model_fingerprint": fingerprint, "pipeline": model_version.pipeline_commit(),
        "frozen_at": now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"file": source_name, "generated_at": live.get("generated_at"), "sha256": _sha(source_text)},
        "games": {"completed": live.get("games_completed"), "scheduled": live.get("games_scheduled"),
                  "after_through_week": live.get("games_after_through_week")},
        "counts": {"teams": len(teams["rows"]), "players": len(players["rows"])},
        "files": {name: _sha(t) for name, t in texts.items()},
        **({"note": note} if note else {}),
    }
    if dry_run:
        return manifest
    folder.mkdir(parents=True)
    for name, text in texts.items():
        (folder / name).write_text(text, encoding="utf-8")
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    index["snapshots"].append({"id": sid, "season": season, "week": week, "kind": kind, "release": release,
                               "model_fingerprint": fingerprint[:16], "frozen_at": manifest["frozen_at"], "previous": manifest["previous"]})
    (root / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return manifest


def main() -> int:
    """Freeze the current season's live file, or verify the frozen record."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--season", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--note")
    args = ap.parse_args()
    if args.verify:
        problems = verify()
        n = len(read_index()["snapshots"])
        print(f"{n} snapshot(s); " + ("all verify" if not problems else f"{len(problems)} problem(s): " + "; ".join(problems)))
        return 1 if problems else 0
    season = args.season or json.loads((APP_DATA / "manifest.json").read_text(encoding="utf-8"))["current_season"]
    name = f"live_{season}.json"
    text = (APP_DATA / name).read_text(encoding="utf-8")
    release = model_version.release()
    if not release:
        print("no release label (the app's js/config.js was not found); a snapshot needs one")
        return 1
    try:
        m = freeze(json.loads(text), name, text, release, model_version.fingerprint(), note=args.note, dry_run=args.dry_run)
    except Refused as e:
        print(f"refused: {e}")
        return 1
    print(f"{'would write' if args.dry_run else 'froze'} {m['id']} ({m['kind']}; previous {m['previous']}): {m['counts']['teams']} teams, "
          f"{m['counts']['players']} players; model {m['model_fingerprint'][:16]}; source generated {m['source']['generated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
