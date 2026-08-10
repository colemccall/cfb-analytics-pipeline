# Archived SQL schema (Supabase era)

**Nothing in this folder is executed by the pipeline.** The project moved off
Supabase/PostgreSQL entirely — all data is local JSON under `data/raw/` and
`data/computed/`, read and written through `utils/store.py`.

These files are kept as reference only: the JSON files mirror these table
definitions one-to-one (`players.json` ↔ the `players` table, and so on), so the
DDL is still the clearest description of what each field means and which keys
are unique.

If you are looking for the current data model, see the "Data Model" section of
the top-level `README.md` instead.
