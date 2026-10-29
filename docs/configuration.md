# Configuration

Every component reads a local `config.json` that is **not tracked**. The tracked
`config.example.json` next to it is the template. Copy the template, fill in the
values, and never commit the result.

```powershell
Copy-Item mcp_server\config.example.json mcp_server\config.json
```

Placeholders use the `REPLACE_WITH_*` convention. `deploy/workflows/sync_workflows.py`
uses the same strings when it sanitizes live workflows, so a sanitized export can
be compared with the templates.

## Configuration files

| File | Template | Used by | Tracked |
| --- | --- | --- | --- |
| `mcp_server/config.json` | `config.example.json` | MCP service and desktop helper | No |
| `deploy/webhook_fastpath/config.json` | `config.example.json` | FastPath | No |
| `android_agent/config.json` | `config.example.json` | Android agent | No |
| `telegram_personal/config.json` | `config.example.json` | Telegram Personal | No |
| `guard/config.json` | `config.example.json` | Guard task | No |
| `mcp_server/permission.json` | — | Current permission mode | No |
| `telegram_personal/<session_name>.session` | — | Telethon login | No |

## MCP server

`mcp_server/config.json` is shared by the service (`:8765`) and the desktop
helper (`:8766`). Both must use the same API key.

| Key | Required | Default | Purpose |
| --- | --- | --- | --- |
| `api_key` | Yes | — | `X-API-Key` for every tool call. Use a long random value. |
| `service_port` | No | `8765` | Service-role port. |
| `desktop_port` | No | `8766` | Desktop-helper port. |
| `telegram_bot_token` | Yes | — | Bot used for permission codes, notifications, and briefs. |
| `telegram_chat_id` | Yes | — | Operator chat ID for those messages. |
| `gemini_api_key` | Yes | — | Embeddings, vision, summaries. |
| `supabase_url` | Yes | — | `https://<project>.supabase.co`. |
| `supabase_service_key` | Yes | — | Service-role key. Never expose it to the browser. |
| `n8n_base` | Yes | `http://localhost:5678` | n8n base URL. |
| `n8n_api_key` | Yes | — | n8n public API key (workflow and execution operations). |
| `n8n_mcp_key` | No | — | n8n MCP token, if the MCP bridge is used. |
| `n8n_db_path` | Yes | SYSTEM profile path | Read-only n8n SQLite path for `context` and the guard. |
| `agent_id` | Yes | — | n8n agent ID, used to build thread IDs. |
| `main_telegram_thread_id` | Yes | — | Primary n8n thread ID. |
| `public_base_url` | Yes | — | Public HTTPS origin used in webhook replies. |
| `brief_telegram_credential_id` | No | — | Telegram credential ID used by generated briefs. |
| `summary_model` | No | `gemini-3.8-flash` | Conversation summary model. |
| `vision_model` | No | `summary_model` | Image analysis model. |
| `chrome_path` | No | `C:\Program Files\Google\Chrome\Application\chrome.exe` | Chrome for Playwright. |
| `exports_dir` | No | `<repo>/exports` | Default document export directory. Leave empty to use the default. |
| `max_command_timeout` | No | `300` | Maximum shell command seconds. |
| `max_record_seconds` | No | `300` | Maximum screen recording seconds. |

Generate the API key and the FastPath secret with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Telegram FastPath

`deploy/webhook_fastpath/config.json`:

| Key | Required | Purpose |
| --- | --- | --- |
| `host` | No | Bind address; keep `127.0.0.1`. |
| `port` | No | `5681`. |
| `mcp_base` | Yes | `http://127.0.0.1:8765`. |
| `mcp_api_key` | Yes | Same value as MCP `api_key`. |
| `telegram_bot_token` | Yes | Bot used to send the fast reply. |
| `telegram_secret` | Yes | `secret_token` configured on the Telegram webhook. |
| `allowed_user_id` | Yes | Numeric Telegram user ID allowlist. |
| `allowed_chat_id` | Yes | Chat that receives fast replies. |
| `n8n_base` | Yes | `http://127.0.0.1:5678` fallback target. |
| `desktop_dir` | No | Directory used by the desktop-file command. |
| `dedupe_seconds` | No | In-memory update dedupe window (default 600). |
| `log_path` | No | JSONL fast-path log. |

The bot token must be the same bot that owns the n8n Telegram webhook, because
Telegram allows one webhook per bot. FastPath validates `X-Telegram-Bot-Api-Secret-Token`
against `telegram_secret`.

## Android agent

`android_agent/config.json`:

| Key | Required | Purpose |
| --- | --- | --- |
| `tailscale_phone_ip` | No | Phone Tailscale IP; empty means auto-discover. |
| `local_phone_ip` | No | LAN fallback IP. |
| `adb_port` | No | Wireless ADB port (default 5555). |
| `server_port` | No | `8767`. |
| `api_key` | Yes | `X-API-Key` for device endpoints. |
| `adb_path` | Yes | `adb.exe` path. |
| `tailscale_path` | No | `tailscale.exe` path for peer discovery. |
| `clock_package` | No | Samsung Clock package for alarm intents. |
| `reconnect_interval_seconds` | No | Retry interval while disconnected. |
| `command_timeout_seconds` | No | Per-ADB-command timeout. |

## Telegram Personal

`telegram_personal/config.json`:

| Key | Required | Purpose |
| --- | --- | --- |
| `api_id`, `api_hash` | Yes | From <https://my.telegram.org>. |
| `phone` | Yes | Account phone number in international format. |
| `session_name` | No | Telethon session file base name. |
| `port` | No | `8768`. |
| `api_key` | Yes | `X-API-Key` for the local REST bridge. |
| `telegram_bot_token` | Yes | Bot used for flood-wait and safety notices. |
| `telegram_chat_id` | Yes | Operator chat for those notices. |
| `rate_limit_per_hour` | No | Outgoing message cap (default 10). |
| `max_file_mb` | No | Upload size cap (default 200). |
| `allowed_contacts` | No | Optional allowlist of contacts. |

Create the session once with `telegram_personal\login.py`. The `.session` file is
a full login and must never be copied into the repository.

## Guard

`guard/config.json`:

| Key | Required | Purpose |
| --- | --- | --- |
| `telegram_bot_token` | Yes | Bot used for webhook self-heal and notices. |
| `telegram_secret` | Yes | Webhook secret token. |
| `telegram_chat_id` | Yes | Chat notified only when the guard changes something. |
| `n8n_db_path` | Yes | n8n SQLite path, read/write for queue hygiene. |
| `thread_like` | Yes | SQL `LIKE` pattern for the agent's thread IDs, e.g. `AGENT_ID:%`. |

## Environment variables

Environment variables are optional overrides; they are useful for tools and
scheduled tasks that should not read the MCP config directly.

| Variable | Used by | Meaning |
| --- | --- | --- |
| `N8N_BASE_URL` | `sync_workflows.py`, `import_workflows.py` | Override `n8n_base`. |
| `N8N_API_KEY` | `sync_workflows.py`, `import_workflows.py`, `maintenance.py` | Override the n8n public API key. |
| `N8N_BASE` | `maintenance.py` | n8n base URL. |
| `OTHVM_BACKUP_DIR` | `maintenance.py` | Backup root; default `../OTHVM-backups`. |
| `OTHVM_OPERATOR_NAME` | `sync_workflows.py` | Operator's English name; redacted from exports when set. |
| `OTHVM_OPERATOR_NAME_AR` | `sync_workflows.py` | Operator's Arabic name; redacted from exports when set. |
| `NSSM_EXE` | Install scripts | Path to `nssm.exe`. |
| `CADDY_EXE` | FastPath installer | Path to `caddy.exe`. |
| `PYTHON_EXE` | Android/FastPath installers | Python interpreter. |
| `SHOPIFY_STORE_DOMAIN` | Shopify workflow | Store domain; the workflow returns a configuration error without it. |
| `SHOPIFY_ADMIN_TOKEN` | Shopify workflow | Admin API token. |
| `SHOPIFY_API_VERSION` | Shopify workflow | Optional API version pin. |

`SHOPIFY_*` variables must be visible to the n8n service process. Set them in
the NSSM service environment (`nssm set n8n AppEnvironmentExtra ...`) and restart
n8n. The Shopify workflow is active but returns an explicit "not configured"
error until they exist. Deactivate it if you do not plan to use Shopify.

## n8n service environment

The verified live service uses:

| Setting | Value |
| --- | --- |
| Application | `C:\Program Files\nodejs\node.exe` |
| Parameters | `"C:\Users\<you>\AppData\Roaming\npm\node_modules\n8n\bin\n8n" start` |
| AppDirectory | `C:\Users\<you>\AppData\Roaming\npm` |
| `N8N_USER_FOLDER` | `C:\Windows\System32\config\systemprofile` |
| `N8N_LOG_LEVEL` | `info` |
| Account | LocalSystem |

Using the SYSTEM profile means the database is at
`C:\Windows\System32\config\systemprofile\.n8n\database.sqlite`. The MCP
`n8n_db_path` and the guard must point there. If you run n8n as a normal user
instead, change all three paths together.

## Safety settings

- Keep `permission.json` at `read` until the install is verified. Switching to
  `write` or `full` requires a Telegram-delivered code.
- Keep Caddy and all local services bound to `127.0.0.1`.
- Do not put secrets in workflow JSON, Code nodes, or docs. Use n8n credentials
  or service environment variables.
- Run `deploy/workflows/sync_workflows.py scan` before every commit that touches
  the workflow mirror.

## Verifying configuration

```powershell
# JSON syntax
Get-ChildItem -Recurse -Filter config.json |
  Where-Object { $_.FullName -notmatch '\.venv' } |
  ForEach-Object { Get-Content $_.FullName -Raw | ConvertFrom-Json | Out-Null; "OK $($_.FullName)" }

# Local health
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://127.0.0.1:8766/health
Invoke-RestMethod http://127.0.0.1:5681/health
Invoke-WebRequest http://127.0.0.1:5678/healthz
```
