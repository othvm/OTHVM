-- OTHVM Phase 3 — long-term knowledge base (pgvector)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.knowledge_base (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  source text NOT NULL,
  source_id text,
  title text,
  content text NOT NULL,
  embedding vector(768),
  metadata jsonb,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

-- ivfflat (cosine) as requested
CREATE INDEX IF NOT EXISTS knowledge_base_embedding_idx
  ON public.knowledge_base USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS knowledge_base_source_idx
  ON public.knowledge_base (source);

-- one row per source document when source_id is provided (upsert target)
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_base_source_id_uidx
  ON public.knowledge_base (source, source_id) WHERE source_id IS NOT NULL;

ALTER TABLE public.knowledge_base ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS knowledge_service ON public.knowledge_base;
CREATE POLICY knowledge_service ON public.knowledge_base
  FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE OR REPLACE FUNCTION public.match_knowledge(
  query_embedding vector(768),
  match_count int DEFAULT 5,
  filter_source text DEFAULT NULL
) RETURNS TABLE (
  id uuid,
  source text,
  source_id text,
  title text,
  content text,
  metadata jsonb,
  similarity float
)
LANGUAGE sql STABLE
AS $$
  SELECT k.id, k.source, k.source_id, k.title, k.content, k.metadata,
         1 - (k.embedding <=> query_embedding) AS similarity
  FROM public.knowledge_base k
  WHERE k.embedding IS NOT NULL
    AND (filter_source IS NULL OR k.source = filter_source)
  ORDER BY k.embedding <=> query_embedding
  LIMIT match_count;
$$;

REVOKE EXECUTE ON FUNCTION public.match_knowledge(vector, int, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.match_knowledge(vector, int, text) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.match_knowledge(vector, int, text) TO service_role;

CREATE OR REPLACE FUNCTION public.knowledge_stats()
RETURNS TABLE (source text, chunks bigint, last_added timestamptz)
LANGUAGE sql STABLE
AS $$
  SELECT k.source, count(*)::bigint AS chunks, max(k.created_at) AS last_added
  FROM public.knowledge_base k
  GROUP BY k.source
  ORDER BY k.source;
$$;

REVOKE EXECUTE ON FUNCTION public.knowledge_stats() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.knowledge_stats() FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.knowledge_stats() TO service_role;

COMMENT ON TABLE public.knowledge_base IS 'OTHVM long-term memory: chunked documents with Gemini embeddings (768d).';
