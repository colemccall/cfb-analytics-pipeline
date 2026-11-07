"""The export contract for sibling apps (scripts/21_export_contract.py → cfb-analytics-app/data/exports/v1/).

Another project will read these files without reading this one's code, so the file has to say everything: every field is in
schema.json in the order the data carries it, every value has the schema's type, and every null carries a reason from the
schema's vocabulary. Never a default number in place of a missing one: the tests check the cases where a default would be the
easy mistake (a withheld lineman, a player with no box-score rows, a team with no rating this season).
"""

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
OUT = ROOT.parent / "cfb-analytics-app" / "data" / "exports" / "v1"

pytestmark = pytest.mark.skipif(not (OUT / "manifest.json").exists(), reason="the v1 export is not written in this checkout")


def _read(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def records(name):
    d = _read(name)
    return [dict(zip(d["fields"], r)) for r in d["rows"]]


@pytest.fixture(scope="module")
def schema():
    return _read("schema.json")


@pytest.fixture(scope="module")
def players():
    return records("players.json")


@pytest.fixture(scope="module")
def teams():
    return records("teams.json")


def test_the_layout_follows_the_schema_field_for_field(schema):
    for name in ("teams.json", "players.json"):
        assert _read(name)["fields"] == [f["name"] for f in schema["files"][name]["fields"]], name


def test_every_value_has_its_type_and_every_null_its_reason(schema, teams, players):
    vocab = set(schema["null_reasons"])
    py = {"integer": int, "number": (int, float), "string": str, "object": dict}
    for name, rows in (("teams.json", teams), ("players.json", players)):
        spec = {f["name"]: f for f in schema["files"][name]["fields"]}
        for r in rows:
            assert set(r["nulls"].values()) <= vocab, (name, r["nulls"])
            for field, f in spec.items():
                v = r[field]
                if v is None:
                    assert f["nullable"], (name, field)
                    assert field in r["nulls"], f"{name}: {field} is null with no reason"
                else:
                    assert isinstance(v, py[f["type"]]) and not isinstance(v, bool), (name, field, v)
                    assert field not in r["nulls"], f"{name}: {field} has a value and a reason"
                    if "enum" in f:
                        assert v in f["enum"], (name, field, v)


def test_keys_are_unique_and_every_player_names_a_team_that_exists(teams, players):
    assert len({t["team_id"] for t in teams}) == len(teams)
    assert len({p["player_id"] for p in players}) == len(players)
    ids = {t["team_id"] for t in teams}
    assert all(p["team_id"] in ids for p in players)


def test_a_lineman_is_withheld_not_given_a_default(players):
    ol = [p for p in players if p["position_group"] == "OL"]
    assert ol, "no offensive linemen in the export"
    for p in ol:
        for f in ("projection", "rating_to_date"):
            assert p[f] is None and p["nulls"][f] == "withheld_ol", (p["player_id"], f)


def test_no_box_score_rows_is_null_games_not_zero(players):
    none = [p for p in players if p["nulls"].get("games_to_date") == "no_box_score_rows"]
    assert none, "expected players with no box-score rows yet"
    assert all(p["games_to_date"] is None and p["rating_to_date"] is None for p in none)
    assert all(p["games_to_date"] is None or p["games_to_date"] >= 1 for p in players)


def test_ratings_are_on_the_published_scale_and_no_sentinel_fills_a_gap(players):
    for f in ("rating_prev", "projection", "rating_to_date"):
        vals = [p[f] for p in players if p[f] is not None]
        assert vals and min(vals) >= 30 and max(vals) <= 99, f
    assert all(p["stars"] is None or 1 <= p["stars"] <= 5 for p in players)


def test_the_export_agrees_with_the_roster_file_it_was_built_from(players):
    rosters = json.loads((OUT.parent.parent / f"rosters_{_read('manifest.json')['season']}.json").read_text(encoding="utf-8"))
    src = {int(r["player_id"]): r for t in rosters.values() for r in t}
    assert len(src) == len(players)
    for p in players[::97]:
        r = src[p["player_id"]]
        assert p["rating_to_date"] == (None if r.get("live_rating") is None else float(r["live_rating"]))
        assert p["rating_prev"] == (None if r.get("rating_prev") is None else float(r["rating_prev"]))


def test_the_manifest_describes_the_files_beside_it(teams, players):
    m = _read("manifest.json")
    assert m["version"].split(".")[0] == "1" and m["rows"] == {"teams.json": len(teams), "players.json": len(players)}
    for name, digest in m["sha256"].items():
        assert hashlib.sha256((OUT / name).read_text(encoding="utf-8").encode("utf-8")).hexdigest() == digest, name
    assert m["model_fingerprint"] and m["release"]


def test_the_builder_validates_its_own_output():
    spec = importlib.util.spec_from_file_location("ec", ROOT / "scripts" / "21_export_contract.py")
    ec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ec)
    season = _read("manifest.json")["season"]
    assert ec.validate(ec.build_teams(season, ec._load(f"live_{season}.json")), ec.build_players(season)) == []
