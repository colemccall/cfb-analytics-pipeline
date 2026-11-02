# utils/

Shared modules the scripts import. Nothing here runs on its own.

| Module | What it is | Who uses it |
|---|---|---|
| `store.py` | **The only persistence layer.** `read_raw` / `write_raw` (`data/raw/`), `read_computed` / `write_computed` (`data/computed/`), `read_ratings(engine)`. `CFB_DATA_DIR` points the whole store at a private copy (scripts 17, 17b and the validators). `read_raw("games")` applies `fill_shutouts`: the harvest once stored a shutout's 0 as missing, so a game with exactly one score is a shutout. | every script |
| `api_client.py` | The CFB Data API: one `fetch_*` per endpoint, a file cache in `.cache/` with no expiry (`CFB_REFRESH_YEARS=2026` bypasses it for that year), `fetch_fresh` / `cache_read` / `cache_write` for a staged refresh. The key comes from the `CFB_API_KEY` environment variable (`.env`, gitignored). | harvest scripts, 06, 10 |
| `seasons.py` | The season facts several scripts share: `FIRST_SEASON`, `CURRENT_SEASON`, `ALL_SEASONS`, `REGULAR_SEASON_GAMES` (12). | 06, 07, 10, 12, 17b |
| `json_utils.py` | `clean_nan` and JSON writing without the `NaN` token. | store, exports |
| `stat_agg.py` | Rebuilds a season's totals from its per-game rows (for players the API wrote no aggregate for). | 07 |
| `box_scores.py` | A team's season totals summed from per-game box scores through a week, so a season can be read as it stood after week N. | 10 (`--through-week`) |
| `opponent_strength.py` | Our own opponent strengths from game results (an offense and a defense per team; FCS pooled). | 06, 10, 17b |
| `results_rating.py` | Power: the number teams are ranked by. | 10 |
| `line_unit.py` | The offensive-line unit rating (the per-lineman rating was withdrawn). | 10 |
| `class_year.py` | A real class year (the stored `year` is constant across most careers). | 12, 15 |
| `coaching.py`, `draft.py` | Head-coach tenure; who left for the NFL. | research scripts |
| `matching.py` | Name and team matching for feeds without player ids. | 02, 03 |
| `shrinkage.py` | Empirical-Bayes shrinkage and intervals for the ranked research findings. | 13, 14 |
