"""Constants in the app's js/config.js that must agree with files the exporter wrote.

config.js loads synchronously before any fetch, so it cannot discover what is on
disk at runtime; each constant here mirrors something the pipeline produced, and
this is what stops the two drifting. Same pattern as
test_export_contract.py::test_frontend_season_constants_agree.
"""

import re
from pathlib import Path

import pytest

APP = Path(__file__).parent.parent.parent / "cfb-analytics-app"
APP_JS = APP / "js" / "config.js"
APP_DATA = APP / "data"

pytestmark = pytest.mark.skipif(
    not (APP_JS.exists() and APP_DATA.exists()),
    reason="cfb-analytics-app not present in this checkout",
)


def _js_int_list(name: str) -> list[int]:
    m = re.search(rf"{name}:\s*\[([^\]]*)\]", APP_JS.read_text(encoding="utf-8"))
    assert m, f"config.js has no {name}"
    return [int(x) for x in re.findall(r"\d{4}", m.group(1))]


def test_no_ea_data_is_exported_or_referenced():
    """EA CFB 27 ratings were removed on 2026-10-27. Nothing may bring them back.

    No ea_ratings_* file in the export, no EA_SEASONS constant in config.js, and
    no ea_ovr column on any exported player row. A third-party's ratings were
    published whole once; this is the guard that keeps the removal a removal.
    """
    assert not list(APP_DATA.glob("ea_ratings*.json")), "an EA ratings file is being exported"
    assert "EA_SEASONS" not in APP_JS.read_text(encoding="utf-8"), "config.js still lists EA seasons"
    players = APP_DATA / "players_2026.json"
    if players.exists():
        import json
        rows = json.loads(players.read_text(encoding="utf-8"))
        assert not any("ea_ovr" in r for r in rows), "players_2026.json still carries ea_ovr"


def test_transfer_seasons_match_exported_files():
    """TRANSFER_SEASONS must list exactly the seasons with a transfers_{season}.json.

    The League page's portal tab offers these seasons. One listed without a file
    is a 404 on that tab; a file not listed is a season of moves the site never
    shows.
    """
    on_disk = sorted(int(m.group(1)) for p in APP_DATA.glob("transfers_*.json")
                     if (m := re.fullmatch(r"transfers_(\d{4})\.json", p.name)))
    assert _js_int_list("TRANSFER_SEASONS") == on_disk, (
        f"config.js TRANSFER_SEASONS {_js_int_list('TRANSFER_SEASONS')} != exported files {on_disk}"
    )


def test_live_seasons_match_exported_files():
    """LIVE_SEASONS must list exactly the seasons with a live_{season}.json.

    The '26 page asks for the file only for a listed season. One listed without a
    file is a 404 on every visit; a file not listed is a season of part-season
    ratings the site never shows.
    """
    on_disk = sorted(int(m.group(1)) for p in APP_DATA.glob("live_*.json")
                     if (m := re.fullmatch(r"live_(\d{4})\.json", p.name)))
    assert _js_int_list("LIVE_SEASONS") == on_disk, (
        f"config.js LIVE_SEASONS {_js_int_list('LIVE_SEASONS')} != exported files {on_disk}"
    )


def test_recruiting_seasons_match_exported_files():
    """RECRUITING_SEASONS must list exactly the class years with a recruiting_{year}.json.

    The menu's Recruiting screen offers these years. One listed without a file is a 404;
    a file not listed is a class the site never shows.
    """
    on_disk = sorted(int(m.group(1)) for p in APP_DATA.glob("recruiting_[0-9]*.json")
                     if (m := re.fullmatch(r"recruiting_(\d{4})\.json", p.name)))
    assert _js_int_list("RECRUITING_SEASONS") == on_disk, (
        f"config.js RECRUITING_SEASONS {_js_int_list('RECRUITING_SEASONS')} != exported files {on_disk}"
    )


def test_card_shard_count_matches_the_export_script():
    """CONFIG.CARD_SHARDS must equal script 12's CARD_SHARDS: the shard a card is read from is
    player_id % CARD_SHARDS in both places, and a mismatch reads the wrong file for most players."""
    js = int(re.search(r"CARD_SHARDS:\s*(\d+)", (APP_DATA.parent / "js" / "config.js").read_text(encoding="utf-8")).group(1))
    py = int(re.search(r"^CARD_SHARDS = (\d+)", (Path(__file__).parent.parent / "scripts" / "12_export_frontend_json.py")
                       .read_text(encoding="utf-8"), re.M).group(1))
    assert js == py, f"config.js CARD_SHARDS {js} != script 12 CARD_SHARDS {py}"


def test_power_game_seasons_match_exported_files():
    """POWER_GAME_SEASONS must list exactly the seasons with a power_games_{season}.json.

    The Rankings screen asks for these to explain each ranking. One listed without a file is a 404;
    a file not listed is an explanation the screen never shows.
    """
    on_disk = sorted(int(m.group(1)) for p in APP_DATA.glob("power_games_[0-9]*.json")
                     if (m := re.fullmatch(r"power_games_(\d{4})\.json", p.name)))
    assert _js_int_list("POWER_GAME_SEASONS") == on_disk, (
        f"config.js POWER_GAME_SEASONS {_js_int_list('POWER_GAME_SEASONS')} != exported files {on_disk}"
    )


def test_headline_weights_match_script_10():
    """HEADLINE_WEIGHTS in config.js (the Compare screen's shares) must equal the blend script 10 computes the headline with."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_s10cfg", Path(__file__).parent.parent / "scripts" / "10_compute_team_ratings.py")
    s10 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s10)
    m = re.search(r"HEADLINE_WEIGHTS:\s*\{\s*roster:\s*([\d./ ]+),\s*stats:\s*([\d./ ]+),\s*results:\s*([\d./ ]+)\s*\}", APP_JS.read_text(encoding="utf-8"))
    assert m, "config.js has no HEADLINE_WEIGHTS"
    roster, stats, results = (eval(x, {"__builtins__": {}}) for x in m.groups())
    assert (roster, stats, results) == pytest.approx((s10.W_ROSTER_BLEND, s10.W_STATS_BLEND, s10.W_RESULTS_BLEND))
