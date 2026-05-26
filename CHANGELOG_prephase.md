# Changelog — Pre-Phase (v2.0.0 → v3.0.0 clean-arch)
Generated: 2026-05-24
Status: PROPOSED — no code changes have been made yet. This document is the review gate.

Every proposed change is listed below with file, line number, before text, after text, reason, and risk.
Changes are organized by Phase. **No edit happens until it appears in this table and is reviewed.**

---

## PHASE 0 — Version Badge (all 6 HTML files + styles.css)

Goal: Display `v2.0.0` in the sidebar bottom on every page.

### Change 001
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/index.html` |
| **Line** | 162 (after the closing `</div>` of the theme-swatches div, inside `.sidebar-bottom`) |
| **Before** | `    </div>` ← closing the inner div containing theme swatches |
| **After** | `    </div>\n    <div class="sidebar-version">v2.0.0</div>` |
| **Reason** | Display version in sidebar bottom per plan |
| **Risk** | Low — additive only, layout-contained |

### Change 002
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/players.html` |
| **Line** | 31 (after the closing `</div>` of theme-swatches, inside `.sidebar-bottom`) |
| **Before** | `    </div>` ← closing the inner div |
| **After** | `    </div>\n    <div class="sidebar-version">v2.0.0</div>` |
| **Reason** | Version badge — players page sidebar |
| **Risk** | Low |

### Change 003
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/teams.html` |
| **Line** | 31 (same pattern — after theme-swatches div, inside `.sidebar-bottom`) |
| **Before** | `    </div>` |
| **After** | `    </div>\n    <div class="sidebar-version">v2.0.0</div>` |
| **Reason** | Version badge — teams page sidebar |
| **Risk** | Low |

### Change 004
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/ratings.html` |
| **Line** | 31 (same pattern) |
| **Before** | `    </div>` |
| **After** | `    </div>\n    <div class="sidebar-version">v2.0.0</div>` |
| **Reason** | Version badge — ratings page sidebar |
| **Risk** | Low |

### Change 005
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/research.html` |
| **Line** | 49 (after theme-swatches div, inside `.sidebar-bottom`) |
| **Before** | `    </div>` |
| **After** | `    </div>\n    <div class="sidebar-version">v2.0.0</div>` |
| **Reason** | Version badge — research page sidebar |
| **Risk** | Low |

### Change 006
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/info.html` |
| **Line** | 115 (after theme-swatches div, inside `.sidebar-bottom`) |
| **Before** | `    </div>` |
| **After** | `    </div>\n    <div class="sidebar-version">v2.0.0</div>` |
| **Reason** | Version badge — info page sidebar |
| **Risk** | Low |

### Change 007
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/styles.css` |
| **Line** | After line 187 (end of `.sidebar-bottom` rule block), before `.sidebar-season-wrap label` |
| **Before** | *(nothing — new rule)* |
| **After** | `.sidebar-version {\n  font-size: var(--fs-xs); color: var(--text-muted);\n  text-align: center; padding-top: 4px;\n  letter-spacing: 0.4px;\n}` |
| **Reason** | Style the version badge — small, muted, respects theme via CSS variable |
| **Risk** | Low — new rule, no conflict |

---

## PHASE 1 — Merge local-arch → main, Tag v2.0.0

No code file changes. Git operations only:
1. `git checkout main && git merge local-arch`
2. Verify: run `python scripts/12_export_frontend_json.py` and confirm `cfb-analytics-app/data/` files populate
3. Verify: serve frontend locally and confirm players, teams, ratings pages load
4. `git tag v2.0.0`

---

## PHASE 2 — v3: clean-arch Branch

Branch: `git checkout -b clean-arch` from main after v2.0.0 tag.

### PIPELINE — utils/

#### Change 101
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/utils/supabase_client.py` |
| **Action** | **REMOVE FILE** (move to `utils/archive/supabase_client.py` or delete) |
| **Before** | 17-line file: `from supabase import create_client, Client` + `get_client()` singleton |
| **After** | File deleted. Import removed from `requirements.txt` |
| **Reason** | No active callers in any local-arch script (01–03, 06–07, 10, 12). Scripts 04, 05, 08, 08b, 11 use `get_connection()` from `db.py`, not `get_client()`. |
| **Risk** | Low — verify no remaining imports: `grep -r "supabase_client" scripts/` before deleting |

#### Change 102
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/requirements.txt` |
| **Line** | 2: `supabase==2.10.0` |
| **Before** | `supabase==2.10.0` |
| **After** | *(line removed)* |
| **Reason** | Companion to Change 101. No scripts import supabase SDK in local-arch. |
| **Risk** | Low — only after confirming no supabase imports remain |

#### Change 103
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/utils/api_client.py` |
| **Line** | 55: `resp = requests.get(url, headers=headers, params=params, timeout=30, verify=False)` |
| **Before** | `resp = requests.get(url, headers=headers, params=params, timeout=30, verify=False)` |
| **After** | `resp = requests.get(url, headers=headers, params=params, timeout=30, verify=False)  # SSL verification disabled — CFB API uses a cert that fails on some Windows environments` |
| **Reason** | Document WHY verify=False is present; prevents future contributor from silently enabling it before understanding the environment constraint |
| **Risk** | None — comment only |

#### Change 104
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/utils/api_client.py` |
| **Action** | Add Google-style docstrings to all public functions (20+ functions) |
| **Before** | Functions have no docstrings or single-line comments |
| **After** | Each function gets a docstring: `"""Short summary.\n\nArgs:\n    ...\nReturns:\n    ...\n"""` |
| **Reason** | PEP 257 / Google style per clean-arch standard |
| **Risk** | None — documentation only |

#### Change 105
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/utils/db.py` |
| **Line** | 28: `print(f"  [db] Connected via {'pooler' if 'pooler' in url else 'direct'}")` |
| **Before** | `print(f"  [db] Connected via {'pooler' if 'pooler' in url else 'direct'}")` |
| **After** | *(line removed — or moved behind `--verbose` flag)* |
| **Reason** | Bare print statements pollute stdout. Per clean-arch standard, logging goes behind a verbose flag. |
| **Risk** | Low — purely diagnostic output |

#### Change 106
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/utils/db.py`, `utils/api_client.py`, `utils/store.py` |
| **Action** | Add type hints to all function signatures |
| **Before** | e.g., `def bulk_upsert(table, rows, conflict_col, conflict_where=None):` |
| **After** | e.g., `def bulk_upsert(table: str, rows: list[dict], conflict_col: str, conflict_where: str | None = None) -> int:` |
| **Reason** | PEP 8 / type-hint standard for clean-arch |
| **Risk** | None — Python type hints are not enforced at runtime |

---

### PIPELINE — scripts/ (archive candidates)

#### Change 201
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/00_dump_supabase_to_json.py` |
| **Action** | Move to `scripts/archive/00_dump_supabase_to_json.py` |
| **Before** | File in `scripts/` |
| **After** | File in `scripts/archive/` with header comment: `# ARCHIVED: One-time migration utility. Already run. Do not re-run.` |
| **Reason** | One-time migration completed. Keeping in main scripts/ creates confusion about run order. |
| **Risk** | None — archive, not deletion |

#### Change 202
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/09_backfill_defender_ids.py` |
| **Action** | Move to `scripts/archive/09_backfill_defender_ids.py` |
| **Before** | File in `scripts/` |
| **After** | File in `scripts/archive/` with header comment: `# ARCHIVED: One-time backfill. Already run. Do not re-run.` |
| **Reason** | One-time backfill completed. |
| **Risk** | None — archive, not deletion |

---

### PIPELINE — script 01

#### Change 211
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/01_harvest_games_players_stats.py` |
| **Action** | Add Google-style docstrings to all public functions: `save_teams()`, `save_players()`, `save_games()`, `save_season_stats()`, `save_postseason_stats()`, `save_game_stats()` |
| **Before** | No docstrings |
| **After** | Each function gets docstring explaining: what table/file it writes, key args, side effects |
| **Reason** | PEP 257 / Google-style per clean-arch standard |
| **Risk** | None |

#### Change 212
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/01_harvest_games_players_stats.py` |
| **Action** | Add type hints to all function signatures |
| **Before** | `def save_teams(conn, year):` |
| **After** | `def save_teams(year: int) -> None:` (note: local-arch functions don't take a db conn) |
| **Reason** | PEP 8 type-hint standard |
| **Risk** | None |

#### Change 213
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/01_harvest_games_players_stats.py` |
| **Action** | Add section header comments throughout (`# ── TEAMS ──────────────────────────────────────────────────────────`) |
| **Reason** | Clean-arch standard: HTML-style section comments for navigation |
| **Risk** | None |

---

### PIPELINE — script 02

#### Change 221
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/02_harvest_recruiting.py` |
| **Line** | 166: `for school in ps_map.get(int(pid), {""}) :` |
| **Before** | `for school in ps_map.get(int(pid), {""}) :` (trailing space before colon) |
| **After** | `for school in ps_map.get(int(pid), {""}) :` → `for school in ps_map.get(int(pid), {""}):`  |
| **Reason** | PEP 8: no space before colon in compound statements |
| **Risk** | None |

#### Change 222
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/02_harvest_recruiting.py` |
| **Action** | Add type hints + Google-style docstrings to: `fuzzy_match_player()`, `build_player_name_index()`, `upsert_recruiting()` |
| **Reason** | PEP 8 / clean-arch standard |
| **Risk** | None |

---

### PIPELINE — script 03

#### Change 231
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/03_harvest_transfers.py` |
| **Action** | Add type hints + Google-style docstrings to: `fuzzy_match_player()`, `upsert_transfers()` |
| **Reason** | PEP 8 / clean-arch standard |
| **Risk** | None |

---

### PIPELINE — script 04 (broken SQL fix)

#### Change 241
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/04_harvest_nil_valuations.py` |
| **Lines** | 190–203: `build_player_index()` function |
| **Before** | `cur.execute("SELECT p.id, p.name, t.school FROM players p LEFT JOIN teams t ON t.id = p.team_id")` |
| **After** | `cur.execute("SELECT DISTINCT ON (p.id) p.id, p.name, t.school FROM players p JOIN player_seasons ps ON ps.player_id = p.id JOIN teams t ON t.id = ps.team_id ORDER BY p.id, ps.season DESC")` |
| **Reason** | `players.team_id` does not exist in v2 schema. Must join through `player_seasons`. DISTINCT ON p.id picks the most recent team per player. |
| **Risk** | Medium — changes player matching logic. Must verify player index is populated correctly after fix. |

---

### PIPELINE — script 05

#### Change 251
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/05_load_coaching_changes.py` |
| **Line** | 230: `start_season = int(years[0]) if years else 2024` |
| **Before** | `start_season = int(years[0]) if years else 2024` |
| **After** | `import datetime` (at module top) + `start_season = int(years[0]) if years else datetime.date.today().year` |
| **Reason** | Hardcoded 2024 fallback will produce wrong data in 2025+. Use current year dynamically. |
| **Risk** | Low |

---

### PIPELINE — script 06

#### Change 261
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/06_compute_edge_scores.py` |
| **Action** | Add type hints + Google-style docstrings to all public functions |
| **Reason** | PEP 8 / clean-arch standard |
| **Risk** | None |

#### Change 262
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/06_compute_edge_scores.py` |
| **Action** | Add section header comments (e.g., `# ── OFFENSIVE EDGE ──`, `# ── DEFENSIVE EDGE ──`, `# ── MAIN ──`) |
| **Reason** | 604-line file is difficult to navigate without section markers |
| **Risk** | None |

---

### PIPELINE — script 07 (largest file — 1526 lines)

#### Change 271
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Lines** | 22–25: Module-level docstring |
| **Before** | `"""...\n    python scripts/06_train_ratings.py --season 2025\n..."""` |
| **After** | `"""...\n    python scripts/07_compute_player_ratings.py --season 2025\n..."""` |
| **Reason** | Wrong filename in docstring — references old name `06_train_ratings.py` |
| **Risk** | None — documentation only |

#### Change 272
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Line** | 102: `POSITION_CEILING: dict[str, int] = {}` |
| **Before** | `POSITION_CEILING: dict[str, int] = {}`\n(and lines 1365–1366: `if POSITION_CEILING.get(pg):` guard) |
| **After** | Remove `POSITION_CEILING = {}` declaration and remove the dead ceiling guard at lines 1365–1366. |
| **Reason** | Defined but never populated — the guard is always falsy. EDGE_OVR_ANCHORS already enforces an implicit ceiling via piecewise mapping. Removing eliminates dead code confusion. |
| **Risk** | Low — removing dead code only. Confirm EDGE_OVR_ANCHORS max values serve as effective ceiling before removing. |

#### Change 273
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Line** | 1477: `return True  # always allow upsert` inside `validate_distribution()` |
| **Before** | *(no change to the return value itself)* |
| **After** | Add a comment block above the function explaining it is **intentionally informational-only**: `# validate_distribution prints per-position stats and warns on drift.\n# It never blocks the upsert — all decisions are informational.\n# See docs/AUDIT_FINDINGS.md §7 for the rationale.` |
| **Reason** | Future contributor might "fix" the always-true return thinking it's a bug. The comment prevents that. |
| **Risk** | None — comment only |

#### Change 274
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Action** | Add Google-style docstrings to all major functions. Priority order: `compute_player_rating()`, `get_playtime_tier()`, `edge_to_ovr()`, `get_era()`, `apply_conference_discount()` |
| **Reason** | Clean-arch standard |
| **Risk** | None |

#### Change 275
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Action** | Add section headers throughout (e.g., `# ── CONSTANTS ──`, `# ── PLAYTIME TIERS ──`, `# ── ANCHORS ──`, `# ── EDGE → OVR ──`, `# ── RATING ENGINE ──`, `# ── EXPORT ──`) |
| **Reason** | 1526-line file; section markers are essential for navigation |
| **Risk** | None |

---

### PIPELINE — script 08 (broken SQL + late import)

#### Change 281
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/08_harvest_ea_cfb25_ratings.py` |
| **Lines** | 265–278: `build_player_index()` function |
| **Before** | `cur.execute("SELECT p.id, p.name, t.school FROM players p LEFT JOIN teams t ON t.id = p.team_id")` |
| **After** | Same fix as Change 241: join through `player_seasons` with `DISTINCT ON (p.id)` |
| **Reason** | `players.team_id` does not exist in v2 schema — script will crash at player match step |
| **Risk** | Medium — same as Change 241 |

#### Change 282
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/08_harvest_ea_cfb25_ratings.py` |
| **Line** | 177: `import re` inside `_parse_page()` function body |
| **Before** | `def _parse_page(html):\n    import re` |
| **After** | `import re` moved to module-level imports at file top |
| **Reason** | PEP 8: imports at module top, not inside functions |
| **Risk** | None |

---

### PIPELINE — script 08b (broken SQL)

#### Change 291
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/08b_harvest_ea_cfb26_ratings.py` |
| **Lines** | `build_player_index()` function (same pattern as 08) |
| **Before** | `SELECT p.id, p.name, t.school FROM players p LEFT JOIN teams t ON t.id = p.team_id` |
| **After** | Same fix as Changes 241 and 281 |
| **Reason** | Same v2 schema issue |
| **Risk** | Medium |

---

### PIPELINE — script 10 (late imports)

#### Change 301
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/10_compute_team_ratings.py` |
| **Line** | 357: `import pandas as pd` inside `compute_roster_quality()` |
| **Before** | `def compute_roster_quality(...):\n    import pandas as pd` |
| **After** | `import pandas as pd` at module top (already imports elsewhere; de-duplicate) |
| **Reason** | PEP 8: imports at top of file |
| **Risk** | None |

#### Change 302
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/10_compute_team_ratings.py` |
| **Line** | 669: `import pandas as pd` inside `export_team_ratings()` |
| **Before** | `def export_team_ratings(...):\n    import pandas as pd` |
| **After** | Removed (uses module-level import from Change 301) |
| **Reason** | PEP 8: imports at top of file; redundant with 301 |
| **Risk** | None |

#### Change 303
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/10_compute_team_ratings.py` |
| **Line** | 657: `"avg_starter_rating": None,  # deprecated in v2; use sub_ratings` |
| **Before** | `"avg_starter_rating": None,  # deprecated in v2; use sub_ratings` |
| **After** | *(line removed from output dict)* |
| **Reason** | Dead field in exported JSON — consumers use `sub_ratings`. Removing prevents confusion. |
| **Risk** | Low — verify no frontend code reads `avg_starter_rating` before removing. (Grep confirms: not referenced in cfb-analytics-app.) |

#### Change 304
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/10_compute_team_ratings.py` |
| **Action** | Add Google-style docstrings + type hints to all major functions |
| **Reason** | PEP 8 / clean-arch standard |
| **Risk** | None |

---

### PIPELINE — script 12

#### Change 311
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/12_export_frontend_json.py` |
| **Action** | Add Google-style docstrings to all export functions: `export_players()`, `export_teams()`, `export_similar_players()`, `export_research()`, etc. |
| **Reason** | PEP 8 / clean-arch standard |
| **Risk** | None |

#### Change 312
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/12_export_frontend_json.py` |
| **Action** | Add section headers (`# ── PLAYERS ──`, `# ── TEAMS ──`, `# ── SIMILAR PLAYERS ──`, `# ── RESEARCH ──`, `# ── MAIN ──`) |
| **Reason** | 940-line file benefits from section navigation |
| **Risk** | None |

---

### PIPELINE — script 11 (second-pass findings)

#### Change 313
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/11_compute_engine_b_ratings.py` |
| **Line** | 17: module docstring run example |
| **Before** | `python scripts/11_compute_engine_b.py` |
| **After** | `python scripts/11_compute_engine_b_ratings.py` |
| **Reason** | Wrong filename in the script's own docstring — missing `_ratings` suffix. Self-referential error; will confuse contributors running the script for the first time. |
| **Risk** | None — documentation only |

#### Change 314
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/11_compute_engine_b_ratings.py` |
| **Line** | 124: `from collections import defaultdict` inside `compute_nil_position_medians()` function body |
| **Before** | `def compute_nil_position_medians(...):\n    from collections import defaultdict` |
| **After** | `from collections import defaultdict` moved to module-level imports at file top |
| **Reason** | PEP 8: imports must be at module top, not inside function bodies. Causes redundant re-import on every call to `compute_nil_position_medians()`. |
| **Risk** | None — `defaultdict` is stdlib; no side effects from moving the import |

---

### PIPELINE — Naming Clarity & Comments (third-pass findings)

Changes 320–349: variable/function renames, parameter name improvements, and inline comment additions across all Python files. All are pure rename or comment-only — no logic changes.

#### Change 320
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/utils/store.py` |
| **Line** | 25: `COMP_DIR = _ROOT / "data" / "computed"` |
| **Before** | `COMP_DIR` |
| **After** | `COMPUTED_DIR` (renamed; update all 3 usages in `read_computed` and `write_computed`) |
| **Reason** | `COMP_DIR` is ambiguous — "comp" can mean compiled, composite, or computed. `COMPUTED_DIR` is unambiguous. `RAW_DIR` already spells out the word; consistency requires `COMPUTED_DIR`. |
| **Risk** | Low — rename only; update all references in store.py |

#### Change 321
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/utils/api_client.py` |
| **Line** | 40: `ck = _cache_key(path, params)` |
| **Before** | `ck` |
| **After** | `cache_key` |
| **Reason** | Two-letter abbreviation with no obvious expansion. `cache_key` is self-documenting. |
| **Risk** | None |

#### Change 322
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/utils/api_client.py` |
| **Function** | `_safe(api_key, path, params)` |
| **Before** | `def _safe(api_key, path, params=None) -> list:` |
| **After** | `def _get_list(api_key, path, params=None) -> list:` |
| **Reason** | "safe" implies security hardening; the actual contract is "returns empty list on failure." `_get_list` describes both what it fetches (via GET) and what it guarantees to return (list). All callers in api_client.py use it for list endpoints only. |
| **Risk** | Low — rename in api_client.py internals only; no external callers |

#### Change 323
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/utils/api_client.py` |
| **Line** | 279: `deff = (r.get("defense") or {}).get("rating", 0)` in `fetch_sp_ratings_breakdown()` |
| **Before** | `deff` |
| **After** | `def_rating` |
| **Reason** | `deff` is a typo-style workaround to avoid the `def` keyword. `def_rating` is readable and clearly intentional. |
| **Risk** | None |

#### Change 324
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/utils/api_client.py` |
| **Lines** | 55–56: `verify=False` in `_get()` |
| **Before** | `verify=False,  # (no comment)` |
| **After** | `verify=False,  # SSL cert validation disabled — CFB Data API cert fails on some Windows venvs; safe for this read-only endpoint` |
| **Reason** | Extends Change 103 with a more specific note about why it's safe (read-only endpoint, known environment constraint). |
| **Risk** | None — comment only |

#### Change 325
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/01_harvest_games_players_stats.py` |
| **Lines** | 87–95: `find_player_stats()` fuzzy match block |
| **Before** | `rf = first_name.lower()` and `sf = parts[0]` |
| **After** | `query_first = first_name.lower()` and `stored_first = parts[0]` |
| **Reason** | `rf` = "real first"?, `sf` = "stored first"? — no way to know without context. `query_first` and `stored_first` are self-explanatory. Same fix in `find_player_ppa()` (same pattern at lines 118–125). |
| **Risk** | None |

#### Change 326
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/01_harvest_games_players_stats.py` |
| **Lines** | 154–164: `build_awards_lookup()` tier assignment |
| **Before** | `tier = 3`, `tier = 2`, `tier = 1` (bare integer literals) |
| **After** | Add named constants at module top: `AWARD_TIER_ALL_AMERICAN = 3`, `AWARD_TIER_FIRST_TEAM = 2`, `AWARD_TIER_HONORABLE = 1`; replace bare integers with these constants |
| **Reason** | `lookup[key] < tier` is opaque when `tier` is 3 or 2. Named constants document what each tier represents and prevent future numeric mix-ups. |
| **Risk** | None |

#### Change 327
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/01_harvest_games_players_stats.py` |
| **Lines** | 229–233: `save_teams()` local variable `combined` |
| **Before** | `combined = list(existing.values())` |
| **After** | `all_teams = list(existing.values())` |
| **Reason** | `combined` is a generic verb-past-participle that gives no information about the content. `all_teams` is explicit. |
| **Risk** | None |

#### Change 328
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/01_harvest_games_players_stats.py` |
| **Function names** | `save_teams()` and `save_players()` |
| **Before** | `def save_teams(teams_raw):` / `def save_players(rosters_by_team, team_id_map, season):` |
| **After** | `def upsert_teams(teams_raw):` / `def upsert_players_and_seasons(rosters_by_team, team_id_map, season):` |
| **Reason** | Both functions do read-merge-write upsert (not plain save/overwrite). `save_teams` implies it truncates and rewrites; it actually merges. `upsert_` matches the DB-oriented language used elsewhere (bulk_upsert). `save_players` is especially misleading because it also writes `player_seasons.json` — two tables in one call. |
| **Risk** | Low — rename with update of all callers (only called in `main()`) |

#### Change 329
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/06_compute_edge_scores.py` |
| **Lines** | 189–196: `_int_stat(d, key)` inner function inside `build_game_context_map()` |
| **Before** | `def _int_stat(d, key):` |
| **After** | `def _parse_stat_value(d, key):` (extracted to module level above `build_game_context_map`) |
| **Reason** | `_int_stat` is misleading — the function actually parses string-encoded numbers like "5-3" (taking the part before the dash) and returns a float, not an int. `_parse_stat_value` is accurate. Moving it to module level makes it testable and discoverable. |
| **Risk** | None |

#### Change 330
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/06_compute_edge_scores.py` |
| **Line** | 243: `DEF_CONTEXT_BLEND = 0.35` |
| **Before** | `DEF_CONTEXT_BLEND = 0.35  # (no comment)` |
| **After** | `DEF_CONTEXT_BLEND = 0.35  # weight applied to team defensive context modifier (vs raw EDGE). See docs/AUDIT_FINDINGS.md §5.` |
| **Reason** | The constant name doesn't explain the formula role. The 0.35 is a deliberate calibration value from AUDIT_FINDINGS; the comment creates a durable link to that rationale. |
| **Risk** | None — comment only |

#### Change 331
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/06_compute_edge_scores.py` |
| **Function** | `_season_means()` — return type |
| **Before** | `def _season_means(sp_map: dict) -> tuple:` (unnamed tuple) |
| **After** | `def _season_means(sp_map: dict) -> tuple[float, float, float]:  # (mean_off_sp, mean_def_sp, mean_overall_sp)` |
| **Reason** | Callers must remember tuple index order: `mean_off, mean_def, mean_ovr = season_means`. The annotation documents this contract. |
| **Risk** | None — annotation only |

#### Change 332
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Function** | `_f(stats, key)` (line 246) |
| **Before** | `def _f(stats, key):` |
| **After** | `def _stat_float(stats: dict, key: str) -> float:` |
| **Reason** | Script 07 defines `_f(stats, key)` (dict lookup + float coercion). Script 12 defines a different `_f(val, default=None)` (bare float conversion). Same name, different signatures, different semantics. `_stat_float` is unambiguous and self-documenting. |
| **Risk** | Low — rename with update of all callers within script 07 only |

#### Change 333
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Function** | `classify_tier()` |
| **Before** | `def classify_tier(pg, stats, games_played=1):` |
| **After** | `def classify_playtime_tier(pg, stats, games_played=1):` |
| **Reason** | "Tier" is overloaded in this codebase — rating tiers (ELITE/GOLD/SILVER) and playtime tiers (starter/role/reserve/bench) are distinct concepts. `classify_playtime_tier` is unambiguous. |
| **Risk** | Low — rename with update of all callers within script 07 |

#### Change 334
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Function** | `get_era()` |
| **Before** | `def get_era(season: int) -> str:` |
| **After** | `def get_rating_era(season: int) -> str:` |
| **Reason** | "Era" alone could mean a historical era of college football. `get_rating_era` makes clear this returns the era label for the *rating system* (which controls whether pre-2016 classic scoring or modern EDGE scoring applies). |
| **Risk** | Low — rename with callers |

#### Change 335
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Constant** | `WEIGHTS_NO_EDGE` |
| **Before** | `WEIGHTS_NO_EDGE = { ... }` |
| **After** | `STAT_ONLY_WEIGHTS = { ... }` |
| **Reason** | "NO_EDGE" is a negation that doesn't convey what the weights actually do. `STAT_ONLY_WEIGHTS` communicates clearly: use these when EDGE score is absent and we fall back to raw stat composites only. |
| **Risk** | Low — rename with update of all callers |

#### Change 336
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Constant** | `STARS_FALLBACK` |
| **Before** | `STARS_FALLBACK = {5: -3, 4: -8, 3: -15, 2: -22, 1: -28, 0: -33}` |
| **After** | `STARS_OVR_DELTA = {5: -3, 4: -8, 3: -15, 2: -22, 1: -28, 0: -33}` |
| **Reason** | `STARS_FALLBACK` sounds like a fallback mechanism; it's actually a lookup that maps recruiting stars to an OVR delta below the position average. `STARS_OVR_DELTA` states exactly what each value represents. |
| **Risk** | None — rename with callers |

#### Change 337
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Function** | `extract_features()` |
| **Before** | `def extract_features(stats: dict, pg: str) -> dict:` |
| **After** | `def compute_stat_features(stats: dict, pg: str) -> dict:` |
| **Reason** | "Extract" implies the features already exist in the input. This function computes derived metrics (comp_pct, yards_per_att, etc.) from raw stats — `compute` is accurate. `features` is ML-jargon; the simpler `stat_features` is clear to a non-ML contributor. |
| **Risk** | Low — rename with callers |

#### Change 338
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Line** | 254: `_composite_to_100(score)` |
| **Before** | `def _composite_to_100(score) -> float:` with formula `(float(score) - 0.7) / 0.3 * 100` |
| **After** | Add inline comment: `# 247Sports composite scale: 0.7 = floor (average walk-on), 1.0 = theoretical ceiling. Maps to 0–100.` |
| **Reason** | The magic numbers 0.7 and 0.3 are specific to 247Sports' composite scoring methodology. Without this comment, a future contributor might think these are calibration values to tune. |
| **Risk** | None — comment only |

#### Change 339
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/12_export_frontend_json.py` |
| **Line** | 77: `def _default(o):` |
| **Before** | `def _default(o):` |
| **After** | `def _json_default(o):` |
| **Reason** | `_default` is easily confused with a Python built-in or a sentinel value. `_json_default` is unambiguous: it is the `default=` parameter handler for `json.dump`. |
| **Risk** | None — rename with the one caller (line 71: `json.dump(..., default=_json_default, ...)`) |

#### Change 340
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/12_export_frontend_json.py` |
| **Lines** | 143–148: short alias variables inside `export_players()` |
| **Before** | `rat = T["ratings"]; ps = T["player_seasons"]; pl = T["players"]; tm = T["teams"]; rec = T["recruiting"]; edge = T["player_edge"]` |
| **After** | Same aliases (keep for readability in join chain) but add comment: `# Short local aliases — rat=ratings, ps=player_seasons, pl=players, tm=teams, rec=recruiting` |
| **Reason** | The one-letter abbreviations are established in pandas-heavy code. Rather than rename every occurrence (adding length to already-wide join code), a single comment block documents the convention. |
| **Risk** | None — comment only |

#### Change 341
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/12_export_frontend_json.py` |
| **Lines** | 155: `rat_s = rat[rat["season"] == season].copy()` |
| **Before** | `rat_s` |
| **After** | `ratings_season` (and update all 20+ occurrences in `export_players()`) |
| **Reason** | `rat_s` is doubly terse — the `_s` suffix reads as "string" to most readers. `ratings_season` is explicit and consistent with the naming pattern already used in `load_position_data()` in script 07. |
| **Risk** | Low — internal to `export_players()` only |

---

### PIPELINE — Fourth-Pass Naming & Comment Fixes (Final Review)

Changes 342–365 from complete line-by-line audit of scripts 02, 03, 05, 07, 08, 08b, 10, 11 and frontend files.

#### Change 342

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/02_harvest_recruiting.py` |
| **Function** | `save_recruiting()` |
| **Before** | `def save_recruiting(rows):` |
| **After** | `def upsert_recruiting(rows):` |
| **Reason** | Parallel to Change 328 (`save_teams` → `upsert_teams`). The function reads existing data, merges, and calls `bulk_upsert`. "save" implies overwrite; "upsert" is accurate. |
| **Risk** | Low — rename + update one caller in `main()` |

#### Change 343

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/02_harvest_recruiting.py` |
| **Line** | ~264: `combined = list(existing_map.values())` |
| **Before** | `combined` |
| **After** | `all_recruits` |
| **Reason** | Parallel to Change 327 (`combined` → `all_teams` in script 01). "combined" says nothing about what was combined. `all_recruits` is self-documenting. |
| **Risk** | None — local rename within `save_recruiting()` |

#### Change 344

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/02_harvest_recruiting.py` |
| **Line** | ~252: `new_rows = {k: v for k, v in seen.items()}` |
| **Before** | `new_rows = {k: v for k, v in seen.items()}` |
| **After** | `new_rows = dict(seen)` |
| **Reason** | The dict comprehension copies `seen` verbatim with no transformation — identical to `dict(seen)`. The comprehension form implies filtering or mapping that isn't happening. |
| **Risk** | None — identical behavior, simpler expression |

#### Change 345

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/03_harvest_transfers.py` |
| **Function** | `save_transfers()` |
| **Before** | `def save_transfers(rows):` |
| **After** | `def upsert_transfers(rows):` |
| **Reason** | Parallel to Change 342 / Change 328 — same pattern in every harvest script. |
| **Risk** | Low — rename + update one caller in `main()` |

#### Change 346

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/03_harvest_transfers.py` |
| **Line** | ~254: `combined = list(existing_map.values())` |
| **Before** | `combined` |
| **After** | `all_transfers` |
| **Reason** | Parallel to Change 343. |
| **Risk** | None |

#### Change 347

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/05_load_coaching_changes.py` |
| **Line** | ~8: module docstring usage line |
| **Before** | `python scripts/05_coaching_changes.py` |
| **After** | `python scripts/05_load_coaching_changes.py` |
| **Reason** | Docstring cites the wrong filename. Parallel to Change 313 (script 11 wrong filename). |
| **Risk** | None — docstring only |

#### Change 348

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/05_load_coaching_changes.py` |
| **Line** | ~27: `PAGE_LOAD_WAIT = 12` |
| **Before** | `PAGE_LOAD_WAIT = 12` |
| **After** | `PAGE_LOAD_WAIT = 12  # seconds; ESPN article requires JS rendering before content appears` |
| **Reason** | Magic number with no context. The comment explains why 12s (not 5s) is needed. |
| **Risk** | None — comment only |

#### Change 349

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Lines** | ~689–699: `_si` and `_sf` helper functions inside the `for` loop body of `_load_seasons()` |
| **Before** | Both helpers defined inside the loop — redefined on every iteration |
| **After** | Extract both to module level above `_load_seasons()` |
| **Reason** | Defining functions inside a loop is valid Python but implies the closure captures a loop variable; here `_si` and `_sf` capture nothing from the loop. Module-level placement makes them testable and avoids repeated re-creation. |
| **Risk** | Low — pure refactor; behavior identical |

#### Change 350

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Function** | `scale_to_range(scores, pg: str = "")` |
| **Before** | Has `pg: str = ""` parameter that is never used inside the function body |
| **After** | Remove `pg` parameter entirely; update all callers to drop the argument |
| **Reason** | Dead parameter. A future reader may assume `pg` controls branching inside the function and waste time finding where. Removing it eliminates the ambiguity. |
| **Risk** | Low — confirm all callers pass pg as positional (check they won't break) |

#### Change 351

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Lines** | Inside `compute_edge_ratings()`: `stat_targets = [30.0, 38.0, 55.0, 64.0, 70.0, 76.0, 78.0]` defined inline |
| **Before** | Inline constant buried inside function body |
| **After** | `STAT_FALLBACK_TARGETS = [30.0, 38.0, 55.0, 64.0, 70.0, 76.0, 78.0]` at module level |
| **Reason** | Inline constants in long functions are invisible to other readers and untestable. Module-level placement matches the pattern of all other anchor constants in this file. |
| **Risk** | None — rename + update one reference |

#### Change 352

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/07_compute_player_ratings.py` |
| **Functions** | `compute_edge_ratings()` and `compute_ratings()` |
| **Before** | Both lack return type annotations |
| **After** | `def compute_edge_ratings(...) -> tuple[np.ndarray, list[dict]]:` and `def compute_ratings(...) -> tuple[np.ndarray, list[dict]]:` |
| **Reason** | These are the two most complex functions in the file. The return type documents that both emit (scores_array, contribs_list) — a contract that callers depend on. |
| **Risk** | None — annotation only |

#### Change 353

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/08_harvest_ea_cfb25_ratings.py` |
| **Lines** | ~177: `import re` inside `_parse_page()` function |
| **Before** | `import re` inside the function body |
| **After** | Move to module-level imports at top of file |
| **Reason** | PEP 8: all imports at top of file unless unavoidable (e.g. circular). `re` is stdlib — no reason to defer it. The deferred import runs on every call to `_parse_page()`. |
| **Risk** | None |

#### Change 354

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/08_harvest_ea_cfb25_ratings.py` |
| **Lines** | Module docstring usage examples |
| **Before** | `python scripts/09_scrape_ea_cfb25.py` |
| **After** | `python scripts/08_harvest_ea_cfb25_ratings.py` |
| **Reason** | Docstring cites the wrong filename. Parallel to Changes 313 and 347. |
| **Risk** | None — docstring only |

#### Change 355

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/08b_harvest_ea_cfb26_ratings.py` |
| **Lines** | Module docstring usage examples |
| **Before** | `python scripts/09b_scrape_ea_cfb26.py` |
| **After** | `python scripts/08b_harvest_ea_cfb26_ratings.py` |
| **Reason** | Same as Change 354. |
| **Risk** | None |

#### Change 356

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/10_compute_team_ratings.py` |
| **Lines** | ~550–551 inside `compute_team_splits()`: `deff = sp_defense_to_ovr(sp["defense"])` |
| **Before** | `deff` |
| **After** | `def_rating` |
| **Reason** | `deff` is a workaround for `def` being a Python keyword. Parallel to Change 221 in `api_client.py`. `def_rating` is unambiguous. |
| **Risk** | Low — rename all uses within `compute_team_splits()` (4 references) |

#### Change 357

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/10_compute_team_ratings.py` |
| **Lines** | ~43–60: constant block |
| **Before** | `W_SP = 0.45` and `W_SP_BLEND = 0.75` defined 90+ lines apart with no explanation of the distinction |
| **After** | Add comment above `W_SP_BLEND`: `# W_SP_BLEND: SP+ fraction of the *headline OVR* blend (SP+ vs. roster mean). Distinct from W_SP (which is SP+'s share of the three-signal composite below).` |
| **Reason** | Two constants both start with `W_SP` but serve different blending formulas. A reader seeing `W_SP_BLEND = 0.75` next to `W_SP = 0.45` will assume they're related sub-weights of the same thing. They're not. |
| **Risk** | None — comment only |

#### Change 358

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/10_compute_team_ratings.py` |
| **Lines** | ~631 inside `run_season()`: `rec_s = recruiting_map.get(team_id)` |
| **Before** | `rec_s` |
| **After** | `recruiting_scaled` |
| **Reason** | Parallel to Change 341 (`rat_s` → `ratings_season` in script 12). `_s` suffix is ambiguous (string? season-filtered? scaled?). `recruiting_scaled` is explicit. |
| **Risk** | None — local variable rename within `run_season()` loop |

#### Change 359

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/scripts/11_compute_engine_b_ratings.py` |
| **Lines** | ~123: `from collections import defaultdict` inside `compute_nil_position_medians()` |
| **Before** | Late import inside function body |
| **After** | Move to module-level imports at top of file (alongside existing `import math`, `import json`, etc.) |
| **Reason** | Parallel to Change 314 (same issue in same file with a different import — both should be module-level). PEP 8. |
| **Risk** | None |

---

### FRONTEND — Additional Naming, Comments & Test Infrastructure

#### Change 407
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/supabaseClient.js` → `dataLoader.js` (post-rename) |
| **Lines** | 61: `const ratMap = {};` / 69: `const sub = ...` / 75: `const tr = ratMap[t.id] \|\| {};` inside `fetchTeams()` |
| **Before** | `ratMap` (ratings map), `tr` (team rating row), `sub` (sub_ratings) |
| **After** | `teamRatingsMap`, `teamRating`, `subRatings` |
| **Reason** | `ratMap` looks like "rat" map. `tr` is indistinguishable from a `<tr>` HTML table row. `sub` gives no indication it holds team sub-score breakdown. All three are resolved within 10 lines of each other, making confusion easy. |
| **Risk** | None — local variable renames within `fetchTeams()` |

#### Change 408
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/config.js` |
| **Function** | `ratingColor(v)` — parameter name |
| **Before** | `function ratingColor(v) {` |
| **After** | `function ratingColor(rating) {` (update all internal uses of `v`) |
| **Reason** | `v` is a common generic placeholder. At a call site like `ratingColor(p.edge_score)`, `v` could be anything. `rating` makes the expected input type and range (0–100) obvious. |
| **Risk** | None — internal rename only |

#### Change 409
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/playerSearch.js` |
| **Function** | `posGroupColor(g)` — parameter name |
| **Before** | `function posGroupColor(g) {` |
| **After** | `function posGroupColor(posGroup) {` (update internal use) |
| **Reason** | `g` is ambiguous. `posGroup` matches the established `pg` convention used everywhere else for position group (the full parameter name avoids the abbreviation at declaration point). |
| **Risk** | None |

#### Change 410
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/playerSearch.js` |
| **Line** | 393: `const ratings_labels = vals.map(...)` |
| **Before** | `ratings_labels` (snake_case) |
| **After** | `ratingsLabels` (camelCase) |
| **Reason** | All other JS local variables in this file use camelCase. `ratings_labels` is a Python-style naming inconsistency that slipped through. |
| **Risk** | None — local variable |

#### Change 411
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/playerSearch.js` |
| **Line** | 336: `const psData = postseasonData \|\| null;` inside `modalContentHtml()` |
| **Before** | `psData` |
| **After** | `postData` |
| **Reason** | `ps` in the pipeline codebase means `player_seasons`. In this file `ps` is used for `postseason`. The collision is confusing when reading across the codebase. `postData` is unambiguous. |
| **Risk** | None — local variable within `modalContentHtml()` |

#### Change 412
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/playerSearch.js` |
| **Lines** | 353: `const EDGE_POS = ["QB","RB","WR","TE","EDGE","DL","LB","CB","S","DB"];` inside `modalContentHtml()` |
| **Before** | Inline constant inside function body |
| **After** | Move to module top as `const EDGE_RATED_POSITIONS = ["QB","RB","WR","TE","EDGE","DL","LB","CB","S","DB"];` |
| **Reason** | Inline constants in long function bodies are invisible to other functions that might need the same list. Module-level placement makes it reusable and consistent with config.js's `CONFIG.POSITIONS`. "EDGE_RATED" disambiguates from the EDGE position group. |
| **Risk** | None |

#### Change 413
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/playerSearch.js` |
| **Lines** | 183–188: inline column label "OAP" in `renderGrid()` draft-board header |
| **Before** | `<span style="text-align:center">OAP</span>` (header column) and `<div class="draft-edge">${oap}</div>` (cell class) |
| **After** | Rename column header to `EDGE` and rename CSS class `draft-edge` in styles.css to `draft-oap` — OR keep `draft-edge` and rename the header `EDGE`. Chosen direction: **rename header to `EDGE`** (consistent with the data field name `edge_score` and the platform terminology) |
| **Reason** | The column is labeled "OAP" (Opponent-Adjusted Production) in the header but uses the CSS class `draft-edge` and the variable `oap`. Three different names for the same concept creates confusion. Pick one and stick with it. "EDGE" is the canonical term throughout the platform. |
| **Risk** | Low — header text change only; CSS class stays `draft-edge` |

#### Change 414
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/ratingsDisplay.js` |
| **Lines** | 143–144: regression line endpoint variables |
| **Before** | `const rx1 = ...; const ry1 = ...; const rx2 = ...; const ry2 = ...;` |
| **After** | `const regX1 = ...; const regY1 = ...; const regX2 = ...; const regY2 = ...;` |
| **Reason** | `rx1`/`ry1` look like "right x" or could be confused with SVG rect coordinates. `regX1` explicitly marks these as regression line endpoints. |
| **Risk** | None |

#### Change 415
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/ratingsDisplay.js` |
| **Lines** | 113–119: linear regression calculation |
| **Before** | `const ssXY = ...; const ssXX = ...;` with no explanation |
| **After** | Add comment above block: `// Linear regression: slope = Σ(xi - x̄)(yi - ȳ) / Σ(xi - x̄)² ; intercept = ȳ - slope·x̄` |
| **Reason** | `ssXY` and `ssXX` are statistics shorthand (sum of squares). Without a comment, a reader unfamiliar with regression notation cannot verify the formula is correct. |
| **Risk** | None — comment only |

#### Change 416

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/playerSearch.js` |
| **Lines** | ~410–422: `CAREER_FIELDS` constant defined inline inside `modalContentHtml()` |
| **Before** | Inline constant inside long function body |
| **After** | `const CAREER_STAT_FIELDS = { QB: [...], RB: [...], ... };` at module top (after `EDGE_RATED_POSITIONS`) |
| **Reason** | Parallel to Change 412 (`EDGE_POS` moved to module level). An 80-line constant buried inside a 400-line function is invisible to anyone searching for stat field definitions. Module-level placement matches the pattern in `config.js`. "CAREER_" prefix disambiguates from `STAT_BLOCK_FIELDS` (Change 417). |
| **Risk** | None — same data, new location |

#### Change 417

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/playerSearch.js` |
| **Lines** | ~611–626: `fields` constant defined inline inside `renderStatBlocks()` |
| **Before** | `const fields = { QB: [...], RB: [...], ... };` inside function |
| **After** | `const STAT_BLOCK_FIELDS = { ... };` at module top (after `CAREER_STAT_FIELDS`) |
| **Reason** | Same rationale as Change 416. `renderStatBlocks` and `modalContentHtml` both define a position→stat-columns map with slightly different content. Naming them distinctly at module level (`CAREER_STAT_FIELDS` vs `STAT_BLOCK_FIELDS`) makes the distinction explicit and both searchable. |
| **Risk** | None |

#### Change 418

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/playerSearch.js` |
| **Lines** | ~434 inside `modalContentHtml()`: `const def = (d, k) => { ... }` |
| **Before** | `const def = (d, k) => { const v = d?.[k]; return v !== null && v !== undefined ? v : null; };` |
| **After** | `const getStatVal = (d, k) => { const v = d?.[k]; return v !== null && v !== undefined ? v : null; };` |
| **Reason** | `def` shadows the `def` keyword in Python (the most common cross-reference language for this codebase) and can confuse readers switching between the two languages. `getStatVal` describes the function's purpose. |
| **Risk** | None — rename all uses within `modalContentHtml()` (4–5 references) |

---

### FRONTEND — CSS Naming

#### Change 605
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/styles.css` |
| **Selector** | `.pos-badge-color` |
| **Before** | `.pos-badge-color { ... }` (used in `playerRowHtml()` to color the position badge by group) |
| **After** | `.pos-group-badge { ... }` (rename selector; update one occurrence in `playerSearch.js`) |
| **Reason** | "color" in the class name is redundant — all badges have color. The word "group" specifies this is a position-group badge (not a tier badge or school badge). |
| **Risk** | Low — one CSS selector + one usage in playerSearch.js |

#### Change 606
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/styles.css` (and usage in `playerSearch.js`) |
| **Selector** | `.d-depth-avatar` |
| **Before** | `.d-depth-avatar` — used in `modalContentHtml()` for the player initials circle in modal header |
| **After** | `.player-initials-avatar` |
| **Reason** | `.d-depth-avatar` is opaque — "d-depth" appears to be a legacy design-system prefix with no meaning in this project. `.player-initials-avatar` describes exactly what the element is. |
| **Risk** | Low — update CSS selector + one occurrence in playerSearch.js modal HTML |

#### Change 607
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/styles.css` and `ratingsDisplay.js` |
| **Selectors** | `.tt-name`, `.tt-meta`, `.tt-ovr` (scatter tooltip sub-elements) |
| **Before** | `tt-name`, `tt-meta`, `tt-ovr` |
| **After** | `tooltip-name`, `tooltip-meta`, `tooltip-rating` |
| **Reason** | `tt` is a non-standard abbreviation (could mean tooltip, but also `<tt>` legacy HTML element, or test template). Spelling out `tooltip-` is clearer. `tt-ovr` → `tooltip-rating` since "ovr" is not a universal abbreviation outside this platform's context. |
| **Risk** | Low — update CSS + querySelector calls in ratingsDisplay.js (3 `.querySelector(".tt-*")` calls) |

#### Change 608

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/styles.css` |
| **Line** | ~544: `.modal-section { }` |
| **Before** | `.modal-section { }` (empty rule) |
| **After** | Remove the rule entirely |
| **Reason** | Empty rules generate no output and serve no purpose. The section IS used for layout (flex gap in `.modal-body`) but that comes from the parent, not `.modal-section`. |
| **Risk** | None |

#### Change 609

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/styles.css` |
| **Line** | ~845: `.tr-sub-section {}` |
| **Before** | `.tr-sub-section {}` (empty rule) |
| **After** | Remove the rule entirely |
| **Reason** | Same as Change 608 — empty CSS rule, no properties, no output. |
| **Risk** | None |

#### Change 610

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/styles.css` |
| **Lines** | ~368–370: `.traj-up { ... }` and `.traj-down { ... }` |
| **Before** | Two rules defining `.traj-up` and `.traj-down` in the player card section |
| **After** | Remove both rules |
| **Reason** | `config.js`'s `trajHtml()` emits `.traj-up1`, `.traj-up2`, `.traj-flat`, `.traj-down1`, `.traj-down2` — not `.traj-up` or `.traj-down`. The two-level system replaced the single-level system at some point and these are orphaned. Grep confirms zero uses in current HTML/JS. |
| **Risk** | Low — grep `class="traj-up"` and `class="traj-down"` across all files first to confirm zero uses |

---

### PHASE 2 — Test Infrastructure (new files)

The project currently has no automated tests. Clean-arch introduces a `tests/` directory in `cfb-analytics-pipeline/` with pure unit tests (no DB, no API, no file I/O) covering the most critical computation functions.

#### Change 801
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/tests/test_edge_anchors.py` (new file) |
| **Action** | Create test file |
| **What it tests** | `edge_to_ovr()` from script 07. Verifies known anchor points return expected OVR values within ±0.5, and that below-minimum edge_score returns 30.0 and above-maximum returns 99.0. One test per position group (12 groups × 3 anchor checks = 36 assertions). |
| **Why** | `EDGE_OVR_ANCHORS` is a permanent calibration table. Any future edit that accidentally shifts an anchor will immediately break these tests, flagging the change before it affects output data. |
| **Risk** | None — new file, no side effects |

#### Change 802
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/tests/test_playtime_tier.py` (new file) |
| **Action** | Create test file |
| **What it tests** | `classify_playtime_tier()` from script 07 (post-rename from Change 333). Tests that at `starter` threshold value → "starter", at `role` threshold → "role", at `reserve` threshold → "reserve", below `reserve` → "bench". Tests all 12 position groups (OL always returns "starter"). |
| **Why** | The four-tier system is a core architectural decision (AUDIT_FINDINGS.md §2). Tier boundary bugs silently change hundreds of players' ratings without any visible error. |
| **Risk** | None |

#### Change 803
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/tests/test_composite_to_100.py` (new file) |
| **Action** | Create test file |
| **What it tests** | `_composite_to_100()` from script 07. Tests: score=1.0 → 100.0, score=0.7 → 0.0, score=0.85 → 50.0, score=None → 40.0 (fallback), score=0.0 → 0.0 (clamped). |
| **Why** | The 0.7/0.3 scale factors are specific to 247Sports data. If the recruiting source ever changes scale, the formula must be updated. Tests document the expected input range and output range permanently. |
| **Risk** | None |

#### Change 804
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/tests/test_clean_nan.py` (new file) |
| **Action** | Create test file |
| **What it tests** | `_clean_nan()` from script 12. Tests: plain float NaN → None, float inf → None, nested dict with NaN → None at that key, nested list with NaN → None at that index, numpy float64 NaN → None, valid float → unchanged, valid int → unchanged. |
| **Why** | `_clean_nan()` is the one function standing between invalid JSON being served to the browser and a broken fetch(). If it fails to handle any edge case, the frontend silently breaks for that player. |
| **Risk** | None |

---

### FRONTEND — HTML: Additional Semantic & Consistency

#### Change 507 ~~(REMOVED — ALREADY DONE)~~

> **Final-pass finding:** All 6 HTML files already have `lang="en"` in the `<html>` tag (`<html lang="en" data-theme="dynasty-dark">`). This change is a no-op — no action needed.

#### Change 508 ~~(MERGED INTO CHANGE 503)~~

> **Final-pass finding:** `info.html` already loads `wght@400;600;700;900` (confirmed on read). Change 508 duplicates Change 503. Font weight fix applies to exactly 4 pages — see Change 503.

---

| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/supabaseClient.js` → renamed to `cfb-analytics-app/js/dataLoader.js` |
| **Action** | Rename file. Update `<script src="js/supabaseClient.js">` in all 4 HTML pages that load it: `index.html` (line 237), `players.html` (line 98), `teams.html` (line 72), `ratings.html` (line 98) |
| **Before** | `<script src="js/supabaseClient.js"></script>` |
| **After** | `<script src="js/dataLoader.js"></script>` |
| **Reason** | File no longer talks to Supabase. Name is misleading. Rename per plan. |
| **Risk** | Low — search-and-replace in 4 files. Verify all 4 pages work after rename. |

#### Change 402
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/dataLoader.js` (formerly supabaseClient.js) |
| **Line** | 1: `// Static JSON data loader — replaces Supabase REST client.` |
| **Before** | `// Static JSON data loader — replaces Supabase REST client.\n// All data comes from pre-built JSON files in data/ served by GitHub Pages.\n// Function signatures are identical to the old Supabase version so callers need no changes.` |
| **After** | `// dataLoader.js — static JSON data loader for cfb-analytics-app.\n// All data comes from pre-built JSON files in data/ (populated by pipeline script 12).\n// Loaded as a global before page-specific scripts.` |
| **Reason** | Update file comment to reflect actual purpose and new filename |
| **Risk** | None |

#### Change 403
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/config.js` |
| **Line** | 1: `// Central configuration — imported by all other JS files.` |
| **After** | Add JSDoc block comments to all 5 exported functions (getRatingTier, ratingColor, ratingTextColor, posColor, trajHtml, starsHtml) using `/** ... */` format per W3Schools JS best practices |
| **Reason** | Clean-arch standard: document public API |
| **Risk** | None |

#### Change 404
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/playerSearch.js` |
| **Line** | 1–2: file header comment |
| **Before** | `// Player search and card rendering for players.html\n// Data source: Supabase for both grid and detail modal.` |
| **After** | `// playerSearch.js — player search grid, modal, and similar-player UI.\n// Data source: dataLoader.js (static JSON from data/ directory).` |
| **Reason** | Comment references "Supabase" — outdated post-local-arch |
| **Risk** | None |

#### Change 405
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/playerSearch.js` |
| **Action** | Add JSDoc block comments to all major functions: `initPlayerSearch()`, `fetchAndRender()`, `openPlayerModal()`, `modalContentHtml()`, `renderRatingBreakdown()`, `renderStatBlocks()`, `mergeStatTotals()` |
| **Reason** | Clean-arch standard |
| **Risk** | None |

#### Change 406
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/js/ratingsDisplay.js` |
| **Action** | Add JSDoc block comments to `initRatings()`, `buildFilters()`, `applyFilters()`, `renderScatterPlot()` |
| **Reason** | Clean-arch standard |
| **Risk** | None |

---

### FRONTEND — HTML/

#### Change 501
| Field | Value |
|-------|-------|
| **Files** | All 6 HTML files |
| **Action** | Add section comments throughout: `<!-- ==================== SIDEBAR NAV ==================== -->`, `<!-- ==================== MAIN CONTENT ==================== -->`, `<!-- ==================== MODAL ==================== -->`, etc. |
| **Reason** | Clean-arch HTML5 standard: section comments for navigation |
| **Risk** | None |

#### Change 502
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/index.html` |
| **Lines** | 268–284: Duplicate `posColor_fn()` and `_ratingColor()` defined inline |
| **Before** | Both functions defined inline in the page's `<script>` block |
| **After** | Add a comment above each: `// Standalone variant — deliberately not using config.js posColor() here\n// to avoid theme-dependency in the home page summary display.` |
| **Reason** | Clarify that these are intentional variants, not accidental duplicates |
| **Risk** | None — comment only |

#### Change 503
| Field | Value |
|-------|-------|
| **Files** | `cfb-analytics-app/players.html`, `teams.html`, `ratings.html`, `research.html` |
| **Before** | `Barlow+Condensed:wght@400;600;700` (missing 900) on 4 of 6 pages |
| **After** | `Barlow+Condensed:wght@400;600;700;900` on all 4 pages |
| **Note** | `index.html` and `info.html` already load 900 — no change needed there. |
| **Reason** | Font-weight 900 is used for `.page-hero h1`, `.draft-rank.top3`, `.home-hero h1`. When the browser can't find 900 it falls back to 700, visually lightening these elements vs. pages that already load 900. |
| **Risk** | Low — additive only; no visual regression |

#### Change 504
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/research.html` |
| **Lines** | 11–29: Inline `<style>` block |
| **After** | Move inline styles to `components.css` under a `/* ── Research Page ── */` section |
| **Reason** | W3Schools HTML5 best practice: no inline `<style>` blocks; all CSS in external files |
| **Risk** | Low — requires verifying research page renders identically after move |

#### Change 505
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/info.html` |
| **Lines** | 11–93: Large inline `<style>` block (82 lines) |
| **After** | Move to `components.css` under `/* ── Info Page ── */` section |
| **Reason** | Same as Change 504 — no inline style blocks per HTML5 standard |
| **Risk** | Low — same verification required |

#### Change 506
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/index.html` |
| **Lines** | 11–130: Large inline `<style>` block (119 lines) |
| **After** | Move to `components.css` under `/* ── Home Page ── */` section |
| **Reason** | Same as Changes 504/505 |
| **Risk** | Low |

---

### FRONTEND — CSS/

#### Change 601
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/styles.css` |
| **Action** | Add section header comments throughout at major boundaries |
| **Before** | Some sections have `/* ── Section Name ── */` comments; many don't |
| **After** | Standardize ALL sections to: `/* ========================================\n   SECTION NAME\n   ======================================== */` |
| **Reason** | Clean-arch standard: consistent section headers for navigation in 1232-line file |
| **Risk** | None — comment only |

#### Change 602
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/components.css` |
| **Action** | Add section header comments in same format as Change 601 |
| **Reason** | Same standard applied to components.css |
| **Risk** | None |

#### Change 603
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/styles.css` |
| **Lines** | 207–223: `.nav-brand`, `.nav-links`, `.nav-links a`, `.nav-links a:hover`, `.nav-links a.active`, `.nav-spacer` |
| **Before** | Rules kept per inline comment: "keep nav-brand/nav-links/nav-spacer as fallback for any remnant uses" |
| **After** | Remove rules entirely. No current HTML uses these classes. The comment itself is the only reference. |
| **Reason** | Dead CSS. The old top-nav was replaced by the sidebar. Removing 17 lines reduces file size and confusion. |
| **Risk** | Low — grep `class="nav-brand"`, `class="nav-links"`, `class="nav-spacer"` across all HTML files first to confirm zero uses. |

#### Change 604
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-app/css/styles.css` |
| **Lines** | 1099–1101: `.draft-info`, `.draft-meta` |
| **Before** | `/* Legacy — no longer used but keep so old references don't break */\n.draft-info { min-width: 0; }\n.draft-meta { font-size: var(--fs-xs); color: var(--text-muted); }` |
| **After** | Remove these rules |
| **Reason** | The comment itself says "no longer used." No HTML references these classes. |
| **Risk** | Low — grep to confirm zero uses before removing. |

---

### PIPELINE — README

#### Change 701
| Field | Value |
|-------|-------|
| **File** | `cfb-analytics-pipeline/README.md` |
| **Before** | 3 lines of whitespace (empty) |
| **After** | Minimal README: what this repo does, setup (venv + .env), run order (01 → 02 → 03 → 05 → 06 → 07 → 10 → 12), data outputs |
| **Reason** | Empty README gives zero onboarding signal |
| **Risk** | None |

---

## PHASE 3 — v4: analytics-implementation Branch

Branch: `git checkout -b analytics-implementation` from main after v3.0.0 tag.
Changes in this phase are planned but not detailed here — Phase 3 gets its own changelog once clean-arch is complete.

Key Phase 3 items (high-level):
- New `scripts/13_predict_trajectories.py` — XGBoost regressor (Engine D)
- Updates to `scripts/12_export_frontend_json.py` to export `data/trajectories.json`
- New UI panels in `players.html` (trajectory tab in modal)
- New `data/trajectories.json` format definition

---

## Files NOT Changing (and why)

| File | Why Not Changing |
|------|----------------|
| `utils/db.py` lines 1–50 (core logic) | Connection management and bulk_upsert logic is correct and well-tested. Only the print at line 28 and docstrings change. |
| `utils/store.py` core functions | read_raw/read_computed/write_computed logic is correct. Only docstrings and type hints change. |
| `scripts/01` POSITION_GROUP_MAP dict | The 12-group system was deliberate per AUDIT_FINDINGS.md §1. Do not re-litigate. |
| `scripts/06` DEF_CONTEXT_BLEND = 0.35 | Per docs/AUDIT_FINDINGS.md §5 — no change to defensive composite formulas. |
| `scripts/07` EDGE_OVR_ANCHORS | Fixed calibration values — never change without a full distribution audit. Per AUDIT_FINDINGS.md §7. |
| `scripts/07` PLAYTIME_TIERS thresholds | Per AUDIT_FINDINGS.md §2 — tier boundaries are deliberate. Do not adjust. |
| `scripts/07` WEIGHTS per position | Per AUDIT_FINDINGS.md §7: "Never adjust weights to fix one player." Not changing any weights in clean-arch. |
| `scripts/07` validate_distribution() return True | Intentional design (see Change 273 — adds comment, doesn't change behavior). |
| `scripts/07` `apply_conference_discount()` no-op | Intentional — returns copy unchanged. Per AUDIT_FINDINGS.md §6. |
| `scripts/10` SP+/roster/recruiting blend weights | 45/30/25 blend per CLAUDE.md. Do not change without full team-rating audit. |
| `scripts/12` DEFAULT_OUTPUT path | Works for this workspace layout. Only changes if repos are restructured. |
| `scripts/12` `_build_conf_history()` overrides | 20 hardcoded conference realignment overrides — correct historical data. Do not remove. |
| `scripts/12` `_clean_nan()` | Required to prevent invalid JSON output. Do not remove or simplify. |
| `js/config.js` CONFIG values | POS_COLORS, SKILL_ATTRS, RATING_TIERS, CURRENT_SEASON — all correct; only docstrings change. |
| `js/config.js` ratingTextColor() luminance threshold | 140 luminance threshold is tuned. Per memory: badge text contrast always #111 on colored backgrounds. |
| `js/ratingsDisplay.js` regression line `over = +8` threshold | 8-point threshold for "overperformer" is deliberate. |
| `css/styles.css` tier color values | `#FFD700`, `#FFA000`, `#78909C`, `#8D6E63` — calibrated tier palette. Do not adjust. |
| `css/styles.css` `@keyframes elite-shimmer` | Intentional animation for ELITE cards. Keep. |
| `sql/schema.sql` | Reference only. Do not modify schema.sql here — schema changes go through Supabase SQL Editor. |
| `scripts/04, 05, 11` DB architecture | These scripts are intentionally DB-only (NIL, coaching, engine_b). Architecture inconsistency is known and accepted. Only fix broken SQL (Change 241) — do not migrate these to local JSON in v3. |
| `scripts/08, 08b` `build_player_index()` function | Both EA scraper scripts have their own `build_player_index()` that queries the DB. This is intentional — they need to match scraped names to DB player IDs. The function is distinct from the local-arch `build_player_index()` in scripts 04/05. Do not consolidate. |
| `scripts/10` `r2(v)` inner function inside `compute_team_splits()` | Short closure capturing nothing from outer scope; could be module-level but it's 1 line and the function is only used in its enclosing scope. Leave in place. |
| `scripts/07` `validate_distribution()` returns True always | Intentional advisory-only design. Change 273 adds a clarifying comment. The return value is not the gate — the caller (`main()`) already runs upsert unconditionally. |
| `js/playerSearch.js` `CAREER_STAT_FIELDS` vs `STAT_BLOCK_FIELDS` distinction | These two position-stat maps are similar but intentionally different: career table shows cumulative stats; stat blocks show per-season view. Do not merge them. |

---

## Deletions (Explicit List)

| Item | Action | Justification |
|------|--------|--------------|
| `utils/supabase_client.py` | Delete (or archive to `utils/archive/`) | No callers in local-arch. Confirmed by grep. |
| `requirements.txt`: `supabase==2.10.0` | Remove line | Companion to above. |
| `scripts/00_dump_supabase_to_json.py` | Move to `scripts/archive/` (not delete) | One-time utility, historically useful reference. |
| `scripts/09_backfill_defender_ids.py` | Move to `scripts/archive/` (not delete) | One-time utility. |
| `styles.css`: `.nav-brand`, `.nav-links`, `.nav-spacer` (lines 207–223) | Delete after grep confirmation | Dead CSS — no HTML references these. |
| `styles.css`: `.draft-info`, `.draft-meta` (lines 1099–1101) | Delete after grep confirmation | Comment says "no longer used." |
| `scripts/07`: `POSITION_CEILING = {}` and dead ceiling guard | Delete | Never populated; EDGE_OVR_ANCHORS is the effective ceiling. |
| `scripts/10` line 657: `"avg_starter_rating": None` | Delete from output dict | Deprecated field, no consumers. |
| `cfb-analytics-app/data/players.json`, `ratings_by_position.json`, `rosters.json`, `schedules.json`, `transfers.json` (root-level) | Delete after confirming no HTML references | Stale legacy files from prior export format. Season-specific variants (`players_{season}.json`, etc.) are canonical. No current JS loads these root-level files. |
| `styles.css` `.modal-section { }` (line ~544) | Delete empty rule | Change 608 |
| `styles.css` `.tr-sub-section {}` (line ~845) | Delete empty rule | Change 609 |
| `styles.css` `.traj-up`, `.traj-down` (lines ~368–370) | Delete orphaned rules | Change 610 — superseded by `.traj-up1/up2/down1/down2` system |

---

## Summary Counts

| Phase | Change Count | Files Affected |
|-------|-------------|---------------|
| Phase 0 (version badge) | 7 | 6 HTML + 1 CSS |
| Phase 1 (merge + tag) | 0 code changes — git ops only | — |
| Phase 2 — archive/delete | 2 scripts + 1 util + 2 requirements lines | 5 files |
| Phase 2 — bug fixes | 8 | scripts 04, 05, 07, 08, 08b, 10 |
| Phase 2 — script 11 fixes (second-pass) | 2 (Changes 313–314) | script 11 |
| Phase 2 — code quality (docstrings, type hints, section headers) | ~30 | All active scripts, all JS files |
| Phase 2 — naming clarity, Python (third-pass, Changes 320–341) | 22 | utils/db, api_client, store; scripts 01, 06, 07, 12 |
| Phase 2 — naming clarity + fixes, Python (fourth-pass, Changes 342–359) | 18 | scripts 02, 03, 05, 07, 08, 08b, 10, 11 |
| Phase 2 — naming clarity, JS (third-pass, Changes 407–418) | 12 | supabaseClient.js, config.js, playerSearch.js, ratingsDisplay.js |
| Phase 2 — naming clarity + dead code, CSS (Changes 605–610) | 6 | styles.css + 2 JS callers |
| Phase 2 — HTML semantic & consistency (Change 503) | 1 (font weight on 4 pages) | players.html, teams.html, ratings.html, research.html |
| Phase 2 — HTML: lang attr (Change 507) | 0 — already present on all 6 pages | — |
| Phase 2 — CSS dead code removal (Changes 603–604) | 2 | styles.css |
| Phase 2 — test infrastructure (Changes 801–804) | 4 new test files | tests/ (new directory) |
| Phase 2 — frontend data legacy file removal | 5 files | cfb-analytics-app/data/ root |
| **Total proposed changes** | **~117** | **~35 files** |

---

*This changelog is the review gate. No edits begin until this document has been reviewed.*
*After review, changes execute in Phase order: 0 → merge → 2 → 3.*
*Each change is numbered so progress can be tracked: mark off each change as it is applied.*
