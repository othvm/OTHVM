# Validation

**Last run:** 2026-10-29 (Asia/Cairo) on the live host.
**Scope:** repository hygiene, syntax, workflow mirror, import plan, database
schema, memory, local services, and publication safety.

This page records what was actually executed. Checks that need a second machine,
extra hardware, or elevation are listed separately and are **not** claimed as
passing.

## Environment

| Component | Verified value |
| --- | --- |
| Windows | Windows 10/11 x64 |
| Python (MCP venv) | 3.12.13 |
| Node.js / npm | 24.21.0 / 12.0.2 |
| n8n | 2.44.7 |
| NSSM | 2.24 |
| Caddy | 2.11.7 |
| Supabase Postgres | hosted project with `vector` 3072-dim support |

Python dependencies are pinned in each `requirements.txt`; `pip check` reports no
broken requirements in the MCP and Telegram Personal venvs.

## Executed checks

### Repository hygiene

| Check | Method | Result |
| --- | --- | --- |
| Python syntax | `py_compile` on all service, tool, and maintenance modules | Pass |
| Guard syntax | `node --check guard/guard.js` | Pass |
| Workflow JSON | Parsed all 68 workflows, `manifest.json`, and six example configs | 75 files, 0 errors |
| Code nodes | Extracted 507 `jsCode` nodes and ran `node --check` inside an async wrapper | 0 failures |
| Duplicate tracked files | SHA-256 grouping over `git ls-files` | 0 duplicate groups |
| Empty tracked files | Size check | 0 |
| Tracked caches | `git ls-files` for `__pycache__` / `.pyc` | 0 |
| Tracked secrets | `git ls-files` for `config.json`, `.session`, `permission.json` | 0 |
| Secret patterns | `git grep` for Google/GitHub/Slack/OpenAI/JWT/private-key patterns | 0 |
| Personal data | `git grep` for emails, phone numbers, operator name | 0 outside legacy table names |
| Absolute paths | `git grep` for `C:\Users\...` | Only the `C:/Users/YOU/Desktop` placeholder |
| Tracked files | `git ls-files` | 145 |
| Git history | `git rev-list --all --count` | 1 root commit |
| Git objects | Scan every blob for the live MCP key | 145 blobs, 0 hits |

The operator name still appears only inside the legacy table names
`othman_memory` / `othvm_memory`, which are part of the live schema and are
required for compatibility. They are not personal data.

### Fast path

`deploy/webhook_fastpath/test_parsers.py`:

- 19 cases: Arabic and English wake commands, Arabic-Indic digits, relative and
  absolute times, filename extraction, quoted names, destructive commands, and
  missing-time behavior.
- Result: `FAILURES 0`.

### Telegram Personal

`telegram_personal/selftest.py` (offline, no Telegram login):

- 10 checks: rate-limit window, 11th-send block, old-send expiry, public health,
  API-key rejection, unknown action, send confirmation, missing credentials,
  and flood-wait pause.
- Result: `ALL CHECKS PASSED`.

### Workflow mirror

| Check | Command | Result |
| --- | --- | --- |
| Live vs local | `sync_workflows.py check` | `live=68 local=68 missing=0 extra=0 changed=0` |
| Secret scan | `sync_workflows.py scan` | `OK: no live secrets or secret patterns` |
| Manifest warnings | Inspect `manifest.json` | 0 |
| Dependency order | Inspect `manifest.json` | 0 violations, 68 entries in import order |
| Sub-workflow references | Resolve every `executeWorkflow` ID | 53 references, 0 missing |
| Node types | Inventory all node types | 31 types, all `n8n-nodes-base.*` or `@n8n/n8n-nodes-langchain.*`; 0 community nodes |
| Active state | Live API | 68 total, 67 active, 1 inactive (`Social Media Hub`) |

`check` and `scan` were run with `OTHVM_OPERATOR_NAME` and
`OTHVM_OPERATOR_NAME_AR` set to the operator's names, matching the environment
used for the last export. Names are not hardcoded in the sanitizer.

### Import plan

`import_workflows.py --dry-run` against the live instance:

- 68 selected, 67 active in the mirror.
- 30 credentials on the target; 21 credential types required.
- 0 missing credentials, 0 unresolved dependencies.
- Nothing written (`dry-run: nothing written`).

This proves the planner and dependency expansion against a populated instance.
It does **not** prove a fresh-instance import; see blocked checks.

### Database

`supabase/scripts/apply_sql.py` was run from the repository root:

| Migration | Result |
| --- | --- |
| `011_unified_memory.sql` | `{"ok": true, "mode": "exec"}` |
| `012_runtime_tables.sql` | `{"ok": true, "mode": "exec"}` |

Both are idempotent and were re-applied during this validation without changing
the memory counts. Live schema checks:

- 31 public tables, including `memory_items`, `memory_events`,
  `knowledge_base`, `agent_registry`, `loops`, `reminders`, and `todos`.
- Functions present: `exec_sql`, `match_knowledge`, `match_memory`,
  `memory_set_embeddings`, `search_memory_text`, `sync_legacy_memory`,
  `touch_memory`.
- Extensions present: `vector`, `pgcrypto`, `uuid-ossp`, `plpgsql`,
  `pg_stat_statements`, `supabase_vault`.

### Memory

| Check | Result |
| --- | --- |
| `memory.stats` | total 128, active 35, missing embeddings 0, events 128 |
| Active by namespace | personal 19, system 16 |
| Forgotten by namespace | personal 29, system 64 |
| `memory.consolidate` dry-run at 0.95 | 0 candidate pairs, 0 merged |
| Arabic semantic recall | Returns relevant identity/contact/social rows with similarity scores |
| Knowledge base | 6 chunks, all `notion` |
| Embedding model | `gemini-embedding-001`, 3072 dims; `memory_items` uses a `halfvec(3072)` HNSW index, `knowledge_base` uses ivfflat |

The recall test returned live personal values; they are intentionally not copied
into this document.

### Services

| Check | Result |
| --- | --- |
| MCP service `:8765` | `ok: true`, role `service`, mode `full`, loops worker running |
| MCP desktop `:8766` | `ok: true`, role `desktop`, interactive session |
| FastPath `:5681` | `ok: true`, service `othvm-telegram-fastpath` |
| n8n `:5678/healthz` | HTTP 200 |
| Android `:8767` | `ok: true`; `adb.devices=[]`, `connected=false` |
| Telegram Personal `:8768` | `ok: true`, authorized |
| Windows services | `n8n`, `ngrok`, `OTHVM-AndroidAgent`, `OTHVM-Caddy`, `OTHVM-FastPath`, `OTHVM-MCP`, `OTHVM-TelegramPersonal`, `Tailscale` all `Running` |

FastPath counters at validation showed 9 updates, 2 forwarded, 0 fast, 7 errors,
0 duplicates. The errors are failed forwards during earlier restarts; the
dedupe design records an update only after a successful forward, so those
updates remain retryable.

`Get-NetTCPConnection` did not list every loopback listener from this shell, but
`netstat -ano` showed active connections and every `/health` endpoint answered.
The health checks are the authoritative result.

### Documentation

- 22 Markdown files; every relative link resolves.
- `docs/audits/AUDIT_2026-10-28.md` was folded into this page and removed.
- Component READMEs cover the MCP server, workflow mirror, FastPath, Android,
  Telegram Personal, and agent.
- The main README is a short entry point with a documentation index.

## Historical fixes folded from the 2026-10-28 audit

These were verified live during the audit and remain part of the current
workflow definitions:

| Area | Fix |
| --- | --- |
| `OTHVM — Memory` | Added the `context` branch so `memory.context` reaches `/tools/context` instead of the unknown-tool response. |
| `OTHVM — Dev` | Added `Flatten Params` so both `{tool, params}` and flat `{operation}` payloads route correctly. |
| `Proactive Checks` | Mark/log reminders only after Telegram accepts the message, so a failed send does not lose the reminder. |
| `Email Brief` | Batched classification into one Gemini request with JSON schema, replacing up to 12 calls/day. |
| FastPath | Dedupe records `update_id` only after a successful forward or fast action; failed updates retry. |
| Permissions | DB, knowledge, memory, and workflow mutations enforce the mode model. |
| Sleep routing | One unambiguous `system.sleep` / `power.sleep` path. |

## Blocked or not executed

| Check | Why | What was done instead |
| --- | --- | --- |
| Fresh install on a second machine | No second Windows host or new n8n instance | Importer dry-run against the populated live instance; migrations re-applied idempotently |
| Fresh Supabase project restore | Would require a second project | Schema and functions verified on the live project; migrations are ordered and idempotent |
| Android live control | `adb.devices=[]`; phone offline in Tailscale | `/health` and service status verified; endpoint contract reviewed |
| Telegram Personal live send | Not exercised to avoid sending a real message | Offline selftest passed; `/health` shows authorized |
| Google Contacts | People API/credential returns `Forbidden` | Workflow and credential structure reviewed; not claimed as working |
| Shopify | `SHOPIFY_STORE_DOMAIN` / `SHOPIFY_ADMIN_TOKEN` absent | Workflow is active but returns a configuration error; documented |
| Windows watchdog task install | Needs an elevated shell | Service health verified; `OTHVM-MCP-Desktop-Watchdog` exists, `OTHVM-MCP-Watchdog-5m` not confirmed |
| Scheduled end-to-end run | Would require waiting for the next schedule | Workflow definitions and triggers verified; not claimed as executed |
| Git history purge | Pass | One root commit; the previous commit is unreachable; the live MCP key is absent from every reachable revision |

## Publication state

The repository history is a single clean root commit and is ready for
publication. The first snapshot contained a live MCP API key inside workflow
exports; that history was discarded rather than amended. Final verification:

```powershell
git log --oneline --all          # one commit
git rev-list --all --count       # 1
git status --short               # clean
$env:OTHVM_OPERATOR_NAME='<name>'
$env:OTHVM_OPERATOR_NAME_AR='<name>'
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py scan
```

The old commit object no longer exists, and the live MCP key is absent from every
reachable revision. Do not add a remote until these checks pass. See
`docs/security.md` for the rotation and incident procedures.
