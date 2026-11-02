"""The season facts more than one script relies on, in one place.

Each script used to type these itself (2008, 2027, 12), and copies drift: the --all-seasons ranges read 2008-2026 in
scripts 06, 07 and 10 but 2008-2025 in script 01. A value here changes every script that reads it, so change it
deliberately, when a new season is added.

The app mirrors FIRST_SEASON and CURRENT_SEASON in js/config.js; tests/test_export_contract.py checks they agree with
the exported manifest.
"""

FIRST_SEASON = 2008          # the first season with data on file
CURRENT_SEASON = 2026        # the season the app opens on (being played, or about to be)
ALL_SEASONS = list(range(FIRST_SEASON, CURRENT_SEASON + 1))

REGULAR_SEASON_GAMES = 12    # the length of a regular season the in-season rules are written on: a game graded as
                             # one of 12 (script 17b), the 8-of-12-games confidence rule (script 07), the team
                             # readings' part-season shrink (script 10)
