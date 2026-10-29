# Memory

OTHVM has two different retrieval stores and one audit trail:

| Store | Purpose | Authority |
| --- | --- | --- |
| `memory_items` | Curated, durable facts about the operator and the system | Unified memory v1 |
| `memory_events` | Append-only audit of writes, deletes, merges, restores | Audit |
| `knowledge_base` | Chunked documents for RAG (Notion, Gmail, Drive, GitHub) | Document store |

`core_context` and `conversation_summaries` provide session context; they are not
a substitute for memory.

## Unified memory v1

The schema is in `supabase/migrations/011_unified_memory.sql`.

```sql
memory_items(
  id uuid primary key,
  namespace text,          -- personal | system
  category text,
  key text,
  value text,
  importance smallint,     -- 1..5
  status text,             -- active | forgotten | superseded | archived
  version integer,
  source text,
  metadata jsonb,
  embedding vector(3072),
  embedding_half halfvec(3072) generated,
  search_vector tsvector generated,
  value_hash text generated,
  access_count integer,
  last_accessed_at timestamptz,
  superseded_by uuid,
  created_at timestamptz,
  updated_at timestamptz,
  unique (namespace, category, key)
)
```

- Embeddings use `gemini-embedding-001` at 3072 dimensions.
- `embedding_half` feeds an HNSW cosine index; `search_vector` feeds a GIN index.
- `value_hash = md5(value)` supports duplicate detection when no key is supplied.
- Only `status='active'` rows are retrievable.
- RLS is not enabled. The table is accessed only with the service-role key from
  the MCP server or n8n. Never expose the service key to a browser.

### Namespaces and categories

| Namespace | Categories | Use for |
| --- | --- | --- |
| `personal` | `personal_info`, `preferences`, `daily_patterns`, `personal_context` | Identity, contact, location, timezone, education, devices, preferences, routines, goals |
| `system` | `capabilities`, `workflows`, `connected_tools`, `operational_rules` | What OTHVM can do, which integrations work, operating rules, scheduled jobs |

Use a stable `category` + `key`. Updating the same key versions the row instead of
creating a duplicate. Keep values short and factual; project documentation
belongs in `docs/`, not in memory.

### Operations

Memory shares the MCP `db` route: call `POST /tools/db` with
`{"operation": "memory.<op>", ...}`. The shorthand `remember`, `recall`,
`forget`, `restore`, and `consolidate` are also accepted. The agent calls it as
`memory` `{tool:'db', params:{operation:'memory.<op>', ...}}`.

| Operation | Parameters | Behavior |
| --- | --- | --- |
| `remember` | `category`, `key?`, `value`, `importance?`, `namespace?`, `source?`, `metadata?`, `embed?` | Creates or updates; returns `changed`, `created`, `embedded`, `legacy_synced` |
| `recall` | `query` or `key`, `namespace?`, `category?`, `limit?`, `min_similarity?` | Vector search with keyword fallback; default limit 6, min similarity 0.35 |
| `forget` | `id` or `category`+`key`, `confirm:true` | Sets `status='forgotten'`, clears the embedding, writes an event |
| `restore` | `id` or `category`+`key` | Reactivates and re-embeds |
| `consolidate` | `dry_run?` (default true), `similarity_threshold?` (0.5–0.999, default 0.95), `namespace?`, `category?`, `prune?`, `prune_days?` | Merges near-duplicates; optional stale low-importance archive |
| `embed` | `limit?` (max 50) | Backfills active rows with `embedding IS NULL` |
| `stats` | — | Total, active, missing embeddings, event count, per-namespace breakdown |
| `history` | `id` or `category`+`key` | Last 20 events for one item |
| `list` | `namespace?`, `category?`, `status?`, `limit?` | Lists items without semantic search |

Write and destructive operations require the MCP permission mode `write` or
`full`. `forget` additionally requires `confirm:true`; `consolidate` requires
`read` only in dry-run mode.

### Retrieval

1. The query is embedded with `RETRIEVAL_QUERY`.
2. `match_memory` scores `0.85 * cosine similarity + 0.15 * text rank`.
3. Results below `min_similarity` are dropped.
4. If embedding fails or returns nothing, `search_memory_text` performs an
   `ILIKE` fallback and tags rows with `fallback: "keyword"`.
5. `touch_memory` increments `access_count` and `last_accessed_at`.

### Audit and provenance

Every create, update, forget, restore, consolidate, and archive writes a
`memory_events` row with old/new values, actor, reason, and metadata. Consolidation
stores the source rows in `metadata.consolidated_from`. This is how a merged fact
can be traced back to its originals.

### Legacy compatibility

The old tables remain:

| Namespace | Legacy table |
| --- | --- |
| `personal` | `othman_memory` |
| `system` | `othvm_memory` |

The MCP server dual-writes them on `remember`, `forget`, and `restore`. Triggers
in `011` mirror direct legacy writes back into `memory_items`. Do not write the
legacy tables directly; use the memory tool.

## Consolidation

The 2026-10-28 pass reduced active memory from 98 rows to 35 canonical rows:
19 personal and 16 system. The remaining rows cover identity/contact/location/
timezone, education, devices, language and communication preferences, calendar
and task rules, study schedule, current focus, projects, interests, connected and
unavailable integrations, capability domains, operating rules, scheduled
workflows, and knowledge-base status. Technical project notes were moved into
this repository.

Provenance for the merged rows is in `metadata.consolidated_from` with
`source=consolidation:2026-10-28`. The dry-run at 0.95 reports zero remaining
candidate pairs.

To repeat a consolidation:

```powershell
$cfg = Get-Content mcp_server\config.json -Raw | ConvertFrom-Json
$headers = @{ 'X-API-Key' = $cfg.api_key }
$body = @{ operation = 'memory.consolidate'; dry_run = $true; similarity_threshold = 0.95 } |
  ConvertTo-Json

# Dry run first
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/tools/db `
  -Headers $headers -ContentType 'application/json' -Body $body

# Apply only after reviewing the pairs
$body = @{ operation = 'memory.consolidate'; dry_run = $false; similarity_threshold = 0.95 } |
  ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/tools/db `
  -Headers $headers -ContentType 'application/json' -Body $body
```

After any bulk change, run `embed` until `remaining=0` and check `stats`.

## Knowledge base (RAG)

`knowledge_base` stores chunked documents with `source`, `source_id`, `title`,
`content`, `metadata`, and a 3072-dimension embedding plus a generated
`halfvec(3072)` ivfflat cosine index. The unique key is `(source, source_id)`, so
re-ingestion updates instead of duplicating.

| Operation | Purpose |
| --- | --- |
| `knowledge.search` | Vector search, returns the best excerpts with source and similarity |
| `knowledge.add` | Add one document and chunk it |
| `knowledge.ingest` | Add many items in one call |
| `knowledge.stats` | Chunk count by source |
| `knowledge.listSources` | Sources and chunk counts |
| `knowledge.get` | Read one chunk by ID |
| `knowledge.delete` | Delete by ID or source with `confirm:true` |

Chunking is 1200 characters with 200 overlap, breaking on newlines, sentences, or
spaces. Embeddings use `gemini-embedding-001`.

Ingestion workflows:

| Workflow | Source |
| --- | --- |
| `Notion Ingest` | Notion pages |
| `Drive Ingest` | Google Drive files |
| `Gmail Ingest` | Gmail messages |
| `GitHub Ingest` | Repository READMEs, issues, commits |

The agent's Supabase vector store uses `knowledge_base` and the
`match_knowledge` RPC. The MCP `knowledge` tool exposes the same store for direct
reads and writes.

At the 2026-10-28 snapshot the store held six useful Notion rows; a test row was
deleted.

## Conversation context

`context` reads n8n's SQLite database read-only and summarizes threads into
`conversation_summaries`:

| Operation | Purpose |
| --- | --- |
| `context.threads` | List recent threads |
| `context.latest` | Latest summary for a thread |
| `context.history` | Recent summaries |
| `context.summarize` | Summarize one thread or all eligible threads |
| `context.sync` | Sync thread metadata |
| `context.maintain` | Prune/refresh summaries |

The `Conversation Summarizer` workflow runs every 10 minutes and writes only when
there are at least 10 new messages. `context.latest` is injected into the morning
brief as the previous session.

## Backup and recovery

- `deploy/scripts/ops/maintenance.py` exports every public Supabase table
  (skipping vector columns) to `OTHVM_BACKUPS/<date>/supabase/`.
- Workflows are recoverable from `deploy/workflows/current/` and re-importable
  with `import_workflows.py`.
- Memory rows are recoverable from the Supabase export. Restore into a fresh
  project, then run `memory.embed` to rebuild embeddings.
- `memory_events` is append-only and should be exported with the rest of the
  database.
- If embeddings are lost, `memory.embed` regenerates them in batches; retrieval
  falls back to keyword search until the backfill completes.

## Hygiene rules

- One canonical row per fact. Reuse keys.
- Keep personal facts in `personal`; keep OTHVM rules and capability status in
  `system`.
- Do not store project documentation, code, logs, or transient chatter.
- Do not store secrets, tokens, passwords, or full chat transcripts.
- Use `metadata` for provenance, not for the fact itself.
- Run `stats` after changes and confirm `missing_embeddings=0`.
- Run `consolidate` as a dry-run before applying it.
