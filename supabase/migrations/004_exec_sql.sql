-- OTHVM — full SQL bridge for the agent.
-- public.exec_sql(query text) -> jsonb
--   * If the statement returns rows (SELECT / WITH / DML ... RETURNING), returns {ok, mode:'query', rows:[...], row_count:n}
--   * Otherwise (DDL / multi-statement), executes it and returns {ok, mode:'exec', rows:[], row_count:n}
-- SECURITY DEFINER so it can run DDL; EXECUTE is restricted to service_role only.

CREATE OR REPLACE FUNCTION public.exec_sql(query text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
  rows jsonb;
  affected bigint;
  q text;
BEGIN
  IF query IS NULL OR btrim(query) = '' THEN
    RAISE EXCEPTION 'empty query';
  END IF;

  q := btrim(query);
  WHILE right(q, 1) = ';' LOOP
    q := btrim(left(q, length(q) - 1));
  END LOOP;
  IF q = '' THEN
    RAISE EXCEPTION 'empty query';
  END IF;

  -- Try to run it as a row-returning statement first.
  BEGIN
    EXECUTE format('WITH _ AS (%s) SELECT coalesce(jsonb_agg(to_jsonb(_)), ''[]''::jsonb) FROM _', q)
      INTO rows;
    RETURN jsonb_build_object(
      'ok', true,
      'mode', 'query',
      'rows', rows,
      'row_count', jsonb_array_length(rows)
    );
  EXCEPTION
    WHEN syntax_error OR feature_not_supported THEN
      NULL; -- not a row-returning statement -> fall through to plain execution
  END;

  EXECUTE q;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RETURN jsonb_build_object(
    'ok', true,
    'mode', 'exec',
    'rows', '[]'::jsonb,
    'row_count', affected
  );
END;
$fn$;

REVOKE ALL ON FUNCTION public.exec_sql(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.exec_sql(text) FROM anon;
REVOKE ALL ON FUNCTION public.exec_sql(text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.exec_sql(text) TO service_role;

NOTIFY pgrst, 'reload schema';
