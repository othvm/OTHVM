# Memory

OTHVM has one canonical fact store, a graph layer, a document store, and one
audit trail:

| Store | Purpose | Authority |
| --- | --- | --- |
| `memory_items` | Curated, durable facts about the operator and the system | Unified memory v1 |
| `memory_entities` / `memory_relations` / `memory_episodes` | Typed entities, explicit edges, and time-bounded events | Memory Graph v1 |
| `memory_entity_mentions` / `memory_episode_items` / `memory_episode_entities` | Provenance links between the graph and facts | Memory Graph v1 |
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

## Memory Graph v1

`supabase/migrations/013_memory_graph.sql` adds a graph layer without moving or
deleting any `memory_items` row. The canonical fact store remains
`memory_items`; the graph adds structure, time, and provenance.

| Table | Contents |
| --- | --- |
| `memory_entities` | Typed things: person, project, tool, workflow, skill, service, component, concept. `canonical_key` is a stable slug unique per namespace; `aliases` supports explicit mention matching. Status is `active`, `forgotten`, or `merged`. |
| `memory_relations` | Explicit directed edges (`from_entity_id`, `predicate`, `to_entity_id`) with confidence `0..1`, `valid_from`, `valid_to`, source, and status `active`, `forgotten`, or `expired`. Self-edges and duplicate edges are rejected. |
| `memory_episodes` | Time-bounded events or conversations with `session_id`, `title`, `summary`, `occurred_at`, `ended_at`, `participants`, importance, and status `active` or `forgotten`. |
| `memory_entity_mentions` | Which memory items explicitly mention an entity, with confidence and source. |
| `memory_episode_items` | Which memory items belong to an episode, with a role. |
| `memory_episode_entities` | Which entities participate in an episode, with a role. |

Entities and episodes use 3072-dimension Gemini embeddings with HNSW cosine
indexes and generated `tsvector` columns for keyword search. Edges are never
inferred during retrieval: only explicitly stored relations are traversed.
`valid_to IS NULL` means an edge is still valid; expired or forgotten edges stay
for audit but are not traversed by default.

### Graph operations

All operations use `POST /tools/db` with
`{"operation": "memory.<op>", ...}`. Write and destructive operations require the
MCP permission mode `write` or `full`; destructive operations additionally
require `confirm: true`.

| Operation | Key parameters | Behavior |
| --- | --- | --- |
| `entity_upsert` | `name`, `entity_type?`, `description?`, `aliases?`, `attributes?`, `importance?`, `namespace?`, `source?`, `embed?` | Creates or updates by `(namespace, canonical_key)`; returns `created`, `changed`, `embedded` |
| `entity_get` | `id` or `name`, `namespace?`, `include_graph?` | Returns the entity, its relations, and linked memory items |
| `entity_search` | `query` or no query, `namespace?`, `entity_type?`, `limit?`, `min_similarity?` | Vector search with keyword fallback; no query lists active entities |
| `entity_forget` | `id` or `name`, `confirm:true`, `reason?` | Soft-forgets the entity and its active relations; audit rows remain |
| `relation_upsert` | `from`, `to` (IDs or names), `predicate?`, `confidence?`, `valid_from?`, `valid_to?`, `source?` | Creates or updates one explicit edge |
| `relation_query` | `id`/`name?`, `direction?` (`in`/`out`/`both`), `predicate?`, `active_only?`, `limit?` | Lists edges, optionally for one entity |
| `relation_forget` | `id`, `confirm:true`, `reason?` | Soft-forgets one edge |
| `traverse` | `id` or `name`, `depth?` (1–5), `namespace?`, `predicate?` | Bounded graph traversal; returns edges, not synthesized prose |
| `episode_add` | `title?`, `summary`, `session_id?`, `occurred_at?`, `ended_at?`, `participants?`, `memory_ids?`, `entity_ids?`, `importance?` | Creates an episode and its provenance links |
| `episode_search` | `query` or no query, `namespace?`, `from?`, `to?`, `limit?`, `min_similarity?` | Vector/keyword search or a recent time window |
| `episode_forget` | `id`, `confirm:true`, `reason?` | Soft-forgets an episode and clears its embedding |
| `graph_stats` | `namespace?` | Counts active entities, episodes, relations, predicates, entity types, and mention links |
| `backfill_graph` | `dry_run?` (default true), `namespace?` (default `system`), `embed?` | Creates entities from the live capability inventory and explicit mention links |
| `recall_hybrid` | `query`, `namespace?`, `limit?`, `min_similarity?`, `graph_depth?`, `include_graph?`, `from?`, `to?` | Runs fact, entity, and episode retrieval plus bounded traversal |

### Hybrid retrieval

`memory.recall_hybrid` is the preferred read when a question mixes facts,
projects, tools, and past events. It returns:

- `memories` from `memory.recall` (vector with keyword fallback);
- `entities` from `match_entities` / `search_entities_text`;
- `episodes` from `match_episodes` / `search_episodes_text`;
- `linked_memories` reached through explicit entity mentions;
- `relations` reached by bounded traversal (depth capped at 5) and direct edges;
- `combined`, a single ranked list tagged with `kind` (`memory`, `entity`,
  `episode`, `linked_memory`);
- `provenance`, listing the retrievers and every memory, entity, and episode ID
  used.

Entity and episode vector scoring follows the same shape as fact retrieval:
`0.85 * cosine similarity + 0.15 * text rank`. If an embedding call fails, the
response includes `entity_embed_error` or `episode_embed_error` and the
corresponding keyword fallback is tagged; the operation does not pretend the
semantic path succeeded.

### Backfill and current state

The 2026-10-30 backfill ran against the live database with `dry_run=false`:

| Metric | Value |
| --- | --- |
| Active entities | 90 |
| Entity rows including merged/forgotten | 91 |
| Entity types | 15 tools, 65 workflows, 5 skills, 2 services, 2 components, 1 agent |
| Explicit relations | 1 (`uses_tool`) |
| Entity mention links | 45 |
| Episodes | 0 |
| Errors on rerun | 0 |

The backfill is idempotent: it matches existing entities by canonical key and
uses `resolution=ignore-duplicates` for mention links. Run the dry-run first:

```powershell
$cfg = Get-Content mcp_server\config.json -Raw | ConvertFrom-Json
$headers = @{ 'X-API-Key' = $cfg.api_key }
$body = @{ operation = 'memory.backfill_graph'; dry_run = $true } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/tools/db `
  -Headers $headers -ContentType 'application/json' -Body $body
```

Then repeat with `dry_run = $false` only after reviewing the counts.

### Deletion and temporal validity

Deletion is soft and audited. `entity_forget` calls the database RPC, marks the
entity `forgotten`, soft-forgets its active relations, and leaves mention and
audit rows intact. `relation_forget` and `episode_forget` mark only that row
`forgotten`. `memory_items` deletion continues to use `memory.forget`, which
clears the embedding and writes a `memory_events` row. Graph statistics exclude
forgotten entities and episodes, so deleted knowledge disappears from retrieval
without destroying the evidence that it existed.

Entity, relation, and episode deletion is not reversible through the MCP API.
Restore from a verified Supabase export if a deletion was a mistake. The
`valid_from` / `valid_to` fields let a relation expire without deleting its
history; `relation_query` filters to active edges by default.

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

- `scripts/backup_memory.py` is the pre-migration snapshot tool. It is read-only
  against Supabase, writes a timestamped JSON file plus SHA-256 manifest under
  `backups/memory/` (git-ignored), re-reads the file, and verifies every row
  count and hash against the live tables.
- The 2026-10-30 snapshot before `013` was verified: 128 `memory_items`, 128
  `memory_events`, 19 `othman_memory`, and 16 `othvm_memory` rows. See
  `backups/memory/memory_20261030T020540Z.json` and its `.manifest.json` on the
  host.
- `deploy/scripts/ops/maintenance.py` exports every public Supabase table
  (skipping vector columns) to `OTHVM_BACKUPS/<date>/supabase/`. That export is
  the one that includes the graph and provenance tables.
- Workflows are recoverable from `deploy/workflows/current/` and re-importable
  with `import_workflows.py`.
- Restore memory rows into a fresh project, then run `memory.embed` to rebuild
  embeddings. Restore graph rows only after `011` and `013` are applied; rerun
  `memory.backfill_graph` to rebuild explicit mention links if a graph export is
  incomplete.
- `memory_events` is append-only and should be exported with the rest of the
  database.
- If embeddings are lost, `memory.embed` regenerates fact embeddings in batches.
  Graph embeddings are rebuilt by the graph backfill; verify with
  `graph_stats` and a keyword/semantic `recall_hybrid` query. Retrieval falls
  back to keyword search until the backfill completes.

## Hygiene rules

- One canonical row per fact. Reuse keys.
- Keep personal facts in `personal`; keep OTHVM rules and capability status in
  `system`.
- Do not store project documentation, code, logs, or transient chatter.
- Do not store secrets, tokens, passwords, or full chat transcripts.
- Use `metadata` for provenance, not for the fact itself.
- Keep graph edges explicit. Do not create a relation unless the source states
  it; retrieval must never invent one.
- Use stable entity names and aliases; `entity_upsert` canonicalizes the key and
  updates instead of duplicating.
- Use `valid_from` / `valid_to` for relationships that change over time instead
  of deleting history.
- Run `stats` after changes and confirm `missing_embeddings=0`; run
  `graph_stats` and confirm the expected active entity/relation counts.
- Run `consolidate` and `backfill_graph` as dry-runs before applying them.
