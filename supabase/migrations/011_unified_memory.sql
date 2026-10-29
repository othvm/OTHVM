-- ============================================================
-- OTHVM — Unified Memory v1 (memory_items / memory_events)
-- ============================================================
-- This is the canonical memory store used by mcp_server/main.py
-- (op_memory / cmd_memory / /recall). The legacy tables
-- othman_memory and othvm_memory are kept for compatibility and
-- are mirrored into memory_items by triggers.
--
-- Embeddings: Gemini gemini-embedding-001 at 3072 dimensions.
-- The full vector lives in embedding; embedding_half is a
-- generated halfvec mirror used by the HNSW index (pgvector
-- cannot build HNSW above 4000 dims for halfvec / 2000 for vector).
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ------------------------------------------------------------
-- memory_items
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.memory_items (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace        text NOT NULL DEFAULT 'personal',
    category         text NOT NULL,
    key              text NOT NULL,
    value            text NOT NULL DEFAULT '',
    importance       smallint NOT NULL DEFAULT 3,
    status           text NOT NULL DEFAULT 'active',
    version          integer NOT NULL DEFAULT 1,
    source           text NOT NULL DEFAULT 'agent',
    metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding        vector(3072),
    embedding_half   halfvec(3072) GENERATED ALWAYS AS (embedding::halfvec(3072)) STORED,
    search_vector    tsvector GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig,
            COALESCE(namespace, '') || ' ' || COALESCE(category, '') || ' ' ||
            COALESCE(key, '') || ' ' || COALESCE(value, ''))
    ) STORED,
    access_count     integer NOT NULL DEFAULT 0,
    last_accessed_at timestamptz,
    superseded_by    uuid,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    value_hash       text GENERATED ALWAYS AS (md5(value)) STORED,
    CONSTRAINT memory_items_namespace_category_key_key UNIQUE (namespace, category, key)
);

CREATE INDEX IF NOT EXISTS memory_items_namespace_idx
    ON public.memory_items (namespace, status);
CREATE INDEX IF NOT EXISTS memory_items_category_idx
    ON public.memory_items (namespace, category);
CREATE INDEX IF NOT EXISTS memory_items_status_idx
    ON public.memory_items (status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS memory_items_updated_idx
    ON public.memory_items (updated_at DESC);
CREATE INDEX IF NOT EXISTS memory_items_value_hash_idx
    ON public.memory_items (namespace, category, value_hash);
CREATE INDEX IF NOT EXISTS memory_items_search_idx
    ON public.memory_items USING gin (search_vector);
CREATE INDEX IF NOT EXISTS memory_items_embedding_half_idx
    ON public.memory_items USING hnsw (embedding_half halfvec_cosine_ops);

COMMENT ON TABLE public.memory_items IS
  'Unified agent memory v1: one row per (namespace, category, key). Values are concise persistent facts, not project documentation.';
COMMENT ON COLUMN public.memory_items.embedding IS
  'Gemini gemini-embedding-001 vector (3072 dims) of namespace/category/key/value.';
COMMENT ON COLUMN public.memory_items.status IS
  'active | forgotten | superseded. Only active rows are retrievable.';

-- ------------------------------------------------------------
-- memory_events (append-only audit log)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.memory_events (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id  uuid,
    namespace  text NOT NULL DEFAULT 'personal',
    category   text NOT NULL DEFAULT '',
    key        text NOT NULL DEFAULT '',
    action     text NOT NULL,
    old_value  text,
    new_value  text,
    reason     text,
    actor      text NOT NULL DEFAULT 'agent',
    metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_events_memory_idx
    ON public.memory_events (memory_id, created_at DESC);

COMMENT ON TABLE public.memory_events IS
  'Append-only audit trail for memory writes, deletes, merges, and restores. Not injected into prompts.';

-- ------------------------------------------------------------
-- Retrieval functions
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.match_memory(
    query_embedding vector,
    query_text text DEFAULT NULL,
    match_count integer DEFAULT 8,
    filter_namespace text DEFAULT NULL,
    filter_category text DEFAULT NULL,
    min_similarity double precision DEFAULT 0.0
) RETURNS TABLE (
    id uuid,
    namespace text,
    category text,
    key text,
    value text,
    importance smallint,
    version integer,
    similarity double precision,
    score double precision,
    updated_at timestamptz,
    metadata jsonb
)
LANGUAGE sql STABLE
AS $$
  WITH q AS (
    SELECT query_embedding::halfvec(3072) AS emb,
           CASE WHEN query_text IS NULL OR btrim(query_text) = '' THEN NULL
                ELSE plainto_tsquery('simple', query_text) END AS tsq
  )
  SELECT m.id, m.namespace, m.category, m.key, m.value, m.importance, m.version,
         1 - (m.embedding_half <=> q.emb) AS similarity,
         (0.85 * (1 - (m.embedding_half <=> q.emb))) +
         (0.15 * CASE WHEN q.tsq IS NOT NULL AND m.search_vector @@ q.tsq
                       THEN LEAST(1.0, ts_rank_cd(m.search_vector, q.tsq) * 4.0)
                       ELSE 0 END) AS score,
         m.updated_at, m.metadata
  FROM public.memory_items m, q
  WHERE m.status = 'active'
    AND m.embedding IS NOT NULL
    AND (filter_namespace IS NULL OR m.namespace = filter_namespace)
    AND (filter_category IS NULL OR m.category = filter_category)
    AND (1 - (m.embedding_half <=> q.emb)) >= min_similarity
  ORDER BY score DESC, similarity DESC, m.importance DESC, m.updated_at DESC
  LIMIT LEAST(GREATEST(match_count, 1), 100);
$$;

CREATE OR REPLACE FUNCTION public.search_memory_text(
    p_query text,
    p_namespace text DEFAULT NULL,
    p_category text DEFAULT NULL,
    p_limit integer DEFAULT 10
) RETURNS TABLE (
    id uuid,
    namespace text,
    category text,
    key text,
    value text,
    importance smallint,
    version integer,
    updated_at timestamptz,
    metadata jsonb
)
LANGUAGE sql STABLE
AS $$
  SELECT m.id, m.namespace, m.category, m.key, m.value, m.importance, m.version,
         m.updated_at, m.metadata
  FROM public.memory_items m
  WHERE m.status = 'active'
    AND (p_namespace IS NULL OR m.namespace = p_namespace)
    AND (p_category IS NULL OR m.category = p_category)
    AND (m.value ILIKE '%' || p_query || '%'
         OR m.key ILIKE '%' || p_query || '%'
         OR m.category ILIKE '%' || p_query || '%')
  ORDER BY m.importance DESC, m.updated_at DESC
  LIMIT LEAST(GREATEST(p_limit, 1), 50);
$$;

CREATE OR REPLACE FUNCTION public.touch_memory(p_ids uuid[])
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE n integer;
BEGIN
  UPDATE memory_items
     SET access_count = access_count + 1,
         last_accessed_at = now()
   WHERE id = ANY(p_ids);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END
$$;

CREATE OR REPLACE FUNCTION public.memory_set_embeddings(p_items jsonb)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE n integer := 0; item jsonb;
BEGIN
  FOR item IN SELECT * FROM jsonb_array_elements(p_items) LOOP
    UPDATE memory_items
       SET embedding = (item->>'embedding')::vector
     WHERE id = (item->>'id')::uuid;
    n := n + 1;
  END LOOP;
  RETURN n;
END
$$;

REVOKE ALL ON FUNCTION public.touch_memory(uuid[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.memory_set_embeddings(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.touch_memory(uuid[]) TO service_role;
GRANT EXECUTE ON FUNCTION public.memory_set_embeddings(jsonb) TO service_role;

-- ------------------------------------------------------------
-- Legacy mirror: keep othman_memory / othvm_memory in sync.
-- The trigger is intentionally one-way (legacy -> unified) and
-- skips values that already exist under another key.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.sync_legacy_memory()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE ns text := TG_ARGV[0];
BEGIN
  IF TG_OP = 'DELETE' THEN
    UPDATE public.memory_items
       SET status = 'forgotten', embedding = NULL, updated_at = now()
     WHERE namespace = ns AND category = OLD.category AND key = OLD.key
       AND status = 'active';
    RETURN OLD;
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.memory_items
     WHERE namespace = ns AND status = 'active'
       AND value_hash = md5(NEW.value)
       AND NOT (category = NEW.category AND key = NEW.key)
  ) THEN
    RETURN NEW;
  END IF;

  INSERT INTO public.memory_items
    (namespace, category, key, value, version, source, metadata, created_at, updated_at)
  VALUES
    (ns, NEW.category, NEW.key, NEW.value, NEW.version,
     'legacy:' || TG_TABLE_NAME,
     jsonb_build_object('legacy_table', TG_TABLE_NAME),
     NEW.last_updated, NEW.last_updated)
  ON CONFLICT (namespace, category, key) DO UPDATE
    SET value = EXCLUDED.value,
        version = GREATEST(memory_items.version, EXCLUDED.version),
        updated_at = GREATEST(memory_items.updated_at, EXCLUDED.updated_at),
        status = 'active'
    WHERE memory_items.value IS DISTINCT FROM EXCLUDED.value
       OR memory_items.status <> 'active';
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_sync_othman_memory ON public.othman_memory;
CREATE TRIGGER trg_sync_othman_memory
    AFTER INSERT OR UPDATE OR DELETE ON public.othman_memory
    FOR EACH ROW EXECUTE FUNCTION public.sync_legacy_memory('personal');

DROP TRIGGER IF EXISTS trg_sync_othvm_memory ON public.othvm_memory;
CREATE TRIGGER trg_sync_othvm_memory
    AFTER INSERT OR UPDATE OR DELETE ON public.othvm_memory
    FOR EACH ROW EXECUTE FUNCTION public.sync_legacy_memory('system');

-- ------------------------------------------------------------
-- Backfill legacy rows into unified memory (idempotent).
-- Existing unified rows win: consolidation is never overwritten
-- by re-running this migration.
-- ------------------------------------------------------------
INSERT INTO public.memory_items
    (namespace, category, key, value, version, source, metadata, created_at, updated_at)
SELECT 'personal', category, key, value, version, 'migration:othman_memory',
       jsonb_build_object('legacy_table', 'othman_memory'), last_updated, last_updated
FROM public.othman_memory
ON CONFLICT (namespace, category, key) DO NOTHING;

INSERT INTO public.memory_items
    (namespace, category, key, value, version, source, metadata, created_at, updated_at)
SELECT 'system', category, key, value, version, 'migration:othvm_memory',
       jsonb_build_object('legacy_table', 'othvm_memory'), last_updated, last_updated
FROM public.othvm_memory
ON CONFLICT (namespace, category, key) DO NOTHING;

-- RLS is disabled on the memory tables in the live project; access is
-- restricted by using the service_role key from the MCP server only.
ALTER TABLE public.memory_items DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_events DISABLE ROW LEVEL SECURITY;
GRANT ALL ON public.memory_items TO service_role;
GRANT ALL ON public.memory_events TO service_role;
REVOKE ALL ON public.memory_items FROM anon, authenticated;
REVOKE ALL ON public.memory_events FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
