"""Which model produced a number: the release label and a fingerprint of the rating code. Read-only.

The frozen weekly snapshots (scripts/22_freeze_snapshot.py) and the export contract (scripts/21_export_contract.py) stamp every
file with both, so a week-over-week change can be classified as a DATA change (new games, same model) or a MODEL change (same
games, different code) without trusting anyone's memory of what shipped when.

  release      CONFIG.VERSION in the app's js/config.js: the human label of what shipped ("v4.25.1"). One source:
               tests/test_frontend_config.py already holds it equal to the newest Build Log entry.
  fingerprint  sha256 over the syntax trees of the code that computes a published number (MODEL_FILES), with docstrings
               removed and comments ignored by the parser. A docstring or comment edit does not change it; any code edit
               does, including one that happens to move no number (the change view then shows a model step that moved 0).
"""

import ast
import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
APP_CONFIG = ROOT.parent / "cfb-analytics-app" / "js" / "config.js"

# The code between the raw data and a published rating: the rating chain (06, 07, 10), the projections (15, 16), the live and
# weekly builders (17, 17b), the export that shapes the app's numbers (12), and every utility they import except the harvest
# client. Engine B (11) is included because script 12 still exports what it writes.
MODEL_FILES = [
    "scripts/06_compute_edge_scores.py", "scripts/07_compute_player_ratings.py", "scripts/10_compute_team_ratings.py",
    "scripts/11_compute_engine_b_ratings.py", "scripts/12_export_frontend_json.py", "scripts/15_predict_trajectories.py",
    "scripts/16_project_ratings.py", "scripts/17_live_season.py", "scripts/17b_weekly_progression.py",
]
MODEL_UTILS_EXCLUDED = {"api_client.py", "model_version.py", "__init__.py"}


def model_files() -> list:
    """The files the fingerprint covers, relative to the pipeline root, in a fixed order."""
    utils = sorted(f"utils/{p.name}" for p in (ROOT / "utils").glob("*.py") if p.name not in MODEL_UTILS_EXCLUDED)
    return MODEL_FILES + utils


class _NoDocstrings(ast.NodeTransformer):
    """Removes the docstring of a module, class or function: words about the code are not the code."""

    def _strip(self, node):
        """Visit the children first, then drop this node's leading string expression (its docstring), keeping the body non-empty."""
        self.generic_visit(node)
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
        return node

    visit_Module = visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _strip


def source_fingerprint(source: str) -> str:
    """sha256 of one file's syntax tree without docstrings (comments never reach the tree)."""
    tree = _NoDocstrings().visit(ast.parse(source))
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode("utf-8")).hexdigest()


def fingerprint() -> str:
    """The model fingerprint: sha256 over (path, per-file fingerprint) for every file in model_files()."""
    h = hashlib.sha256()
    for rel in model_files():
        h.update(rel.encode("utf-8"))
        h.update(source_fingerprint((ROOT / rel).read_text(encoding="utf-8")).encode("ascii"))
    return h.hexdigest()


def release() -> str | None:
    """CONFIG.VERSION from the app's js/config.js, or None when the app is not checked out beside the pipeline."""
    if not APP_CONFIG.exists():
        return None
    m = re.search(r'VERSION:\s*"([^"]+)"', APP_CONFIG.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def pipeline_commit() -> dict:
    """The pipeline's git commit and whether its working tree differs from it (a stamp from a dirty tree says so)."""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip())
        return {"commit": head, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}
