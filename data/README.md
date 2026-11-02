# data/

Local JSON, no database. `utils/store.py` is the only code that should read or write `raw/` and `computed/`.
Most of this folder is **gitignored** (see `../.gitignore`): it is rebuilt from the API cache and the scripts, and a
fresh clone has none of it.

| Folder / file | Tracked | What it holds | Written by |
|---|---|---|---|
| `raw/` | no | What the API said, normalised: `games`, `teams`, `players`, `player_seasons`, `stats` (per-game `game_aggregate` rows and per-season `season_aggregate` rows), `recruiting`, `transfers`, `team_records`, `team_advanced_season`, and the supplemental tables. Also `player_edge.json` and `player_game_scores.json` from script 06. | 01–05, 01b, 06, 09 |
| `computed/` | no | Ratings and everything derived: `ratings.json` (one row per player-season **per engine** — always filter by `engine`), `team_ratings.json`, projections, `live_{season}.json`, `weekly_*_{season}.json`, validation outputs. | 07, 10, 11, 13–17b, validators |
| `staging/` | no | A refresh waiting to be validated (`01b_refresh_in_season.py --stage`). Safe to delete after `--merge`. | 01b |
| `models/` | yes | Trained model files for script 15. | 15 |
| `commons/` | yes | Stadium photo metadata from Wikimedia Commons. | 20 |
| `espn/` | yes | An ESPN dump read by no script (65 MB). A candidate for removal. | — |
| `raw_backup_*.zip` | one is | Snapshots of `raw/` taken before risky changes. | by hand |
| `coaching_changes_seed.csv` | **no** | Hand-seeded coaching changes. Script 05 needs it; `data/*.csv` is gitignored, so a fresh clone silently skips it. | by hand |

## Rules that hold everywhere

- **Missing means unknown, never zero.** A field the API did not send is `null`, not 0.
- **The shutout rule.** Before v4.22 the harvest stored a team's 0 points as missing. A game with exactly one score
  is therefore a shutout: `read_raw("games")` fills the 0 (`fill_shutouts`). Read `games.json` through the store, never
  raw. A game with both scores missing has not been played.
- **One team per player per season** in `player_seasons` (see `_dedupe_player_seasons` in script 01).
- **A re-harvested season is replaced, not appended** (`write_raw(..., season_key="season", seasons=[...])`).
- **`ratings.json` holds several engines** (`edge`, `engine_b`, `projected`): reading it unfiltered double-counts.
- **Partial seasons never enter `ratings.json`'s `edge` engine.** Scripts 17 and 17b rate a season being played in a
  temp copy (`CFB_DATA_DIR`) and export their own files.

The invariants behind these rules are in [`../docs/PIPELINE_INVARIANTS.md`](../docs/PIPELINE_INVARIANTS.md).
