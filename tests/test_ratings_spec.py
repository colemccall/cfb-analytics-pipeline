"""docs/RATINGS_SPEC.md says what the code says: the constants table in it is the one `scripts/spec_constants.py` generates now.

The spec is the one place a reader goes to learn what sets a rating, and a hand-typed table of constants is how a document drifts from the
code it describes (the way the registry once said CROSS_WEIGHT was 1.0 while the code had 1.5). So the table is generated, and this test
fails when a constant changes without the spec being regenerated: run `python scripts/spec_constants.py --update-spec`.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SPEC = ROOT / "docs" / "RATINGS_SPEC.md"
APP_METHODS = ROOT.parent / "cfb-analytics-app" / "js" / "methods.js"


def _constants():
    spec = importlib.util.spec_from_file_location("spec_constants", ROOT / "scripts" / "spec_constants.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_specs_constants_table_is_what_the_code_says_now():
    text = SPEC.read_text(encoding="utf-8")
    m = _constants()
    assert m.START in text and m.END in text, "the spec has no constants block: add the markers and run scripts/spec_constants.py --update-spec"
    have = text[text.index(m.START): text.index(m.END) + len(m.END)]
    assert have == m.block(), "a constant changed (or the table was edited by hand): run python scripts/spec_constants.py --update-spec"


def test_every_owner_setting_in_the_table_is_labelled_as_the_owners():
    rows = _constants().rows()
    owner = {name for name, _, _, who, _ in rows if who == "owner"}
    for needed in ("OPP_FCS_MULT", "QB composite", "W_ROSTER_BLEND"):
        assert any(n.startswith(needed) for n in owner), f"{needed} is an owner setting and the table does not say so"


def test_the_frozen_constants_claude_md_names_are_marked_frozen():
    rows = _constants().rows()
    frozen = " ".join(name for name, _, _, _, fz in rows if fz == "frozen")
    for needed in ("EDGE_OVR_ANCHORS", "PLAYTIME_TIERS", "COMPOSITE_OVR_ANCHORS"):
        assert needed in frozen, f"{needed} is frozen in CLAUDE.md section 3 and the table does not say so"


def test_the_site_carries_the_spec_as_a_methods_entry_that_points_back_here():
    if not APP_METHODS.exists():
        pytest.skip("cfb-analytics-app not present in this checkout")
    src = APP_METHODS.read_text(encoding="utf-8")
    assert 'id: "system-spec"' in src, "the Methods registry has no system-spec entry (the site's copy of the spec)"
    assert "RATINGS_SPEC.md" in src, "the system-spec entry does not name docs/RATINGS_SPEC.md as its source"
