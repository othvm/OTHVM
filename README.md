# OTHVM

A self-hosted personal AI operating system for one operator. A single Telegram
chat drives an n8n AI agent that delegates work to n8n tool workflows and to
local Windows services controlling a laptop and an Android phone. Durable memory
and document retrieval live in Supabase.

The live n8n instance and Supabase project are authoritative. This repository is
their sanitized, reproducible definition: code, schema migrations, workflow
mirror, agent instructions, and operating docs.

## What it does

- Telegram is the only interface. The agent replies in Arabic or English and
  keeps conversational context.
- 15 agent tools cover mail, calendar, files, tasks, notes, contacts, phone,
  computer control, system info, automation, memory, development, finance,
  Telegram, and vision.
- 68 n8n workflows implement those tools, scheduled briefs, reminders, streaks,
  loops, and ingestion jobs.
- A deterministic fast path handles wake/alarm and desktop-file commands without
  calling the LLM.
- Unified memory stores curated facts with embeddings, an audit trail, and
  keyword fallback; a separate knowledge base stores chunked documents for RAG.
- Everything runs on local hardware plus free APIs. No paid service is required.

## Architecture

```
Telegram
   │  HTTPS webhook
   ▼
public tunnel ──► Caddy :5680 ──┬─► FastPath :5681   wake/alarm + desktop files
                                └─► n8n :5678        everything else
                                        │
                                        ▼
                                 OTHVM agent (n8n)
                                        │  15 workflow tools
      ┌───────────────┬─────────────────┼──────────────────┬──────────────┐
      ▼               ▼                 ▼                  ▼              ▼
   Supabase        MCP :8765        Android :8767    Telegram Personal   n8n
   memory/RAG      desktop :8766                     :8768               sub-workflows
```

| Component | Role | Port |
| --- | --- | --- |
| `mcp_server/` | Windows control REST server; desktop helper handles GUI actions | 8765 / 8766 |
| `android_agent/` | ADB/Tailscale bridge for the phone | 8767 |
| `telegram_personal/` | Telethon bridge to a personal Telegram account | 8768 |
| `deploy/webhook_fastpath/` | Deterministic Telegram path plus Caddy router | 5681 / 5680 |
| `deploy/workflows/` | Sanitized workflow mirror, sync and import tools, manifest | — |
| `guard/` | n8n webhook, queue, and checkpoint hygiene | — |
| `deploy/scripts/ops/` | Watchdogs, weekly maintenance, task installers | — |
| `deploy/remote_wake/` | Optional Termux Wake-on-LAN bot | — |
| `supabase/migrations/` | Schema for memory, knowledge, runtime tables | — |
| `agent/` | Agent instructions, five skills, sanitized definition | — |

## Requirements

- Windows 10/11 x64.
- Python 3.12.
- Node.js 24 and n8n 2.44.7.
- NSSM and Caddy (Chocolatey is simplest).
- A Supabase project with `pgvector`.
- A Telegram bot and the operator's numeric user ID.
- Optional: ngrok or another HTTPS tunnel, Tailscale, Android platform-tools.

Verified versions and the full install sequence are in
[`docs/setup.md`](docs/setup.md).

## Quick start

```powershell
git clone <your-repo-url> D:\OTHVM
Set-Location D:\OTHVM

# Python environments
py -3.12 -m venv mcp_server\.venv
mcp_server\.venv\Scripts\python.exe -m pip install -r mcp_server\requirements.txt
mcp_server\.venv\Scripts\python.exe -m playwright install chromium
py -3.12 -m venv telegram_personal\.venv
telegram_personal\.venv\Scripts\python.exe -m pip install -r telegram_personal\requirements.txt

# Local configuration (never commit these)
Copy-Item mcp_server\config.example.json mcp_server\config.json
Copy-Item deploy\webhook_fastpath\config.example.json deploy\webhook_fastpath\config.json
Copy-Item android_agent\config.example.json android_agent\config.json
Copy-Item telegram_personal\config.example.json telegram_personal\config.json
Copy-Item guard\config.example.json guard\config.json
```

Then:

1. Apply the Supabase migrations in order; see
   [`docs/setup.md`](docs/setup.md#3-create-the-supabase-schema).
2. Import the workflows with `deploy/workflows/import_workflows.py`; see
   [`docs/workflows.md`](docs/workflows.md#import-into-a-fresh-n8n).
3. Create the agent from [`agent/README.md`](agent/README.md).
4. Install and start the services; see
   [`docs/operations.md`](docs/operations.md).
5. Run the first-run checks in [`docs/setup.md`](docs/setup.md#12-first-run-checks).

A fresh install is complete only when the agent replies on Telegram, a tool call
succeeds, and `memory.stats` reports zero missing embeddings.

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/setup.md`](docs/setup.md) | Fresh install on a new machine and new n8n instance |
| [`docs/configuration.md`](docs/configuration.md) | Every config file and environment variable |
| [`docs/architecture.md`](docs/architecture.md) | Components, data flow, trust boundaries, storage |
| [`docs/workflows.md`](docs/workflows.md) | Workflow mirror, import order, credentials, webhooks |
| [`docs/memory.md`](docs/memory.md) | Unified memory, retrieval, consolidation, knowledge base |
| [`docs/operations.md`](docs/operations.md) | Services, health checks, logs, backups, incidents |
| [`docs/security.md`](docs/security.md) | Secret inventory, threat model, rotation, publication |
| [`docs/validation.md`](docs/validation.md) | What was actually executed and what is blocked |

Component-level notes live next to the code: `mcp_server/README.md`,
`deploy/workflows/README.md`, `deploy/webhook_fastpath/README.md`,
`android_agent/README.md`, `telegram_personal/README.md`, and `agent/README.md`.

## Security model

- Services bind to `127.0.0.1`. Only the Telegram webhook hostname is public,
  through a tunnel.
- Every tool call requires an API key. The MCP server also enforces a permission
  mode (`none`, `read`, `write`, `full`); changing it requires a six-digit code
  delivered on Telegram.
- n8n, MCP, FastPath, Caddy, and the Android service run as LocalSystem on
  Windows. `system_command` is effectively root; treat it accordingly.
- Live configs, Telethon sessions, logs, browser profiles, and runtime state are
  untracked. Only `config.example.json` templates are committed.
- Workflow exports are sanitized: credential IDs, chat IDs, bot tokens, API
  keys, emails, absolute paths, and the operator's name are replaced with
  placeholders.
- `sync_workflows.py scan` must pass before committing workflow changes.

See [`docs/security.md`](docs/security.md) for the full inventory and rotation
procedure.

## Status

Verified on the current host:

- 68 workflows mirrored, 67 active, 0 differences from live.
- All `executeWorkflow` references resolve; no community nodes.
- MCP, desktop helper, FastPath, and n8n health checks pass.
- Memory consolidated to 35 active rows with 0 missing embeddings.
- Agent definition and five skills extracted and sanitized.

Blocked or unverified:

- Android live control (no ADB device attached).
- Telegram Personal live send (service is authorized; no message was sent during
  validation).
- Google Contacts (People API/credential needs reconnecting).
- Shopify (store domain and admin token not configured).
- Windows service repair/watchdog installation (needs an elevated shell).
- Fresh install on a second machine (the importer was tested in dry-run only).

Details and evidence are in [`docs/validation.md`](docs/validation.md).

## Backups

`deploy/scripts/ops/maintenance.py` prunes n8n executions, exports every public
Supabase table, and consolidates memory. The default backup root is
`..\OTHVM-backups`, outside the repository, configurable with
`OTHVM_BACKUP_DIR`. The n8n SQLite database alone is not a complete backup:
credentials are encrypted with a key stored in the n8n user folder.

## License

No license has been declared. Treat this repository as private unless a license
is added.
