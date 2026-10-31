# OTHVM — Laptop Control MCP Server

REST tool server that gives the OTHVM agent control over the operator's Windows laptop.

**Base URL:** `http://127.0.0.1:8765`
**Auth:** `X-API-Key: <api_key from config.json>` on every tool call.

Copy `config.example.json` to `config.json` and fill in the values before the
first run. The live `config.json` is untracked.

---

## Architecture (important)

Windows services run in **session 0**, which has no desktop. Screenshots,
mouse/keyboard, window control, app launching and Playwright would all fail there.
So the server runs in two roles from the same file:

| Role | Port | Session | Runs as | Purpose |
|------|------|---------|---------|---------|
| **service** (`main.py`) | 8765 | 0 | LocalSystem (NSSM `OTHVM-MCP`) | files, processes, shell, system, permission, persistence; proxies GUI actions |
| **desktop** (`main.py --desktop`) | 8766 | 1 (interactive) | founder | screenshots, input, windows, apps, browser |

The service transparently forwards GUI-bound actions to `127.0.0.1:8766`
and tags the response with `"via": "desktop"`.

Autostart for the desktop helper:
- `start_desktop.vbs` runs the helper hidden in the interactive user session.
- `watchdog_desktop.vbs` is the silent entry point used by the
  `OTHVM-MCP-Desktop-Watchdog` scheduled task; it calls `watchdog_desktop.ps1`,
  which restarts the helper if port 8766 is down.
- `watchdog_desktop.cmd` is the same check with a visible console, for manual
  use.

---

## Agent bridge (desktop pet)

`agent_bridge.py` lets the desktop pet talk to the **existing** published n8n
agent. It does not contain a prompt, tool registry, or second reasoning loop.

| Endpoint | Purpose |
| --- | --- |
| `GET /agent/status` | Whether `agent_id`, `n8n_mcp_key`, and `n8n_db_path` are configured/readable |
| `POST /agent/chat` | Request-scoped SSE stream: `{message, session_id?}` or `{request_type:"approval", continuation, approved}` |
| `GET /events` | Server-wide SSE stream of real MCP tool activity and chat events |

The bridge:

1. calls n8n MCP `call_agent` with the configured `agent_id`;
2. discovers the draft-chat thread and tails n8n's `agents_messages` SQLite log
   read-only;
3. converts only real `reasoning`, `tool-call`, and `text` records into events;
4. classifies tool names into execution states for animation;
5. passes a suspended result's real continuation back through the same
   `call_agent` path for approve/deny.

Chat events are republished on `/events`, so clients must deduplicate by
`session_id`. The pet ignores background activity while a foreground request is
active. No message text or tool payload is written to the bridge log.

Required config: `agent_id`, `n8n_mcp_key`, `n8n_db_path`, and `n8n_base`.
`n8n_mcp_key` is a separate n8n MCP token, not the public API key. The service
normally runs as LocalSystem and can read the SYSTEM-profile n8n database; the
interactive user may not be able to.

---

## Permissions

`permission.json` holds the current mode:

| Mode | Allows |
|------|--------|
| `none` | nothing |
| `read` | read files, list files/windows/processes, screenshots, page extraction, system info |
| `write` | read + write files, mouse/keyboard, open/close apps, browser actions, sleep |
| `full` | everything: PowerShell, shutdown/restart, delete files/folders, execute JS |

**Changing the mode requires founder approval:** calling
`POST /tools/permission {"action":"set_mode","mode":"full"}` sends a 6-digit code
to the founder on Telegram. The agent must ask for the code and call again with
`"token":"123456"`. Codes expire in 5 minutes; max 3 pending.

Default mode: `read`.

---

## Endpoints

All calls are `POST /tools/<category>` with `{"action": "...", ...params}`.

### `/tools/system`
`screenshot`, `screen_record` (`duration`), `get_processes` (`name`, `limit`),
`kill_process` (`pid` | `name`), `system_command` (`command`, `timeout`),
`shutdown` (`delay`), `restart` (`delay`), `sleep`, `get_system_info`.

### `/tools/files`
`file_read`, `file_write`, `file_append`, `file_delete`, `file_move`,
`file_copy`, `file_list`, `file_search` (`name` | `content`), `folder_create`,
`folder_delete`.

### `/tools/apps`
`open_application`, `close_application`, `focus_window`, `list_windows`,
`minimize_window`, `maximize_window`.

### `/tools/input`
`keyboard_type` (Unicode/Arabic via clipboard), `keyboard_shortcut` (`ctrl+c`),
`keyboard_press`, `mouse_click`, `mouse_move`, `mouse_scroll`.

### `/tools/browser` (Playwright + Chrome, persistent profile)
`browser_open`, `browser_navigate`, `browser_click`, `browser_type`,
`browser_extract`, `browser_screenshot`, `browser_execute_js`, `browser_scroll`,
`browser_close`, `browser_get_url`, `browser_get_title`.

### `/tools/permission`
`get_mode`, `set_mode`.

### `/tools/knowledge` (RAG — not gated by the laptop permission mode)
`search` (`query`, `limit`=5, `filter_source`), `add` (`source`, `content`, `source_id`, `title`, `metadata`), `ingest` (`source`, `items[]`), `stats`,
`listSources`, `get` (`id`), `delete` (✋ `id` | `source` + `confirm:true`).

- Storage: Supabase Postgres + pgvector, table `knowledge_base` (`vector(3072)`, generated `halfvec(3072)` ivfflat cosine index).
- Embeddings: `gemini-embedding-001` (3072 dims, RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY, batches of 50).
- Chunking: 1200 chars, 200 overlap, breaks on newline/sentence/space.
- Sources: `notion`, `gmail`, `drive`, `github_readme`, `github_issues`, `github_commits`.
- Ingestion workflows: `Notion Ingest`, `Gmail Ingest`, `Drive Ingest`, `GitHub Ingest`.
- Upsert key: `(source, source_id)` via `resolution=merge-duplicates`.

### `/tools/db` (SQL + unified memory + graph)
Database actions: `read`, `write`, `delete`, `query`, `execute`, `count`,
`schema.*`, `context.*`, `admin.*`. Memory actions use the same route with
`operation: memory.<op>`:

- facts: `stats`, `remember`, `recall`, `forget`, `restore`, `consolidate`,
  `embed`, `history`, `list`;
- graph: `entity_upsert`, `entity_get`, `entity_search`, `entity_forget`,
  `relation_upsert`, `relation_query`, `relation_forget`, `traverse`,
  `episode_add`, `episode_search`, `episode_forget`, `graph_stats`,
  `backfill_graph`;
- hybrid: `recall_hybrid`.

The shorthand `remember`, `recall`, `forget`, `restore`, and `consolidate` also
work. Destructive graph operations require `confirm:true`. See
`docs/memory.md` for the schema, retrieval, and deletion rules.

### `/tools/workflows` and `/tools/loops`
`workflows` wraps the n8n public API (`list`, `get`, `create`, `update`,
`delete`, `activate`, `deactivate`, `executions`). `loops` manages the durable
loop queue (`list`, `get`, `create`, `update`, `tick`, `cancel`, `stats`).

### `/tools/reminders`, `/tools/android`, `/tools/commands`, `/tools/sleep`
`reminders` manages one-shot reminders; `android` proxies the Android agent;
`commands` runs the deterministic slash-command dispatcher; `sleep` records
sleep/wake state.

### `/tools/code_session`, `/tools/vision`, `/tools/doc`
Long-running code sessions, image analysis through Gemini, and document
extraction/export. These are asynchronous actions on the generic endpoint.

### `/tools/context` (conversation memory)
`threads` (`limit`), `latest` (`thread_id`), `history` (`thread_id`, `limit`), `summarize` (`thread_id` | `all`, `force`, `min_new`=10, `max_messages`=40).

- Reads n8n's live SQLite DB read-only (`n8n_db_path`) for `agents_messages` / `agents_threads`.
- Summarizes with `summary_model` (Gemini Flash, JSON output, Arabic) and stores rows in Supabase `conversation_summaries`.
- Rolling watermark per thread (`last_message_at`); the first summary covers the most recent 40 text messages.
- Schedule: `OTHVM — Conversation Summarizer` runs every 10 minutes and only writes when ≥10 new messages.
- `context.latest` is injected into the Morning Brief as **🧠 آخر جلسة:**.

### Other
`GET /health` (no auth), `GET /permission` (auth), `GET /agent/status` (auth),
`POST /agent/chat` (auth, SSE), `GET /events` (auth, SSE).

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | the whole server (both roles) plus `/agent/*` and `/events` |
| `agent_bridge.py` | n8n MCP `call_agent` bridge and real telemetry reader |
| `config.example.json` | sanitized configuration template |
| `config.json` | local API key, ports, Telegram, Gemini, Supabase (untracked) |
| `permission.json` | current mode |
| `pending_permission.json` | temporary confirmation codes |
| `install_service.cmd` | NSSM service installer |
| `start_desktop.vbs` | hidden desktop-helper launcher |
| `watchdog_desktop.*` | desktop-helper watchdog |
| `temp/` | screenshots, recordings |
| `logs/actions.log` | JSONL audit log of every tool call |
| `browser_profile/` | persistent Chrome profile (untracked) |

---

## Operations

```powershell
# service (installed by NSSM)
nssm start OTHVM-MCP
nssm stop OTHVM-MCP
nssm restart OTHVM-MCP

# desktop helper
wscript.exe .\start_desktop.vbs

# health
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8766/health

# agent bridge (use the real key)
curl -H "X-API-Key: <api_key>" http://127.0.0.1:8765/agent/status
```

Logs: `logs/service.out.log`, `logs/service.err.log`, `logs/desktop.out.log`,
`logs/desktop.err.log`, `logs/actions.log`. The desktop pet writes its own
`desktop_pet/logs/pet.log`.

---

## Security notes

- Binds to `127.0.0.1` only (n8n runs on the same machine).
- Every request needs the API key.
- Destructive operations are gated by the permission mode, and mode changes need
  a Telegram-delivered code that only the founder sees.
- `system_command` runs with the full privileges of the caller (SYSTEM in service
  role) — treat it as root.
- `/agent/chat` uses `n8n_mcp_key` and can invoke the same tools as Telegram. It
  is protected by the MCP API key; do not expose port 8765 beyond loopback.
- The bridge reads n8n's database read-only. It never writes agent memory or
  fabricates an event; a failed n8n call is returned as an error.
