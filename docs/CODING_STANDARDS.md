# Coding standards — cfb-analytics-pipeline

The rules for writing code in this repo, each paired with what enforces it. The
project-wide rules (evidence before shipping, registries, frozen decisions) are in
`../../CLAUDE.md` and outrank this file; the data-model invariants (join anchors, the
NaN rule, the engine filter, run order) are in `PIPELINE_INVARIANTS.md`. The app's
equivalent of this file is `../../cfb-analytics-app/CODING_STANDARDS.md`.

---

## 1. Shape

- **Scripts are numbered by run order** and each does one stage: harvest (01–05, 08,
  09), compute (06, 07, 10, 11, 13–16), export (12), validate (`validate_*.py`).
  Full recompute order: `06 → 07 → 10 → 11 → 15 → 16 → 10 (projected) → 13 → 14 → 12`.
- **`utils/store.py` is the only persistence layer.** Read with `read_raw` /
  `read_computed` / `read_ratings`; write with `write_raw` / `write_computed`. Never
  `open()` a data file for writing anywhere else without the same NaN scrub and
  replace-by-key semantics.
- **Every write replaces by key, never appends.** A season re-run must replace that
  season's rows. (`player_seasons` was appended to for two years and accumulated
  7,206 duplicate pairs.)
- **Read ratings through `read_ratings(engine)`.** `ratings.json` holds several
  engines; an unfiltered read double-counts.
- **Shared helpers go in `utils/`.** Two copies drift — scripts 04 and 05 still carry
  duplicate `_make_driver()`s, recorded rather than fixed because both scrapers are
  blocked.

## 2. Documentation in the code

- **Every module has a docstring**: what it reads, what it writes, how to run it, and
  what changed about it that a reader would otherwise have to rediscover.
- **Every module-level function or method over five lines, and every `main`, has a
  docstring.** Enforced by `tests/test_docstrings.py`: 282/282 as of 2026-09-23, from
  180/282. Nested helpers are exempt.
- **A docstring says what the code computes, not what it intends** — the same rule as
  the `formula` field in `METHODS`. When they differ, the docstring says so. Examples
  written this pass: script 10's `run_season` blends the *previous* run's team
  season stats; script 13's P5 flag reads each team's *current* conference; script
  02's matcher guesses on an ambiguous exact name.
- **Constants carry their provenance in a comment**: where the value came from and the
  measurement behind it, or that it is frozen and why (`CLAUDE.md` §3).
- **Delete dead constants.** A declared-but-unused weight is read as the formula.
  Script 10's `W_SP / W_ROSTER / W_RAW` (0.45 / 0.30 / 0.25) sat above the live 0.50 /
  0.30 / 0.20 blend until 2026-09-23.

## 3. Data rules (summary — `PIPELINE_INVARIANTS.md` is authoritative)

- **Missing means unknown, never zero** — except for summing counting stats, where
  the helper says so in its docstring (`_coerce_float`, `_num`).
- **No NaN reaches JSON.** `write_json` and the store writers use `allow_nan=False`.
- **Join through `player_seasons`**, the anchor; never through `ratings.player_id`.
- **Anchors are absolute.** No pool-percentile mapping of a published number.

## 4. Style

- Python 3.12, standard library + pandas/numpy; type hints on new public functions.
- `snake_case` functions, `UPPER_SNAKE` module constants, `_leading` for private.
- A script's CLI lives in `main()` behind `if __name__ == "__main__":`.
- Print a readout of what was written (counts, and distribution percentiles for
  anything rated) so a run can be checked by eye.
- A hard gate exits non-zero and writes nothing (`sys.exit(1)`), as scripts 15 and 16
  do. Never write a partial or distorted season.

## 5. Before you call it done

```bash
pytest tests/ -q                     # includes the docstring and frontend-constant tests
python scripts/validate_ratings.py --season 2025
python scripts/validate_vs_draft.py          # if ratings changed
python scripts/validate_reliability.py       # if anything upstream of a rating changed
```

Then the app's checks (`node --check`, `tools/doc-check.mjs`, the UI smoke suite) if
anything was exported.
