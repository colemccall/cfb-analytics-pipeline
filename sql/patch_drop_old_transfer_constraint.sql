-- Drop the old (player_id, transfer_year) unique constraint on transfers.
-- The new constraint uq_transfers_player_year_from on (player_id, transfer_year, from_team_id)
-- supersedes it — a player can transfer from different schools in the same year (rare but valid).

-- First check what the constraint is named:
-- SELECT conname FROM pg_constraint WHERE conrelid = 'transfers'::regclass AND contype = 'u';

-- Drop by name — may be either of these depending on how it was created:
ALTER TABLE transfers DROP CONSTRAINT IF EXISTS uq_transfers_player_year;
ALTER TABLE transfers DROP CONSTRAINT IF EXISTS transfers_player_id_transfer_year_key;

-- Also drop the index form if it exists:
DROP INDEX IF EXISTS uq_transfers_player_year;
