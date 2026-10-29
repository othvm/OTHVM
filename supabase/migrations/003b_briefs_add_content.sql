-- OTHVM — add content column to briefs_registry (for dynamic briefs: the agent's content is stored +
-- can be edited via briefs.update). Run: node deploy/_sb_sql.js supabase/003b_briefs_add_content.sql
ALTER TABLE briefs_registry ADD COLUMN IF NOT EXISTS content TEXT NOT NULL DEFAULT '';
