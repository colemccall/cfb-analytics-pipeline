-- Phase D: Add sub_ratings JSONB column to ratings table.
-- sub_ratings stores position-specific advanced metrics derived from play-by-play.
-- For display and research only — not weighted into overall_rating.
-- Run in Supabase SQL Editor before running the new script 06.

ALTER TABLE ratings
    ADD COLUMN IF NOT EXISTS sub_ratings JSONB;

COMMENT ON COLUMN ratings.sub_ratings IS
    'Position-specific advanced metrics from plays table. Examples per position: '
    'QB: explosive_pass_rate, deep_ball_rate, third_down_rating, clutch_rating, turnover_worthy_rate. '
    'RB: explosive_run_rate, yards_after_contact, third_down_rating, clutch_rating, receiving_versatility. '
    'WR/TE: explosive_catch_rate, deep_target_share, yards_after_catch, third_down_rating, td_rate. '
    'EDGE/DL/LB/CB/S: third_down_rating, passing_down_rating, clutch_rating, '
    'explosive_play_allow_rate, ball_skills_rating, run_stop_grade. '
    'Values normalized 0-1 via percentile rank within position group, except raw rates.';
