-- ============================================================
-- OTHVM — Runtime tables used by the MCP server and workflows
-- ============================================================
-- These tables exist in the live Supabase project but were not
-- created by earlier migrations. They hold short-lived operational
-- state (queues, reminders, loops, flags), not durable memory.
-- ============================================================

-- ------------------------------------------------------------
-- agent_registry — known agents and their capabilities
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.agent_registry (
    agent_key    text PRIMARY KEY,
    display_name text NOT NULL,
    description  text NOT NULL DEFAULT '',
    capabilities jsonb DEFAULT '[]'::jsonb,
    is_active    boolean NOT NULL DEFAULT true
);

ALTER TABLE public.agent_registry ENABLE ROW LEVEL SECURITY;
GRANT ALL ON public.agent_registry TO service_role;

-- ------------------------------------------------------------
-- conversation_log — raw agent messages mirrored from n8n SQLite
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.conversation_log (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    text NOT NULL DEFAULT '',
    thread_id     text NOT NULL DEFAULT '',
    role          text NOT NULL DEFAULT 'user',
    content       text NOT NULL DEFAULT '',
    message_ref   text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    summarized_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS conversation_log_message_ref_key
    ON public.conversation_log (message_ref);
CREATE INDEX IF NOT EXISTS conversation_log_created_idx
    ON public.conversation_log (created_at);
CREATE INDEX IF NOT EXISTS conversation_log_unsummarized_idx
    ON public.conversation_log (summarized_at) WHERE summarized_at IS NULL;

ALTER TABLE public.conversation_log ENABLE ROW LEVEL SECURITY;
GRANT ALL ON public.conversation_log TO service_role;

COMMENT ON TABLE public.conversation_log IS
  'Raw conversation transcript mirrored from the n8n agents_messages table; summarized into conversation_summaries.';

-- ------------------------------------------------------------
-- loops — recurring check-ins / follow-ups
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.loops (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title          text NOT NULL,
    kind           text NOT NULL DEFAULT 'checkin',
    prompt         text NOT NULL,
    cadence_minutes integer NOT NULL DEFAULT 30,
    next_run_at    timestamptz NOT NULL DEFAULT now(),
    last_run_at    timestamptz,
    status         text NOT NULL DEFAULT 'active',
    runs           integer NOT NULL DEFAULT 0,
    max_runs       integer NOT NULL DEFAULT 10,
    ack_required   boolean NOT NULL DEFAULT true,
    acked_at       timestamptz,
    snoozes        integer NOT NULL DEFAULT 0,
    chat_id        text,
    goal_id        uuid REFERENCES public.goals_memory(id),
    stop_condition text,
    metadata       jsonb DEFAULT '{}'::jsonb,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    created_at     timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.loops ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS loops_service_all ON public.loops;
CREATE POLICY loops_service_all ON public.loops
    FOR ALL TO service_role USING (true) WITH CHECK (true);
GRANT ALL ON public.loops TO service_role;

-- ------------------------------------------------------------
-- message_buffer — inbound Telegram messages awaiting processing
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.message_buffer (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id     text,
    message_id  bigint,
    content     text,
    received_at timestamptz NOT NULL DEFAULT now(),
    processed   boolean NOT NULL DEFAULT false,
    CONSTRAINT message_buffer_chat_message_key UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS message_buffer_pending_idx
    ON public.message_buffer (chat_id, processed, received_at);

ALTER TABLE public.message_buffer ENABLE ROW LEVEL SECURITY;
GRANT ALL ON public.message_buffer TO service_role;

-- ------------------------------------------------------------
-- notes — quick free-form notes
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.notes (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content    text NOT NULL,
    tags       text[] NOT NULL DEFAULT '{}'::text[],
    is_active  boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS notes_active_idx
    ON public.notes (is_active, created_at DESC);

ALTER TABLE public.notes DISABLE ROW LEVEL SECURITY;
GRANT ALL ON public.notes TO service_role;

-- ------------------------------------------------------------
-- reminders — one-off scheduled reminders
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.reminders (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    text       text NOT NULL,
    remind_at  timestamptz NOT NULL,
    status     text NOT NULL DEFAULT 'pending',
    chat_id    text,
    sent_at    timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reminders_due_idx
    ON public.reminders (status, remind_at);

ALTER TABLE public.reminders DISABLE ROW LEVEL SECURITY;
GRANT ALL ON public.reminders TO service_role;

-- ------------------------------------------------------------
-- sleep_sessions — sleep / wake tracking
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.sleep_sessions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id       text NOT NULL DEFAULT '',
    label         text NOT NULL DEFAULT 'Sleep',
    start_at      timestamptz NOT NULL DEFAULT now(),
    wake_at       timestamptz NOT NULL,
    wake_check_at timestamptz,
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','notified','woke','snoozed','missed','cancelled')),
    reminders     integer NOT NULL DEFAULT 0,
    snoozes       integer NOT NULL DEFAULT 0,
    last_ping_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sleep_sessions_chat_idx
    ON public.sleep_sessions (chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS sleep_sessions_due_idx
    ON public.sleep_sessions (status, wake_check_at);

ALTER TABLE public.sleep_sessions ENABLE ROW LEVEL SECURITY;
GRANT ALL ON public.sleep_sessions TO service_role;

CREATE OR REPLACE FUNCTION public.sleep_action(
    p_session_id uuid,
    p_action text,
    p_minutes integer DEFAULT 10
) RETURNS public.sleep_sessions
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE r public.sleep_sessions;
BEGIN
  UPDATE public.sleep_sessions SET
    status = CASE lower(p_action)
      WHEN 'woke' THEN 'woke'
      WHEN 'still' THEN 'notified'
      WHEN 'snooze' THEN 'snoozed'
      WHEN 'cancel' THEN 'cancelled'
      ELSE status END,
    wake_check_at = CASE lower(p_action)
      WHEN 'still' THEN now() + interval '5 minutes'
      WHEN 'snooze' THEN now() + make_interval(mins => greatest(1, least(120, coalesce(p_minutes, 10))))
      ELSE wake_check_at END,
    snoozes = snoozes + CASE WHEN lower(p_action) = 'snooze' THEN 1 ELSE 0 END,
    last_ping_at = now(),
    updated_at = now()
  WHERE id = p_session_id
  RETURNING * INTO r;
  RETURN r;
END
$$;

REVOKE ALL ON FUNCTION public.sleep_action(uuid, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.sleep_action(uuid, text, integer) TO service_role;

-- ------------------------------------------------------------
-- social_posts — outbound social publishing log
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.social_posts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    platform    text NOT NULL,
    content     text,
    media_url   text,
    external_id text,
    url         text,
    status      text NOT NULL,
    error       text,
    metadata    jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.social_posts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS social_posts_service_all ON public.social_posts;
CREATE POLICY social_posts_service_all ON public.social_posts
    FOR ALL TO service_role USING (true) WITH CHECK (true);
GRANT ALL ON public.social_posts TO service_role;

-- ------------------------------------------------------------
-- system_flags — small JSON key/value runtime flags
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.system_flags (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text
);

ALTER TABLE public.system_flags ENABLE ROW LEVEL SECURITY;
GRANT ALL ON public.system_flags TO service_role;

-- ------------------------------------------------------------
-- todos — lightweight local task list
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.todos (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at   timestamptz NOT NULL DEFAULT now(),
    title        text,
    notes        text DEFAULT '',
    status       text DEFAULT 'pending',
    priority     integer DEFAULT 2,
    due_date     date,
    completed_at timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.todos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS todos_service_all ON public.todos;
CREATE POLICY todos_service_all ON public.todos
    FOR ALL TO service_role USING (true) WITH CHECK (true);
GRANT ALL ON public.todos TO service_role;

NOTIFY pgrst, 'reload schema';
