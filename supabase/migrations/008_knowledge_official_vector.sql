-- OTHVM — migrate the knowledge base to the official Agent "Knowledge" vector store.
-- The official n8n vector store embeds queries with google/gemini-embedding-001 at its
-- default size (3072). pgvector cannot build an ivfflat index above 2000 dimensions,
-- so we keep the full vector(3072) column and index a halfvec(3072) mirror (limit 4000).

CREATE EXTENSION IF NOT EXISTS vector;

DROP INDEX IF EXISTS public.knowledge_base_embedding_idx;
DROP FUNCTION IF EXISTS public.match_knowledge(vector, int, text);

-- Embeddings will be regenerated at 3072 dimensions by deploy/reembed_knowledge_3072.js.
ALTER TABLE public.knowledge_base
  ALTER COLUMN embedding TYPE vector(3072) USING NULL;

ALTER TABLE public.knowledge_base DROP COLUMN IF EXISTS embedding_half;
ALTER TABLE public.knowledge_base
  ADD COLUMN embedding_half halfvec(3072)
  GENERATED ALWAYS AS (embedding::halfvec(3072)) STORED;

CREATE INDEX IF NOT EXISTS knowledge_base_embedding_half_idx
  ON public.knowledge_base USING ivfflat (embedding_half halfvec_cosine_ops)
  WITH (lists = 100);

CREATE OR REPLACE FUNCTION public.match_knowledge(
  query_embedding vector(3072),
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
         1 - (k.embedding_half <=> query_embedding::halfvec(3072)) AS similarity
  FROM public.knowledge_base k
  WHERE k.embedding IS NOT NULL
    AND (filter_source IS NULL OR k.source = filter_source)
  ORDER BY k.embedding_half <=> query_embedding::halfvec(3072)
  LIMIT match_count;
$$;

REVOKE EXECUTE ON FUNCTION public.match_knowledge(vector, int, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.match_knowledge(vector, int, text) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.match_knowledge(vector, int, text) TO service_role;

COMMENT ON TABLE public.knowledge_base IS
  'OTHVM long-term memory: chunked documents with Gemini gemini-embedding-001 (3072d), indexed via halfvec for the official Agent Knowledge vector store.';

NOTIFY pgrst, 'reload schema';
