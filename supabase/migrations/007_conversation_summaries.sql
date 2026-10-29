-- OTHVM Phase 4 — conversation summaries
CREATE TABLE IF NOT EXISTS public.conversation_summaries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id text NOT NULL,
  agent_id text,
  platform text,
  title text,
  message_count int NOT NULL DEFAULT 0,
  first_message_at timestamptz,
  last_message_at timestamptz,
  last_message_id text,
  summary text NOT NULL,
  topics jsonb DEFAULT '[]'::jsonb,
  open_items jsonb DEFAULT '[]'::jsonb,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversation_summaries_thread_idx
  ON public.conversation_summaries (thread_id, created_at DESC);
CREATE INDEX IF NOT EXISTS conversation_summaries_last_idx
  ON public.conversation_summaries (thread_id, last_message_at DESC);

ALTER TABLE public.conversation_summaries ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS conversation_summaries_service ON public.conversation_summaries;
CREATE POLICY conversation_summaries_service ON public.conversation_summaries
  FOR ALL TO service_role USING (true) WITH CHECK (true);

GRANT ALL ON public.conversation_summaries TO service_role;

COMMENT ON TABLE public.conversation_summaries IS 'Rolling Arabic summaries of OTHVM conversations (every 10 agent messages).';
