-- ============================================================
-- OTHVM — Memory Graph v1 (013)
-- ============================================================
-- Extends the unified memory store with structured entities,
-- explicit relations, episodes, and provenance links.  The
-- canonical fact store remains public.memory_items; nothing is
-- moved or deleted by this migration.
--
--   memory_entities        typed things (person, project, tool, ...)
--   memory_relations       explicit directed edges with validity
--   memory_episodes        time-bounded events/conversations
--   memory_entity_mentions entity -> memory_items provenance
--   memory_episode_items   episode -> memory_items provenance
--   memory_episode_entities episode -> entity provenance
--
-- Retrieval functions mirror the existing memory RPCs and add
-- bounded graph traversal.  All statements are idempotent.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ------------------------------------------------------------
-- updated_at trigger helper (shared by the graph tables)
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.memory_graph_touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

-- ------------------------------------------------------------
-- memory_entities
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.memory_entities (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace        text NOT NULL DEFAULT 'personal',
    entity_type      text NOT NULL DEFAULT 'concept',
    name             text NOT NULL,
    canonical_key    text NOT NULL,
    aliases          text[] NOT NULL DEFAULT '{}'::text[],
    description      text NOT NULL DEFAULT '',
    attributes       jsonb NOT NULL DEFAULT '{}'::jsonb,
    importance       smallint NOT NULL DEFAULT 3,
    status           text NOT NULL DEFAULT 'active',
    source           text NOT NULL DEFAULT 'agent',
    metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding        vector(3072),
    embedding_half   halfvec(3072) GENERATED ALWAYS AS (embedding::halfvec(3072)) STORED,
    search_vector    tsvector GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig,
            COALESCE(namespace, '') || ' ' || COALESCE(entity_type, '') || ' ' ||
            COALESCE(name, '') || ' ' || COALESCE(description, ''))
    ) STORED,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT memory_entities_namespace_key_key UNIQUE (namespace, canonical_key),
    CONSTRAINT memory_entities_status_check CHECK (status IN ('active', 'forgotten', 'merged')),
    CONSTRAINT memory_entities_importance_check CHECK (importance BETWEEN 1 AND 5)
);

CREATE INDEX IF NOT EXISTS memory_entities_namespace_type_idx
    ON public.memory_entities (namespace, entity_type, status);
CREATE INDEX IF NOT EXISTS memory_entities_name_idx
    ON public.memory_entities (lower(name));
CREATE INDEX IF NOT EXISTS memory_entities_aliases_idx
    ON public.memory_entities USING gin (aliases);
CREATE INDEX IF NOT EXISTS memory_entities_search_idx
    ON public.memory_entities USING gin (search_vector);
CREATE INDEX IF NOT EXISTS memory_entities_embedding_half_idx
    ON public.memory_entities USING hnsw (embedding_half halfvec_cosine_ops);

DROP TRIGGER IF EXISTS trg_memory_entities_updated_at ON public.memory_entities;
CREATE TRIGGER trg_memory_entities_updated_at
    BEFORE UPDATE ON public.memory_entities
    FOR EACH ROW EXECUTE FUNCTION public.memory_graph_touch_updated_at();

COMMENT ON TABLE public.memory_entities IS
  'Typed entities for structured memory. canonical_key is a stable slug unique per namespace.';
COMMENT ON COLUMN public.memory_entities.aliases IS
  'Alternative surface forms used for explicit keyword mention matching and text search.';

-- ------------------------------------------------------------
-- memory_relations
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.memory_relations (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace        text NOT NULL DEFAULT 'personal',
    from_entity_id   uuid NOT NULL REFERENCES public.memory_entities(id) ON DELETE CASCADE,
    predicate        text NOT NULL,
    to_entity_id     uuid NOT NULL REFERENCES public.memory_entities(id) ON DELETE CASCADE,
    confidence       real NOT NULL DEFAULT 1.0,
    status           text NOT NULL DEFAULT 'active',
    source           text NOT NULL DEFAULT 'agent',
    metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    valid_from       timestamptz NOT NULL DEFAULT now(),
    valid_to         timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT memory_relations_unique UNIQUE (namespace, from_entity_id, predicate, to_entity_id),
    CONSTRAINT memory_relations_no_self CHECK (from_entity_id <> to_entity_id),
    CONSTRAINT memory_relations_status_check CHECK (status IN ('active', 'forgotten', 'expired')),
    CONSTRAINT memory_relations_confidence_check CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE INDEX IF NOT EXISTS memory_relations_from_idx
    ON public.memory_relations (from_entity_id, predicate, status);
CREATE INDEX IF NOT EXISTS memory_relations_to_idx
    ON public.memory_relations (to_entity_id, predicate, status);
CREATE INDEX IF NOT EXISTS memory_relations_namespace_idx
    ON public.memory_relations (namespace, status);
CREATE INDEX IF NOT EXISTS memory_relations_validity_idx
    ON public.memory_relations (valid_from, valid_to) WHERE status = 'active';

DROP TRIGGER IF EXISTS trg_memory_relations_updated_at ON public.memory_relations;
CREATE TRIGGER trg_memory_relations_updated_at
    BEFORE UPDATE ON public.memory_relations
    FOR EACH ROW EXECUTE FUNCTION public.memory_graph_touch_updated_at();

COMMENT ON TABLE public.memory_relations IS
  'Explicit directed relationships with confidence and temporal validity. No inferred edges are stored here.';
COMMENT ON COLUMN public.memory_relations.valid_to IS
  'NULL means still valid. Expired/forgotten edges stay for audit but are not traversed.';

-- ------------------------------------------------------------
-- memory_episodes
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.memory_episodes (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace        text NOT NULL DEFAULT 'personal',
    session_id       text,
    title            text NOT NULL DEFAULT '',
    summary          text NOT NULL DEFAULT '',
    occurred_at      timestamptz NOT NULL DEFAULT now(),
    ended_at         timestamptz,
    participants     jsonb NOT NULL DEFAULT '[]'::jsonb,
    importance       smallint NOT NULL DEFAULT 3,
    status           text NOT NULL DEFAULT 'active',
    source           text NOT NULL DEFAULT 'agent',
    metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding        vector(3072),
    embedding_half   halfvec(3072) GENERATED ALWAYS AS (embedding::halfvec(3072)) STORED,
    search_vector    tsvector GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig,
            COALESCE(namespace, '') || ' ' || COALESCE(title, '') || ' ' ||
            COALESCE(summary, ''))
    ) STORED,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT memory_episodes_status_check CHECK (status IN ('active', 'forgotten')),
    CONSTRAINT memory_episodes_importance_check CHECK (importance BETWEEN 1 AND 5)
);

CREATE INDEX IF NOT EXISTS memory_episodes_namespace_time_idx
    ON public.memory_episodes (namespace, occurred_at DESC);
CREATE INDEX IF NOT EXISTS memory_episodes_session_idx
    ON public.memory_episodes (session_id);
CREATE INDEX IF NOT EXISTS memory_episodes_search_idx
    ON public.memory_episodes USING gin (search_vector);
CREATE INDEX IF NOT EXISTS memory_episodes_embedding_half_idx
    ON public.memory_episodes USING hnsw (embedding_half halfvec_cosine_ops);

DROP TRIGGER IF EXISTS trg_memory_episodes_updated_at ON public.memory_episodes;
CREATE TRIGGER trg_memory_episodes_updated_at
    BEFORE UPDATE ON public.memory_episodes
    FOR EACH ROW EXECUTE FUNCTION public.memory_graph_touch_updated_at();

COMMENT ON TABLE public.memory_episodes IS
  'Episodic memory: a time-bounded event, conversation, or execution summary with provenance links.';

-- ------------------------------------------------------------
-- Provenance link tables
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.memory_entity_mentions (
    entity_id   uuid NOT NULL REFERENCES public.memory_entities(id) ON DELETE CASCADE,
    memory_id   uuid NOT NULL REFERENCES public.memory_items(id) ON DELETE CASCADE,
    namespace   text NOT NULL DEFAULT 'personal',
    confidence  real NOT NULL DEFAULT 1.0,
    source      text NOT NULL DEFAULT 'agent',
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, memory_id)
);

CREATE INDEX IF NOT EXISTS memory_entity_mentions_memory_idx
    ON public.memory_entity_mentions (memory_id);
CREATE INDEX IF NOT EXISTS memory_entity_mentions_entity_idx
    ON public.memory_entity_mentions (entity_id, confidence DESC);

CREATE TABLE IF NOT EXISTS public.memory_episode_items (
    episode_id  uuid NOT NULL REFERENCES public.memory_episodes(id) ON DELETE CASCADE,
    memory_id   uuid NOT NULL REFERENCES public.memory_items(id) ON DELETE CASCADE,
    role        text NOT NULL DEFAULT 'mentioned',
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (episode_id, memory_id)
);

CREATE INDEX IF NOT EXISTS memory_episode_items_memory_idx
    ON public.memory_episode_items (memory_id);

CREATE TABLE IF NOT EXISTS public.memory_episode_entities (
    episode_id  uuid NOT NULL REFERENCES public.memory_episodes(id) ON DELETE CASCADE,
    entity_id   uuid NOT NULL REFERENCES public.memory_entities(id) ON DELETE CASCADE,
    role        text NOT NULL DEFAULT 'participant',
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (episode_id, entity_id)
);

CREATE INDEX IF NOT EXISTS memory_episode_entities_entity_idx
    ON public.memory_episode_entities (entity_id);

COMMENT ON TABLE public.memory_entity_mentions IS
  'Provenance: which memory items explicitly mention an entity. Links are only created by explicit keyword/ID evidence.';
COMMENT ON TABLE public.memory_episode_items IS
  'Provenance: memory items attached to an episode.';
COMMENT ON TABLE public.memory_episode_entities IS
  'Provenance: entities participating in an episode.';

-- ------------------------------------------------------------
-- Entity retrieval
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.match_entities(
    query_embedding vector,
    query_text text DEFAULT NULL,
    match_count integer DEFAULT 8,
    filter_namespace text DEFAULT NULL,
    filter_type text DEFAULT NULL,
    min_similarity double precision DEFAULT 0.0
) RETURNS TABLE (
    id uuid,
    namespace text,
    entity_type text,
    name text,
    canonical_key text,
    aliases text[],
    description text,
    attributes jsonb,
    importance smallint,
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
  SELECT e.id, e.namespace, e.entity_type, e.name, e.canonical_key, e.aliases,
         e.description, e.attributes, e.importance,
         1 - (e.embedding_half <=> q.emb) AS similarity,
         (0.85 * (1 - (e.embedding_half <=> q.emb))) +
         (0.15 * CASE WHEN q.tsq IS NOT NULL AND e.search_vector @@ q.tsq
                       THEN LEAST(1.0, ts_rank_cd(e.search_vector, q.tsq) * 4.0)
                       ELSE 0 END) AS score,
         e.updated_at, e.metadata
  FROM public.memory_entities e, q
  WHERE e.status = 'active'
    AND e.embedding IS NOT NULL
    AND (filter_namespace IS NULL OR e.namespace = filter_namespace)
    AND (filter_type IS NULL OR e.entity_type = filter_type)
    AND (1 - (e.embedding_half <=> q.emb)) >= min_similarity
  ORDER BY score DESC, similarity DESC, e.importance DESC, e.updated_at DESC
  LIMIT LEAST(GREATEST(match_count, 1), 100);
$$;

CREATE OR REPLACE FUNCTION public.search_entities_text(
    p_query text,
    p_namespace text DEFAULT NULL,
    p_type text DEFAULT NULL,
    p_limit integer DEFAULT 10
) RETURNS TABLE (
    id uuid,
    namespace text,
    entity_type text,
    name text,
    canonical_key text,
    aliases text[],
    description text,
    attributes jsonb,
    importance smallint,
    updated_at timestamptz,
    metadata jsonb
)
LANGUAGE sql STABLE
AS $$
  SELECT e.id, e.namespace, e.entity_type, e.name, e.canonical_key, e.aliases,
         e.description, e.attributes, e.importance, e.updated_at, e.metadata
  FROM public.memory_entities e
  WHERE e.status = 'active'
    AND (p_namespace IS NULL OR e.namespace = p_namespace)
    AND (p_type IS NULL OR e.entity_type = p_type)
    AND (
        e.name ILIKE '%' || p_query || '%'
        OR e.canonical_key ILIKE '%' || p_query || '%'
        OR e.description ILIKE '%' || p_query || '%'
        OR EXISTS (SELECT 1 FROM unnest(e.aliases) AS a WHERE a ILIKE '%' || p_query || '%')
    )
  ORDER BY e.importance DESC, e.updated_at DESC
  LIMIT LEAST(GREATEST(p_limit, 1), 100);
$$;

-- ------------------------------------------------------------
-- Episode retrieval
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.match_episodes(
    query_embedding vector,
    query_text text DEFAULT NULL,
    match_count integer DEFAULT 8,
    filter_namespace text DEFAULT NULL,
    p_from timestamptz DEFAULT NULL,
    p_to timestamptz DEFAULT NULL,
    min_similarity double precision DEFAULT 0.0
) RETURNS TABLE (
    id uuid,
    namespace text,
    session_id text,
    title text,
    summary text,
    occurred_at timestamptz,
    ended_at timestamptz,
    participants jsonb,
    importance smallint,
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
  SELECT ep.id, ep.namespace, ep.session_id, ep.title, ep.summary,
         ep.occurred_at, ep.ended_at, ep.participants, ep.importance,
         1 - (ep.embedding_half <=> q.emb) AS similarity,
         (0.85 * (1 - (ep.embedding_half <=> q.emb))) +
         (0.15 * CASE WHEN q.tsq IS NOT NULL AND ep.search_vector @@ q.tsq
                       THEN LEAST(1.0, ts_rank_cd(ep.search_vector, q.tsq) * 4.0)
                       ELSE 0 END) AS score,
         ep.updated_at, ep.metadata
  FROM public.memory_episodes ep, q
  WHERE ep.status = 'active'
    AND ep.embedding IS NOT NULL
    AND (filter_namespace IS NULL OR ep.namespace = filter_namespace)
    AND (p_from IS NULL OR ep.occurred_at >= p_from)
    AND (p_to IS NULL OR ep.occurred_at <= p_to)
    AND (1 - (ep.embedding_half <=> q.emb)) >= min_similarity
  ORDER BY score DESC, similarity DESC, ep.importance DESC, ep.occurred_at DESC
  LIMIT LEAST(GREATEST(match_count, 1), 100);
$$;

CREATE OR REPLACE FUNCTION public.search_episodes_text(
    p_query text,
    p_namespace text DEFAULT NULL,
    p_from timestamptz DEFAULT NULL,
    p_to timestamptz DEFAULT NULL,
    p_limit integer DEFAULT 10
) RETURNS TABLE (
    id uuid,
    namespace text,
    session_id text,
    title text,
    summary text,
    occurred_at timestamptz,
    ended_at timestamptz,
    participants jsonb,
    importance smallint,
    updated_at timestamptz,
    metadata jsonb
)
LANGUAGE sql STABLE
AS $$
  SELECT ep.id, ep.namespace, ep.session_id, ep.title, ep.summary,
         ep.occurred_at, ep.ended_at, ep.participants, ep.importance,
         ep.updated_at, ep.metadata
  FROM public.memory_episodes ep
  WHERE ep.status = 'active'
    AND (p_namespace IS NULL OR ep.namespace = p_namespace)
    AND (p_from IS NULL OR ep.occurred_at >= p_from)
    AND (p_to IS NULL OR ep.occurred_at <= p_to)
    AND (ep.title ILIKE '%' || p_query || '%'
         OR ep.summary ILIKE '%' || p_query || '%'
         OR ep.session_id ILIKE '%' || p_query || '%')
  ORDER BY ep.importance DESC, ep.occurred_at DESC
  LIMIT LEAST(GREATEST(p_limit, 1), 100);
$$;

-- ------------------------------------------------------------
-- Bounded graph traversal
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.memory_traverse(
    p_start uuid,
    p_depth integer DEFAULT 2,
    p_namespace text DEFAULT NULL,
    p_predicate text DEFAULT NULL
) RETURNS TABLE (
    depth integer,
    from_id uuid,
    predicate text,
    to_id uuid,
    to_name text,
    to_type text,
    confidence real,
    path uuid[]
)
LANGUAGE sql STABLE
AS $$
  WITH RECURSIVE walk AS (
      SELECT 1 AS depth,
             r.from_entity_id AS from_id,
             r.predicate,
             r.to_entity_id AS to_id,
             r.confidence,
             ARRAY[r.from_entity_id, r.to_entity_id] AS path
      FROM public.memory_relations r
      WHERE r.status = 'active'
        AND r.from_entity_id = p_start
        AND (r.valid_to IS NULL OR r.valid_to > now())
        AND (p_namespace IS NULL OR r.namespace = p_namespace)
        AND (p_predicate IS NULL OR r.predicate = p_predicate)
      UNION ALL
      SELECT w.depth + 1,
             r.from_entity_id,
             r.predicate,
             r.to_entity_id,
             r.confidence,
             w.path || r.to_entity_id
      FROM walk w
      JOIN public.memory_relations r ON r.from_entity_id = w.to_id
      WHERE w.depth < LEAST(GREATEST(p_depth, 1), 5)
        AND r.status = 'active'
        AND (r.valid_to IS NULL OR r.valid_to > now())
        AND (p_namespace IS NULL OR r.namespace = p_namespace)
        AND (p_predicate IS NULL OR r.predicate = p_predicate)
        AND NOT (r.to_entity_id = ANY (w.path))
  )
  SELECT w.depth, w.from_id, w.predicate, w.to_id, e.name, e.entity_type,
         w.confidence, w.path
  FROM walk w
  JOIN public.memory_entities e ON e.id = w.to_id
  WHERE e.status = 'active'
  ORDER BY w.depth, e.name
  LIMIT 500;
$$;

-- ------------------------------------------------------------
-- Stats
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.memory_graph_stats(p_namespace text DEFAULT NULL)
RETURNS jsonb
LANGUAGE sql STABLE
AS $$
  SELECT jsonb_build_object(
      'entities', (SELECT count(*) FROM public.memory_entities
                    WHERE status = 'active'
                      AND (p_namespace IS NULL OR namespace = p_namespace)),
      'entities_total', (SELECT count(*) FROM public.memory_entities
                    WHERE p_namespace IS NULL OR namespace = p_namespace),
      'relations', (SELECT count(*) FROM public.memory_relations
                    WHERE status = 'active'
                      AND (p_namespace IS NULL OR namespace = p_namespace)),
      'episodes', (SELECT count(*) FROM public.memory_episodes
                    WHERE status = 'active'
                      AND (p_namespace IS NULL OR namespace = p_namespace)),
      'entity_mentions', (SELECT count(*) FROM public.memory_entity_mentions
                    WHERE p_namespace IS NULL OR namespace = p_namespace),
      'episode_items', (SELECT count(*) FROM public.memory_episode_items
                    WHERE p_namespace IS NULL OR EXISTS (
                        SELECT 1 FROM public.memory_episodes ep
                        WHERE ep.id = memory_episode_items.episode_id
                          AND (p_namespace IS NULL OR ep.namespace = p_namespace))),
      'entity_types', COALESCE((
          SELECT jsonb_object_agg(entity_type, n)
          FROM (
              SELECT entity_type, count(*) AS n
              FROM public.memory_entities
              WHERE status = 'active'
                AND (p_namespace IS NULL OR namespace = p_namespace)
              GROUP BY entity_type
              ORDER BY entity_type
          ) t
      ), '{}'::jsonb),
      'predicates', COALESCE((
          SELECT jsonb_object_agg(predicate, n)
          FROM (
              SELECT predicate, count(*) AS n
              FROM public.memory_relations
              WHERE status = 'active'
                AND (p_namespace IS NULL OR namespace = p_namespace)
              GROUP BY predicate
              ORDER BY predicate
          ) t
      ), '{}'::jsonb)
  );
$$;

-- ------------------------------------------------------------
-- Controlled deletion helpers (soft-forget; audit stays)
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.memory_forget_entity(p_id uuid, p_reason text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    e public.memory_entities%ROWTYPE;
    rels integer := 0;
BEGIN
    SELECT * INTO e FROM public.memory_entities WHERE id = p_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'error', 'entity not found');
    END IF;
    UPDATE public.memory_entities
       SET status = 'forgotten', embedding = NULL,
           metadata = metadata || jsonb_build_object('forget_reason', COALESCE(p_reason, ''))
     WHERE id = p_id;
    UPDATE public.memory_relations
       SET status = 'forgotten', updated_at = now(),
           metadata = metadata || jsonb_build_object('forget_reason', COALESCE(p_reason, ''))
     WHERE status = 'active'
       AND (from_entity_id = p_id OR to_entity_id = p_id);
    GET DIAGNOSTICS rels = ROW_COUNT;
    RETURN jsonb_build_object('ok', true, 'id', p_id, 'name', e.name,
                              'relations_forgotten', rels);
END
$$;

REVOKE ALL ON FUNCTION public.memory_forget_entity(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.memory_forget_entity(uuid, text) TO service_role;

-- ------------------------------------------------------------
-- Access control: same model as memory_items/memory_events.
-- ------------------------------------------------------------
ALTER TABLE public.memory_entities DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_relations DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_episodes DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_entity_mentions DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_episode_items DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_episode_entities DISABLE ROW LEVEL SECURITY;

GRANT ALL ON public.memory_entities TO service_role;
GRANT ALL ON public.memory_relations TO service_role;
GRANT ALL ON public.memory_episodes TO service_role;
GRANT ALL ON public.memory_entity_mentions TO service_role;
GRANT ALL ON public.memory_episode_items TO service_role;
GRANT ALL ON public.memory_episode_entities TO service_role;

REVOKE ALL ON public.memory_entities FROM anon, authenticated;
REVOKE ALL ON public.memory_relations FROM anon, authenticated;
REVOKE ALL ON public.memory_episodes FROM anon, authenticated;
REVOKE ALL ON public.memory_entity_mentions FROM anon, authenticated;
REVOKE ALL ON public.memory_episode_items FROM anon, authenticated;
REVOKE ALL ON public.memory_episode_entities FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
