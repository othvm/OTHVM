-- OTHVM — Part C1: multi-turn code editing sessions.
-- One row per file session; content holds the working copy until save_file.

CREATE TABLE IF NOT EXISTS public.code_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id text NOT NULL UNIQUE,
  path text NOT NULL,
  path_key text NOT NULL,
  project_path text,
  content text NOT NULL DEFAULT '',
  base_hash text,
  file_hash text,
  encoding text NOT NULL DEFAULT 'utf-8',
  newline text NOT NULL DEFAULT E'\n',
  had_final_newline boolean NOT NULL DEFAULT true,
  sections jsonb NOT NULL DEFAULT '[]'::jsonb,
  cursor_start integer,
  cursor_end integer,
  history jsonb NOT NULL DEFAULT '[]'::jsonb,
  edits_count integer NOT NULL DEFAULT 0,
  dirty boolean NOT NULL DEFAULT false,
  last_test jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  saved_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS code_sessions_path_key_idx ON public.code_sessions (path_key);
CREATE INDEX IF NOT EXISTS code_sessions_updated_idx ON public.code_sessions (updated_at DESC);
CREATE INDEX IF NOT EXISTS code_sessions_dirty_idx ON public.code_sessions (dirty) WHERE dirty;

CREATE OR REPLACE FUNCTION public.code_sessions_touch()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS code_sessions_touch_trg ON public.code_sessions;
CREATE TRIGGER code_sessions_touch_trg
BEFORE UPDATE ON public.code_sessions
FOR EACH ROW EXECUTE FUNCTION public.code_sessions_touch();

ALTER TABLE public.code_sessions ENABLE ROW LEVEL SECURITY;

NOTIFY pgrst, 'reload schema';
