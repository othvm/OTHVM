# Workflows

n8n is the runtime. The repository mirrors every live workflow in
`deploy/workflows/current/` and indexes them in `deploy/workflows/manifest.json`.
The live instance is authoritative; the mirror is for review, versioning, and
disaster recovery.

## Current snapshot

| Metric | Value |
| --- | --- |
| Workflows | 68 |
| Active | 67 |
| Inactive | 1 (`Social Media Hub`) |
| Sub-workflows (`executeWorkflowTrigger`) | 56 |
| Schedule triggers | 11 (one workflow has four) |
| Webhook triggers | 6 |
| Missing workflows vs live | 0 |
| Content differences vs live | 0 |
| Dependency-order violations | 0 |
| Import warnings | 0 |

The inactive workflow is intentionally disabled until social platform
credentials are supplied.

## Files

| File | Purpose |
| --- | --- |
| `current/<id>_<slug>.json` | One sanitized workflow per file. Never edit by hand. |
| `manifest.json` | Generated index: IDs, names, active state, tags, triggers, webhooks, credentials, dependencies, import order, SHA-256. |
| `sync_workflows.py` | Export/sanitize live workflows, compare, rebuild the manifest, scan for leaks. |
| `import_workflows.py` | Dependency-first import with credential resolution and ID remapping. |

The mirror removes instance-only fields (`activeVersion`, `shared`, `pinData`,
`staticData`, `meta`, `parentFolderId`, `versionId`, `versionCounter`,
`triggerCount`, `nodeGroups`, `isArchived`) and replaces secrets and identifiers
with stable placeholders.

## Sync from live

```powershell
# Preview what would change
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py export --dry-run

# Rewrite current/ and manifest.json
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py export

# Compare live and local without writing
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py check

# Rebuild the manifest from current/
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py manifest

# Fail if any live secret or absolute path remains
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py scan
```

`export` prunes local files that no longer exist live. Use `--no-prune` to keep
them for review. `check` exits non-zero when live and local differ.

Configuration comes from `mcp_server/config.json`, or from `--base-url` /
`--api-key`, or from `N8N_BASE_URL` / `N8N_API_KEY`.

Sanitization replaces:

- credential IDs → `REPLACE_WITH_CREDENTIAL_ID` (credential names are kept);
- Telegram chat/user IDs → `REPLACE_WITH_TELEGRAM_CHAT_ID`;
- Telegram bot tokens → `REPLACE_WITH_TELEGRAM_BOT_TOKEN`;
- MCP API keys → `REPLACE_WITH_MCP_API_KEY`;
- Groq keys → `REPLACE_WITH_GROQ_API_KEY`;
- GitHub owner → `REPLACE_WITH_GITHUB_OWNER`;
- emails, Supabase URLs, ngrok hosts, JWTs, Google API keys, and tokens by
  pattern;
- absolute repository and user paths;
- the operator's name (English and Arabic).

The sanitizer reads live config files for exact-value redaction, so run it from a
checkout that has the local `config.json` files. A checkout without them still
redacts patterns, but exact-value coverage is lower.

## Import into a fresh n8n

```powershell
# 1. Plan only
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py --dry-run

# 2. Write a credential checklist
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py `
  --write-credential-template credentials.json

# 3. See what the target already has
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py --list-credentials

# 4. Fill credentials.json with target credential IDs, then import
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py `
  --apply --credential-map credentials.json --activate --report import_report.json

# 5. Re-run safely after a partial failure
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py `
  --apply --update-existing --credential-map credentials.json --activate
```

Useful options:

| Option | Effect |
| --- | --- |
| `--only <id-or-name>` | Import one workflow and all of its dependencies. Repeatable. |
| `--update-existing` | Update workflows matched by name instead of skipping. |
| `--activate` | Activate workflows that are active in the mirror. |
| `--allow-missing-credentials` | Import structurally even if a credential is absent. The workflow will fail at runtime. |
| `--no-remap` | Keep source `executeWorkflow` IDs. Only for inspection. |
| `--report <path>` | Write the import report. Default `deploy/workflows/import_report.json`. |

The import tool:

1. reads `manifest.json` and validates every file;
2. selects workflows and expands dependencies;
3. resolves credentials by `type::name`, then by unique name;
4. maps existing workflows by name;
5. validates every `executeWorkflow` reference before writing;
6. creates or updates in dependency order, remapping sub-workflow IDs;
7. optionally activates;
8. writes a report with created/updated/skipped/activated/failed IDs.

`credentials.json` and `import_report.json` are local state and are ignored by
git.

## Credential types and scopes

The mirror references 21 credential types in workflow nodes. `supabaseApi` is
additionally used by the agent's vector store, not by a workflow node, so it is
listed here for completeness. Scopes below are the minimum needed by the
workflow operations; n8n's OAuth consent screen may request a slightly wider set.
Reconnect a credential in n8n if a Google tool reports
`credential ... needs to be reconnected`.

| n8n type | Used by | Required access |
| --- | --- | --- |
| `gmailOAuth2` | Gmail, Email Brief, Gmail Ingest, Telegram Commands | Gmail modify, send, labels |
| `googleCalendarOAuth2Api` | Calendar, Morning Briefing, Proactive Checks, Telegram Commands | Calendar events read/write |
| `googleDriveOAuth2Api` | Drive, Drive Ingest, Document Reader | Drive read/write |
| `googleSheetsOAuth2Api` | Sheets, Files | Spreadsheets read/write |
| `googleSlidesOAuth2Api` | Slides, Files | Presentations read/write |
| `googleDocsOAuth2Api` | Google Docs, Files | Documents read/write |
| `googleTasksOAuth2Api` | Tasks, Telegram Commands | Tasks read/write |
| `googleContactsOAuth2Api` | Contacts, Telegram Commands | Contacts read/write; People API enabled |
| `youTubeOAuth2Api` | YouTube | YouTube read; write only if managing playlists/comments |
| `googleBooksOAuth2Api` | Google Books | Books read; bookshelf write if used |
| `googlePalmApi` | Gemini calls in several workflows | Generative Language API key |
| `githubOAuth2Api` | GitHub, GitHub Ingest | `repo`, `workflow`, `read:user` |
| `notionApi` | Notion, Notion Ingest | Integration token; target pages/databases shared with it |
| `linearApi` | Linear | Issues/projects read/write |
| `todoistApi` | Todoist | Tasks/projects read/write |
| `airtableTokenApi` | Airtable | `data.records:read`, `data.records:write`, schema read |
| `stripeApi` | Stripe | Restricted key with the resources used (balance, customers, charges, invoices, products, prices, refunds) |
| `nasaApi` | NASA | `api.nasa.gov` key |
| `discordBotApi` | Discord | Bot token, message content intent, channel permissions |
| `telegramApi` | Bot, commands, briefs, callbacks | Bot token from BotFather |
| `nvidiaApi` | Agent model | NVIDIA API key for the Nemotron endpoint |
| `supabaseApi` | Agent vector store | Project URL + service-role key |

`Groq` is not an n8n credential in the mirror: the voice workflow uses
`REPLACE_WITH_GROQ_API_KEY` directly in a Code/HTTP node. Put the real key in the
workflow on the target instance, or move it to an n8n credential and update the
node.

## Webhooks

| Workflow | Method | Path | Purpose |
| --- | --- | --- | --- |
| Loop Ack | GET | `loop-ack` | Telegram inline button for loop acknowledgment |
| Telegram Commands | POST | `othvm-commands` | Deterministic slash commands |
| Streak Callback Handler | POST | `othvm-streak-callback` | Streak buttons |
| Model Router | POST | `othvm-model-router` | Model selection/fallback |
| Google Health | POST | `othvm-google-health` | Google credential health check |
| Sleep Wake Monitor | GET | `sleep-monitor` | Sleep/wake state |

Webhook workflows must be **active** for n8n to register their production URL.
The agent's Telegram webhook is registered by the agent integration and is routed
through Caddy/FastPath; the six paths above are ordinary n8n webhooks.

## Activation requirements

- Import credentials before activation. A workflow with a missing credential can
  still be created, but it fails when it runs.
- Activate sub-workflows and tool workflows even though they have no schedule:
  the agent and callers expect stable, published definitions.
- Keep `Social Media Hub` inactive until its platform credentials exist.
- After activation, verify the webhook paths in **n8n → Workflow → Webhook** and
  send one test request.
- n8n tags are **UI-only** for this setup. The public API rejects `tags` on
  workflow create/update (`request/body/tags is read-only`). The manifest records
  tags read from live, but the import tool does not send them. Recreate tags in
  the UI if you need them.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `request/body/tags is read-only` | Public API does not accept tags | Remove `tags` from the body; set them in the UI |
| `PATCH method not allowed` | Workflow activation is not PATCH | Use `POST /api/v1/workflows/{id}/activate` or `/deactivate` |
| `Cannot delete a published workflow` | Workflow is still active | Deactivate, wait a few seconds, delete |
| `missing credentials` in dry-run | Credential name/type not on target | Create it, or add its ID to `credentials.json` |
| `unresolved sub-workflow references` | A dependency is not in the mirror or uses a dynamic expression | Import the dependency first; inspect `manifest.json` warnings |
| `check` reports `changed` | Live changed since the last export | Review, then run `export` |
| `scan` fails | A live value or absolute path remains | Fix the sanitizer pattern and re-export; never commit the file |
| Workflow imports but does nothing | It is inactive, or its credential is broken | Activate and inspect the execution |

## Safety

- Never run `import_workflows.py --apply` against production without a fresh
  backup and a dry-run.
- Never hand-edit `current/`. Change n8n, validate live, then export.
- Never put a token in a workflow to make an import pass. Use credentials.
- Keep the n8n encryption key and the Supabase service key out of the repository.
