# Architecture

OTHVM is a single-operator personal AI operating system. Telegram is the
primary remote interface; a local desktop pet is a second *view* over the same
agent, not a second agent. n8n runs the agent and every tool workflow. Local
Windows services expose hardware control, and Supabase holds durable memory and
runtime state.

The live n8n instance and the live Supabase project are authoritative. The
repository is a sanitized, reproducible copy of their definitions.

## Design principles

- **One operator, one agent, multiple views.** A numeric Telegram user ID
  allowlist protects the bot; there is no multi-tenant logic. The desktop pet
  posts to the same published agent through the MCP bridge and reuses its
  tools, memory, and approval policy.
- **Workflow-as-tool.** Every agent capability is an n8n workflow invoked with
  `executeWorkflow`. Adding a tool means adding a workflow and a tool entry, not
  changing the agent runtime.
- **Local-first control.** The laptop and phone are controlled by loopback
  services. Nothing is exposed publicly except the Telegram webhook path.
- **Deterministic fast path.** Wake/alarm and desktop-file requests bypass the
  LLM through Caddy → FastPath, with n8n as the fallback.
- **Durable memory is separate from runtime state.** `memory_items` stores
  curated facts; queues, loops, reminders, and sessions live in runtime tables.
- **Secrets stay local.** Only `config.example.json` files are tracked. Live
  configs, sessions, tokens, and runtime state are ignored by git.

## Component map

```
Telegram                          Desktop pet (interactive session)
   │  HTTPS webhook                    │  text / push-to-talk voice
   ▼                                   ▼
ngrok / public tunnel ──► Caddy :5680 ──┬─► FastPath :5681   wake/alarm + desktop files
                                        └─► n8n :5678        everything else
                                                ▲
                                                │ MCP bridge /agent/chat
                                                │ (same published agent)
                                       OTHVM agent (n8n)
                                                │  15 workflow tools
        ┌───────────────┬───────────────┬───────┴────────┬────────────────┐
        ▼               ▼               ▼                ▼                ▼
   Supabase        MCP :8765       Android :8767   Telegram Personal   n8n sub-workflows
   memory/RAG      desktop :8766                   :8768               (68 workflows)
```

## Processes and services

| Component | Process | Port | Account | Start method |
| --- | --- | --- | --- | --- |
| n8n | `node .../n8n start` | 5678 | LocalSystem | NSSM service `n8n` |
| MCP service | `mcp_server/main.py` | 8765 | LocalSystem | NSSM service `OTHVM-MCP` |
| MCP desktop helper | `mcp_server/main.py --desktop` | 8766 | Interactive operator | `start_desktop.vbs` + desktop watchdog |
| Desktop pet | `desktop_pet/app.py` | — | Interactive operator | `pythonw.exe` / optional HKCU `Run` value |
| Android agent | `android_agent/main.py` | 8767 | LocalSystem | NSSM service `OTHVM-AndroidAgent` |
| Telegram Personal | `telegram_personal/main.py` | 8768 | LocalSystem | NSSM service `OTHVM-TelegramPersonal` |
| FastPath | `deploy/webhook_fastpath/main.py` | 5681 | LocalSystem | NSSM service `OTHVM-FastPath` |
| Caddy | `caddy run --config Caddyfile` | 5680 | LocalSystem | NSSM service `OTHVM-Caddy` |
| Public tunnel | ngrok or equivalent | — | LocalSystem | NSSM service `ngrok` (optional) |
| Guard | `guard/guard.js` | — | SYSTEM task | Task Scheduler, every 3 minutes |
| MCP watchdog | `deploy/scripts/ops/watchdog_mcp.ps1` | — | SYSTEM task | Task Scheduler, every 5 minutes |
| Weekly maintenance | `deploy/scripts/ops/maintenance.py` | — | SYSTEM task | Task Scheduler, Monday 04:00 |

The MCP service and the desktop helper are two roles of the same file. Windows
services run in session 0, which has no interactive desktop; GUI actions
(screenshots, input, windows, apps, Playwright) are forwarded from 8765 to 8766
and tagged `"via": "desktop"`.

## Data flow

### Chat request

1. Telegram sends an update to the public webhook.
2. Caddy matches the agent webhook path and forwards to FastPath.
3. FastPath handles a deterministic wake/alarm or desktop-file command itself;
   everything else is forwarded to n8n.
4. n8n routes the message to the OTHVM agent.
5. The agent calls workflow tools (`mail`, `calendar`, `computer`, `memory`, …).
6. Tool workflows call the MCP server, Google APIs, Supabase, or local services.
7. The agent replies through Telegram.

If FastPath is unavailable, Caddy's error handler sends the update to n8n, so
the agent still answers.

### Desktop pet chat and voice

1. The panel posts `{message, session_id?}` to `POST /agent/chat` on the MCP
   service, or a push-to-talk clip is transcribed locally/cloud-side first.
2. `mcp_server/agent_bridge.py` calls the n8n MCP `call_agent` tool for the
   published `OTHVM` agent. There is no second prompt, tool registry, or
   reasoning loop.
3. The bridge tails the live n8n `agents_messages` SQLite log for the session
   and emits only real records: `reasoning` → thinking, `tool-call` → a
   classified execution state, `text` → assistant text.
4. The request-scoped SSE stream returns `accepted`, `thread`, `thinking`,
   `tool`, `assistant_text`, `response`, `approval`, `done`, or `error`.
5. `GET /events` republishes chat events plus unrelated MCP tool activity.
   The pet deduplicates by `session_id`; a foreground request always wins over
   background animation. The client reconnects with exponential backoff.
6. TTS reads only the real `response` text. The state machine defers `SUCCESS`
   until speech ends, so `request_active` cannot remain stuck.
7. A suspended result carries the real n8n continuation; approve/deny resumes
   the *same* request through `/agent/chat` with `request_type: "approval"`.
   The pet never implements its own approval policy.

### Memory read

`memory` `{tool:'db', operation:'recall', query}` → MCP `/tools/db` with
`operation: memory.recall` → Gemini embedding → Supabase `match_memory` (vector)
or `search_memory_text` (keyword fallback). Results are returned with
`similarity` or `score`, and `touch_memory` updates access metadata.

`memory.recall_hybrid` runs the same fact retrieval plus entity and episode
retrieval, bounded graph traversal, and entity-mention provenance, then returns
one ranked `combined` list and an explicit `provenance` object. This is the
preferred read for questions that mix facts, projects, tools, and past events.

### Memory write

`remember` validates and slugs the key, checks for an existing
`(namespace, category, key)`, embeds the text, upserts `memory_items`, appends a
`memory_events` row, and dual-writes the legacy table. Updating the same key
increments `version`.

### Document retrieval

The agent's Supabase vector store uses `knowledge_base` and `match_knowledge`.
Ingestion workflows (`Notion Ingest`, `Drive Ingest`, `Gmail Ingest`, `GitHub
Ingest`) chunk documents and store `gemini-embedding-001` vectors. The MCP
`knowledge` tool exposes the same store directly.

## Storage

| Store | Contents | Authority |
| --- | --- | --- |
| Supabase Postgres | Unified memory, memory graph (entities/relations/episodes/provenance), events, knowledge base, runtime queues, todos, notes, loops, reminders, conversation summaries | Live project |
| n8n SQLite | Workflows, credentials, agent, chat threads, execution queue, checkpoints | Live instance |
| Workflow mirror | Sanitized JSON + manifest | Repository, regenerated from live |
| Local files | Configs, logs, browser profile, trash, exports | Host only, untracked |
| External backups | Supabase exports + n8n workflow exports | `OTHVM-backups` outside the repo |

## Trust boundaries

- **Public:** the Telegram webhook hostname only. Caddy binds to loopback and is
  reached through the tunnel.
- **Loopback:** n8n, MCP, FastPath, Android, and Telegram Personal bind to
  `127.0.0.1`. API keys are still required.
- **LocalSystem:** n8n, MCP service, FastPath, Caddy, Android, and Telegram
  Personal run with high privileges. `system_command` is effectively root.
- **Operator session:** the desktop helper, desktop pet, and browser profile run
  as the interactive user. The pet stores no secrets; it reads the MCP and
  Gemini keys from `mcp_server/config.json` or environment variables.
- **Supabase:** service-role key is held by the MCP server and n8n; RLS is not a
  boundary for these components.

See `docs/security.md` for the secret inventory and safety model.

## Repository layout

| Path | Purpose |
| --- | --- |
| `agent/` | Sanitized agent instructions, skills, and reference schema |
| `mcp_server/` | Windows control REST server, desktop helper, and agent bridge |
| `desktop_pet/` | Transparent desktop character, voice layer, and control panel |
| `android_agent/` | ADB/Tailscale bridge |
| `telegram_personal/` | Telethon bridge to a personal Telegram account |
| `guard/` | n8n queue/checkpoint/webhook hygiene task |
| `deploy/webhook_fastpath/` | Deterministic Telegram fast path + Caddy config |
| `deploy/workflows/` | Workflow mirror, sync/import tools, manifest |
| `deploy/scripts/ops/` | Watchdog and weekly maintenance |
| `deploy/remote_wake/` | Termux Wake-on-LAN bot |
| `supabase/` | SQL migrations and the `exec_sql` apply helper |
| `docs/` | Setup, configuration, workflows, memory, operations, validation, security |
