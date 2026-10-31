# Validation

**Last run:** 2026-10-30 (Asia/Cairo) on the live host.
**Scope:** repository hygiene, syntax, workflow mirror, import plan, database
schema, memory and the memory graph, local services, the desktop pet, voice,
and publication safety.

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
| faster-whisper | 1.2.1 (ctranslate2 4.8.4, av 19.0.2) |
| Edge TTS | 7.3.1 |
| Desktop pet | Python 3.12, Tk 8.6, Pillow 13.0.0 |

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
| `013_memory_graph.sql` | `{"ok": true, "mode": "exec"}` |

All three are idempotent and were re-applied during this validation without
changing the memory counts. Live schema checks:

- 31 public tables, including `memory_items`, `memory_events`,
  `memory_entities`, `memory_relations`, `memory_episodes`, the three provenance
  tables, `knowledge_base`, `agent_registry`, `loops`, `reminders`, and `todos`.
- Functions present: `exec_sql`, `match_knowledge`, `match_memory`,
  `match_entities`, `match_episodes`, `search_entities_text`,
  `search_episodes_text`, `memory_traverse`, `memory_graph_stats`,
  `memory_forget_entity`, `memory_set_embeddings`, `search_memory_text`,
  `sync_legacy_memory`, `touch_memory`.
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

### Memory Graph v1

The pre-migration backup was written and verified before `013` was applied:

| Check | Result |
| --- | --- |
| Snapshot | `backups/memory/memory_20261030T020540Z.json` |
| Manifest | `memory_20261030T020540Z.manifest.json` |
| `memory_items` | 128 rows |
| `memory_events` | 128 rows |
| `othman_memory` / `othvm_memory` | 19 / 16 rows |
| Verification | File hashes and live row counts matched |

`memory.backfill_graph` was then run against the live database:

| Check | Result |
| --- | --- |
| Active entities | 90 (91 rows including one merged/forgotten) |
| Entity types | 15 tools, 65 workflows, 5 skills, 2 services, 2 components, 1 agent |
| Explicit relations | 1 (`uses_tool`) |
| Entity mention links | 45 |
| Active episodes | 0 |
| Rerun | Idempotent; 0 errors |

Live retrieval checks:

- `memory.graph_stats` returned the counts above plus `memory_items=128` and
  `memory_active=35`.
- `memory.recall_hybrid` returned ranked facts with similarity/score and the
  expected `provenance` object.
- A real agent request called the `memory` tool, reached the `WORKING` state from
  the actual tool record, returned `ok: true`, and replied with the exact stored
  value for `projects`.
- The graph schema, indexes, RPCs, and soft-delete functions were applied
  successfully; deletion is not exercised against live data because there was
  nothing safe to delete.

### Desktop pet, voice, and bridge

| Check | Result |
| --- | --- |
| Layered window transparency | Corner pixel delta `0.0` against the hidden desktop; center delta `105.3`; transparent pixels click-through; opaque pixels hit-testable |
| Window behavior | Always-on-top; animation frames changed (`28,199` pixels); `Ctrl+Alt+O` hides/shows |
| Dragging | Logged drag start/end matched the window delta after switching to `GetMessagePos()` |
| Real chat stream | `accepted → thread → thinking → assistant_text → response → done`; `BRIDGE_CHAT_OK` in 10.78 s |
| Real tool telemetry | A `computer` tool call produced `EXECUTING`, `ok: true`, and the real response `# OTHVM` in 27.42 s |
| Real memory call | `memory` tool call produced `WORKING`, `ok: true`, exact stored reply in 90.07 s |
| Panel transcript | Posted Tk messages appeared in the transcript; `events: connected`, `agent: ready · log: readable`, state `SPEAKING` |
| State after TTS | `done` while speaking no longer leaves `request_active`; `SUCCESS`/`IDLE` is applied after `speaking_end` |
| Real error path | NVIDIA timeout surfaced in the transcript; state went `ERROR → IDLE`; no fabricated success |
| Approval card | Synthetic approve/deny/informational paths passed and recorded the exact continuation payload |
| Edge TTS | Arabic MP3 `32,832` bytes; 16 kHz mono WAV `175,182` bytes; `speaking_end` engine `edge` |
| SAPI fallback | `speaking_end` engine `sapi`; English-only voices on this host |
| Recorder | Real microphone capture: `35,820` bytes, 1.12 s, 16 kHz mono |
| Local Whisper | Arabic transcript correct; language `ar`, probability `1.0`, decode `0.55–0.75 s` after load |
| Gemini → local fallback | Gemini timed out at 15 s; local returned in 20.72 s total with `engine: local` and `fallback_from` |
| Autostart | `not installed → installed → status → removed → not installed` |
| Wake word / auto-send | Disabled, missing phrase, phrase stripping, manual-send, and empty-transcript paths passed |
| Foreground/background dedup | Known session ignored; unrelated background animates; active foreground keeps `THINKING`; expired session stops suppressing |
| SSE reconnect | Controlled server: 2 attempts, 2 connected events, events from both connections; real restart showed exponential backoff |
| Approval resume | **Blocked**: the published agent has no approval-gated tools; changing its policy is out of scope |

`desktop_pet/artifacts/approval_card.png` is the real Tk panel captured with
`PrintWindow`; it shows the approval card, both action buttons, and the live
panel status line. Behavior was also verified through the real Tk widgets and
payload assertions.

### Services

| Check | Result |
| --- | --- |
| MCP service `:8765` | `ok: true`, role `service`, mode `full`, loops worker running |
| MCP desktop `:8766` | `ok: true`, role `desktop`, interactive session |
| Agent bridge | `/agent/status` returned `ok: true`, `agent_id`, `agent_configured: true`, `db_readable: true` |
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

- All tracked Markdown relative links were checked on 2026-10-30; none were
  broken.
- `docs/audits/AUDIT_2026-10-28.md` was folded into this page and removed.
- Component READMEs cover the MCP server, desktop pet, workflow mirror,
  FastPath, Android, Telegram Personal, and agent.
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
| Live approval resume from the pet | The published agent has no approval-gated tools; changing its policy is out of scope | Approval card and continuation payload verified synthetically with the real Tk widgets |
| Gemini STT availability | `gemini-3.8-flash` intermittently returns `503 UNAVAILABLE` or hangs; older models return `404 no longer available` | 15 s timeout and local Whisper fallback verified end-to-end |
| NVIDIA agent availability | `integrate.api.nvidia.com:443` intermittently times out at 10 s | The real error is surfaced; no success is fabricated |
| MCP restart from inside itself | `nssm restart` stops the service and the caller; starting it again requires elevation | Recovered with an elevated `Start-Service`; documented as an operational caveat |
| Android live control | `adb.devices=[]`; phone offline in Tailscale | `/health` and service status verified; endpoint contract reviewed |
| Telegram Personal live send | Not exercised to avoid sending a real message | Offline selftest passed; `/health` shows authorized |
| Google Contacts | People API/credential returns `Forbidden` | Workflow and credential structure reviewed; not claimed as working |
| Shopify | `SHOPIFY_STORE_DOMAIN` / `SHOPIFY_ADMIN_TOKEN` absent | Workflow is active but returns a configuration error; documented |
| Windows watchdog task install | Needs an elevated shell | Service health verified; `OTHVM-MCP-Desktop-Watchdog` exists, `OTHVM-MCP-Watchdog-5m` not confirmed |
| Scheduled end-to-end run | Would require waiting for the next schedule | Workflow definitions and triggers verified; not claimed as executed |
| Git history purge | Pass | Sanitized root plus feature commits; the previous root is unreachable from the branch; the MCP, Android, and command-webhook secrets are absent from every reachable revision |

## Publication state

The sanitized root snapshot is commit `6c0124f`
(`Initial clean OTHVM repository`) on `master` at
`https://github.com/othvm/OTHVM.git`. The follow-up commit adds the desktop pet,
voice layer, agent bridge, memory graph, and documentation.

The first pushed root (`5f5fb0d`) still contained two hardcoded credentials: the
Android agent API key in `android_agent/main.py` and the Telegram command
webhook secret in the workflow mirror. Both were removed from the tree, the
workflow sanitizer was tightened to catch underscore-bearing `othvm_` values,
and the root commit was amended before the feature push. The old root is
unreachable from the branch. The live values remain active and must be rotated;
see [`security.md`](security.md) for the incident procedure.

Before the follow-up push:

```powershell
git log --oneline --all
git status --short
$env:OTHVM_OPERATOR_NAME='<name>'
$env:OTHVM_OPERATOR_NAME_AR='<name>'
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py scan
```

The old root is unreachable from the branch, and the MCP, Android, and
command-webhook secrets are absent from every reachable revision. The live
Android and command-webhook values were not rotated because the Android service
restart requires elevation and the command webhook caller is not defined in this
repository; rotate both before treating them as safe. See `docs/security.md` for
the rotation and incident procedures.
