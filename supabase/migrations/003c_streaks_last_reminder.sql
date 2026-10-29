-- OTHVM — Part B: track the last day a streak reminder was sent (keeps the
-- per-minute checker idempotent: sends the daily reminder exactly once).
-- Run: node deploy/_sb_sql.js supabase/003c_streaks_last_reminder.sql
ALTER TABLE streaks ADD COLUMN IF NOT EXISTS last_reminder_sent DATE;
