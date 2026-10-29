-- Make (source, source_id) a real unique constraint so PostgREST can upsert.
DROP INDEX IF EXISTS public.knowledge_base_source_id_uidx;
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_base_source_id_uidx
  ON public.knowledge_base (source, source_id);
