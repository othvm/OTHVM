# Workflow mirror

`current/` is the canonical sanitized snapshot of the live n8n instance. The
live instance is authoritative; this directory is for review, versioning, and
disaster recovery. Regenerate it with `sync_workflows.py` instead of editing it.

The full operational guide is [`docs/workflows.md`](../../docs/workflows.md).

## Contents

| File | Purpose |
| --- | --- |
| `current/<id>_<slug>.json` | One sanitized workflow per file. Never edit by hand. |
| `manifest.json` | Generated index: IDs, names, active state, tags, triggers, webhook paths, credential types, dependencies, import order, SHA-256. |
| `sync_workflows.py` | Export/sanitize live workflows, compare, rebuild the manifest, scan for leaks. |
| `import_workflows.py` | Dependency-first import with credential resolution and ID remapping. |

Current snapshot: **68** workflows, **67** active, **1** inactive
(`Social Media Hub`, disabled until its platform credentials exist).

## Sync from live

```powershell
# Preview
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py export --dry-run

# Rewrite current/ and manifest.json
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py export

# Compare without writing; non-zero exit when live and local differ
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py check

# Rebuild the manifest from current/
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py manifest

# Fail on live secrets or absolute paths
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py scan
```

Configuration is read from `mcp_server/config.json`, or from `--base-url` /
`--api-key`, or from `N8N_BASE_URL` / `N8N_API_KEY`.

## Sanitization

The exporter removes instance-only fields (`activeVersion`, `shared`, `pinData`,
`staticData`, `meta`, `parentFolderId`, `versionId`, `versionCounter`,
`triggerCount`, `nodeGroups`, `isArchived`) and replaces:

| Live value | Placeholder |
| --- | --- |
| Credential ID | `REPLACE_WITH_CREDENTIAL_ID` (credential name is kept) |
| Telegram chat/user ID | `REPLACE_WITH_TELEGRAM_CHAT_ID` |
| Telegram bot token | `REPLACE_WITH_TELEGRAM_BOT_TOKEN` |
| MCP API key | `REPLACE_WITH_MCP_API_KEY` |
| Groq key | `REPLACE_WITH_GROQ_API_KEY` |
| GitHub owner | `REPLACE_WITH_GITHUB_OWNER` |
| Email, Supabase URL, ngrok host, JWT, Google API key, generic token | Pattern-based placeholder |
| Absolute repository/user path | Relative path or placeholder |
| Operator name (English and Arabic) | `المستخدم` / operator-neutral text |

The sanitizer reads the local `config.json` files for exact-value redaction, so
run it from a checkout that has them. A checkout without them still redacts
patterns, but exact-value coverage is lower.

Operator names are not hardcoded. Set `OTHVM_OPERATOR_NAME` and
`OTHVM_OPERATOR_NAME_AR` before `export` and `scan` so the operator's English and
Arabic names are replaced with `Operator` / `المستخدم`. The same environment
must be used for both commands, otherwise `scan` cannot check for those names.

## Import into n8n

The importer is dependency-first and dry-run by default.

```powershell
# 1. Plan only
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py --dry-run

# 2. Write a credential checklist
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py `
  --write-credential-template credentials.json

# 3. List credentials already on the target
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py --list-credentials

# 4. Fill credentials.json with target credential IDs, then import
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py `
  --apply --credential-map credentials.json --activate --report import_report.json

# 5. Re-run safely after a partial failure
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py `
  --apply --update-existing --credential-map credentials.json --activate
```

| Option | Effect |
| --- | --- |
| `--only <id-or-name>` | Import one workflow and all of its dependencies. Repeatable. |
| `--update-existing` | Update workflows matched by name instead of skipping. |
| `--activate` | Activate workflows that are active in the mirror. |
| `--allow-missing-credentials` | Import structurally even if a credential is absent. The workflow will fail at runtime. |
| `--no-remap` | Keep source `executeWorkflow` IDs. Inspection only. |
| `--report <path>` | Write the import report (default `deploy/workflows/import_report.json`). |

The importer reads the manifest, expands dependencies, resolves credentials by
`type::name` and then by unique name, validates every `executeWorkflow`
reference, creates or updates in dependency order, remaps sub-workflow IDs, and
optionally activates. `credentials.json` and `import_report.json` are ignored by
git.

## Activation and webhooks

- Import credentials before activation; a workflow with a missing credential can
  be created but fails at runtime.
- Activate sub-workflows and tool workflows even without a schedule; callers
  expect published definitions.
- Webhook workflows must be active to register their production URL. The paths
  are listed in `manifest.json` and `docs/workflows.md`.
- n8n tags are UI-only here: the public API rejects `tags` on workflow
  create/update (`request/body/tags is read-only`). The manifest records live
  tags, but the importer does not send them.
- Use `POST /api/v1/workflows/{id}/activate|deactivate`; `PATCH` is not allowed.
  Deactivate a workflow before deleting it.

## Safety

- Never run `import_workflows.py --apply` against production without a fresh
  backup and a dry-run.
- Never put a token in a workflow to make an import pass. Use n8n credentials or
  service environment variables.
- Never commit `credentials.json`, `import_report.json`, or a live export.
- Keep the n8n encryption key and the Supabase service key out of the repository.
