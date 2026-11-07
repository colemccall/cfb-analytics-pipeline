"""Frozen weekly snapshots (scripts/22_freeze_snapshot.py) and the model fingerprint (utils/model_version.py).

A snapshot is the record of what the site showed in one week under one model; it is only worth having if it cannot change.
These tests pin the four promises: a snapshot is never overwritten, a run that changes nothing is refused, every step is
classified as data, model or both, and every snapshot on disk still hashes to what its manifest recorded. The fingerprint
must ignore words about the code (docstrings, comments) and notice any change to the code itself.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SNAP_ROOT = ROOT.parent / "cfb-analytics-app" / "data" / "snapshots"


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, ROOT / name)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


S = _load("scripts/22_freeze_snapshot.py", "freeze")
MV = _load("utils/model_version.py", "model_version")


def live(week=9, rating=80.0):
    """A two-team, two-player live file."""
    return {"season": 2026, "through_week": week, "generated_at": "2026-11-02T00:00:00+00:00", "games_completed": 100, "games_scheduled": 300,
            "games_after_through_week": 4,
            "teams": [{"team_id": 1, "school": "A", "overall_rating": 90.0, "offense_rating": 88.0, "defense_rating": 91.0, "power_rating": 12.5,
                       "roster_half": 85.0, "stats_half": 92.0, "results_half": 93.0, "wins": 8, "losses": 1},
                      {"team_id": 2, "school": "B", "overall_rating": None, "offense_rating": None, "defense_rating": None, "power_rating": -3.0,
                       "roster_half": 70.0, "stats_half": None, "results_half": None, "wins": 3, "losses": 6}],
            "players": [{"player_id": 10, "team_id": 1, "position_group": "QB", "overall_rating": rating, "rating_basis": "production", "games_played": 9},
                        {"player_id": 11, "team_id": 2, "position_group": "OL", "overall_rating": None, "rating_basis": None, "games_played": None}]}


def freeze(root, lv, release="v1", fp="f" * 64):
    text = json.dumps(lv)
    return S.freeze(lv, "live_2026.json", text, release, fp, root=root, now="2026-11-06T00:00:00+00:00")


class TestFreeze:
    def test_the_first_snapshot_is_written_with_its_stamp_and_listed(self, tmp_path):
        m = freeze(tmp_path, live())
        assert m["id"] == "2026-wk09-v1" and m["kind"] == "first" and m["previous"] is None
        assert m["games"] == {"completed": 100, "scheduled": 300, "after_through_week": 4}
        assert (tmp_path / "2026-wk09-v1" / "teams.json").exists() and S.verify(tmp_path) == []
        assert [e["id"] for e in S.read_index(tmp_path)["snapshots"]] == ["2026-wk09-v1"]

    def test_a_missing_value_stays_null_and_an_unrated_player_is_not_listed(self, tmp_path):
        freeze(tmp_path, live())
        teams = json.loads((tmp_path / "2026-wk09-v1" / "teams.json").read_text(encoding="utf-8"))
        b = dict(zip(teams["fields"], teams["rows"][1]))
        assert b["rating"] is None and b["rank_rating"] is None and b["rank_power"] == 2
        players = json.loads((tmp_path / "2026-wk09-v1" / "players.json").read_text(encoding="utf-8"))
        assert [r[0] for r in players["rows"]] == [10]

    def test_a_snapshot_is_never_overwritten(self, tmp_path):
        freeze(tmp_path, live())
        with pytest.raises(S.Refused, match="never overwritten"):
            freeze(tmp_path, live(rating=99.0))

    def test_a_run_that_changes_nothing_is_refused_even_under_a_new_release_label(self, tmp_path):
        freeze(tmp_path, live())
        with pytest.raises(S.Refused, match="nothing changed"):
            freeze(tmp_path, live(), release="v2")

    def test_each_step_is_classified(self, tmp_path):
        freeze(tmp_path, live(week=9))
        assert freeze(tmp_path, live(week=10))["kind"] == "data"                              # a week passed, same model
        assert freeze(tmp_path, live(week=10), release="v2", fp="e" * 64)["kind"] == "model"  # same week, new code
        m = freeze(tmp_path, live(week=11), release="v3", fp="d" * 64)
        assert m["kind"] == "data+model" and m["previous"] == "2026-wk10-v2"

    def test_a_snapshot_that_changed_after_freezing_stops_the_next_run(self, tmp_path):
        freeze(tmp_path, live())
        (tmp_path / "2026-wk09-v1" / "players.json").write_text('{"fields":[],"rows":[]}', encoding="utf-8")
        assert S.verify(tmp_path) == ["2026-wk09-v1/players.json: changed since it was frozen"]
        with pytest.raises(S.Refused, match="no longer verifies"):
            freeze(tmp_path, live(week=10))


class TestFingerprint:
    def test_words_about_the_code_do_not_change_it(self):
        a = 'def f(x):\n    """What f does."""\n    return x + 1  # one\n'
        b = 'def f(x):\n    # a different comment\n    return x + 1\n'
        assert MV.source_fingerprint(a) == MV.source_fingerprint(b)

    def test_any_change_to_the_code_does(self):
        assert MV.source_fingerprint("def f(x):\n    return x + 1\n") != MV.source_fingerprint("def f(x):\n    return x + 2\n")

    def test_it_covers_the_rating_chain_and_not_the_harvest_client(self):
        files = MV.model_files()
        for needed in ("scripts/06_compute_edge_scores.py", "scripts/07_compute_player_ratings.py", "scripts/10_compute_team_ratings.py",
                       "scripts/17_live_season.py", "utils/opponent_strength.py"):
            assert needed in files
        assert "utils/api_client.py" not in files and all((ROOT / f).exists() for f in files)


@pytest.mark.skipif(not (SNAP_ROOT / "index.json").exists(), reason="no snapshots frozen in this checkout")
class TestTheFrozenRecord:
    def test_every_snapshot_on_disk_still_hashes_to_its_manifest(self):
        assert S.verify(SNAP_ROOT) == []

    def test_the_index_lists_each_snapshot_once_and_each_names_its_previous(self):
        entries = S.read_index(SNAP_ROOT)["snapshots"]
        ids = [e["id"] for e in entries]
        assert len(ids) == len(set(ids))
        for i, e in enumerate(entries):
            earlier = [x["id"] for x in entries[:i] if x["season"] == e["season"]]
            assert e["previous"] == (earlier[-1] if earlier else None)
