# Fresh install

This guide rebuilds OTHVM on a new Windows machine with a new n8n instance and a
new Supabase project. The repository supplies code, migrations, workflows, and
the agent definition. Credentials and OAuth consent are created on the new
machine and are never copied from the old one.

## Verified environment

The current installation was built and checked with these versions:

| Component | Version |
| --- | --- |
| Windows | Windows 10/11 x64 |
| Python (MCP venv) | 3.12.13 |
| Node.js | 24.21.0 |
| npm | 12.0.2 |
| n8n | 2.44.7 |
| NSSM | 2.24 |
| Caddy | 2.11.7 |
| Supabase Postgres | hosted project with `pgvector` |
| Tailscale | current Windows client |
| Android platform-tools | current `adb` |
| Desktop pet | Pillow 13.0.0, sounddevice 0.5.6, Edge TTS 7.3.1 |
| Local STT | faster-whisper 1.2.1 (optional fallback) |

Use Python 3.12 for every venv. Newer versions may work, but they are not the
verified runtime.

## 1. Prerequisites

Install:

1. **Git** for Windows.
2. **Python 3.12** from python.org, including the `py` launcher.
3. **Node.js 24 LTS** (includes npm).
4. **NSSM** and **Caddy** (Chocolatey is the simplest: `choco install nssm caddy`).
5. Optional: **ngrok** or another HTTPS tunnel, **Tailscale**, and Android
   **platform-tools** for the phone.
6. Optional: a microphone and speakers for the desktop pet. The local Whisper
   base model needs about 70 MB of disk (larger models need more).
7. A **Supabase** project and an **n8n** installation (both have free tiers).
8. Accounts/API keys for the services you want: Google Cloud OAuth, Telegram
   BotFather, Gemini, NVIDIA, Groq, Notion, Linear, Todoist, Airtable, Stripe,
   NASA, Discord, GitHub.

Do not install paid services. Every component in this repository runs on free
tiers or local hardware.

## 2. Clone and create the Python environments

```powershell
git clone https://github.com/othvm/OTHVM.git D:\OTHVM
Set-Location D:\OTHVM

py -3.12 -m venv mcp_server\.venv
mcp_server\.venv\Scripts\python.exe -m pip install --upgrade pip
mcp_server\.venv\Scripts\python.exe -m pip install -r mcp_server\requirements.txt
mcp_server\.venv\Scripts\python.exe -m playwright install chromium
```

Telegram Personal needs its **own** virtualenv because it pins Telethon and a
different FastAPI version:

```powershell
py -3.12 -m venv telegram_personal\.venv
telegram_personal\.venv\Scripts\python.exe -m pip install --upgrade pip
telegram_personal\.venv\Scripts\python.exe -m pip install -r telegram_personal\requirements.txt
```

The Android agent and FastPath use the MCP venv by default; their installers
accept `PYTHON_EXE` if you prefer separate environments.

## 3. Create the Supabase schema

1. Create a Supabase project and copy the **project URL** and **service-role
   key**. Keep the service-role key server-side only.
2. Open **SQL Editor** and run, in order:
   - `supabase/migrations/001_create_tables.sql`
   - `supabase/migrations/003_briefs_streaks_expenses.sql`
   - `supabase/migrations/003b_briefs_add_content.sql`
   - `supabase/migrations/003c_streaks_last_reminder.sql`
   - `supabase/migrations/004_exec_sql.sql`
   - `supabase/migrations/004b_model_fallback_log.sql`

   `002_seed_data.sql` is optional scaffolding. It inserts placeholder personal
   facts; edit the placeholders first or skip it and let the agent create memory
   later.
3. Create `mcp_server/config.json` from the example and fill in
   `supabase_url` and `supabase_service_key` (other keys can be filled later).
4. Apply the remaining migrations through the `exec_sql` RPC:

```powershell
$files = @(
  '005_knowledge_base.sql',
  '006_knowledge_upsert.sql',
  '007_conversation_summaries.sql',
  '008_knowledge_official_vector.sql',
  '009_code_sessions.sql',
  '010_modes_builder_tables.sql',
  '011_unified_memory.sql',
  '012_runtime_tables.sql',
  '013_memory_graph.sql'
)
foreach ($f in $files) {
  mcp_server\.venv\Scripts\python.exe supabase\scripts\apply_sql.py "supabase\migrations\$f"
}
```

5. Verify:

```sql
select count(*) from public.memory_items;
select count(*) from public.knowledge_base;
select count(*) from public.memory_entities;
select count(*) from public.memory_relations;
select count(*) from public.memory_episodes;
select proname from pg_proc where proname in
  ('match_memory','search_memory_text','touch_memory','exec_sql',
   'match_entities','match_episodes','memory_traverse','memory_graph_stats');
```

`011` backfills the legacy tables into `memory_items`. On a fresh project those
tables are empty unless you ran `002`, so the result is normally zero rows.
`013` is additive and leaves `memory_items` untouched.

If you are upgrading an existing installation, run
`scripts\backup_memory.py` first and keep the JSON plus manifest until the graph
backfill is verified. See [`docs/memory.md`](memory.md#backup-and-recovery).

## 4. Install and start n8n

```powershell
npm install -g n8n@2.44.7
```

Start n8n once interactively to create the owner account:

```powershell
$env:N8N_USER_FOLDER = "$env:USERPROFILE\.n8n"
n8n start
```

Open <http://localhost:5678>, create the owner, then stop n8n. For a machine-wide
service, use the SYSTEM profile so the service can run without a logged-in user:

```powershell
$nssm = 'C:\ProgramData\chocolatey\lib\NSSM\tools\nssm.exe'
$node = 'C:\Program Files\nodejs\node.exe'
$n8n  = "$env:APPDATA\npm\node_modules\n8n\bin\n8n"

& $nssm install n8n $node "`"$n8n`" start"
& $nssm set n8n AppDirectory "$env:APPDATA\npm"
& $nssm set n8n ObjectName LocalSystem
& $nssm set n8n Start SERVICE_AUTO_START
& $nssm set n8n AppEnvironmentExtra `
    "N8N_USER_FOLDER=C:\Windows\System32\config\systemprofile" `
    "N8N_LOG_LEVEL=info" `
    "WEBHOOK_URL=https://YOUR_PUBLIC_HOST/"
& $nssm set n8n AppStdout "$env:ProgramData\n8n\service.out.log"
& $nssm set n8n AppStderr "$env:ProgramData\n8n\service.err.log"
& $nssm start n8n
```

Notes:

- `WEBHOOK_URL` must be the public HTTPS origin, not `localhost`, or Telegram
  cannot reach the webhook.
- If you run n8n as your own user instead of LocalSystem, use a user-writable
  `N8N_USER_FOLDER` and update `n8n_db_path` in the MCP and guard configs.
- n8n stores credentials with an encryption key in its user folder. Back up that
  folder; copying only the database is not enough.

Create a **public API key** in n8n: **Settings → API → Create API key**. Put it
in `mcp_server/config.json` as `n8n_api_key`.

## 5. Import the workflows

The mirror is sanitized, so every credential must exist on the target instance
before activation.

```powershell
# Inspect the plan; writes nothing
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py --dry-run

# Create an offline list of required credential slots
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py `
  --write-credential-template credentials.json

# List credentials already on the target
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py --list-credentials
```

Create the credentials in the n8n UI (or with the API) using the types and names
from `deploy/workflows/manifest.json`. Fill `credentials.json` with the target
credential IDs, then import dependency-first:

```powershell
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py `
  --apply --credential-map credentials.json --activate --report import_report.json
```

The tool:

- creates workflows in dependency order (sub-workflows before callers);
- remaps every `executeWorkflow` reference to the new workflow IDs;
- leaves workflows inactive unless `--activate` is passed;
- skips workflows whose names already exist unless `--update-existing` is passed;
- never prints secret values.

`credentials.json` and `import_report.json` are local state; do not commit them.
If a workflow fails halfway, fix the reported error and rerun with
`--update-existing`; created workflows are matched by name.

## 6. Configure the MCP server

```powershell
Copy-Item mcp_server\config.example.json mcp_server\config.json
notepad mcp_server\config.json
```

Fill at least: `api_key`, `telegram_bot_token`, `telegram_chat_id`,
`gemini_api_key`, `supabase_url`, `supabase_service_key`, `n8n_base`,
`n8n_api_key`, `n8n_db_path`, `public_base_url`. `agent_id` and
`main_telegram_thread_id` are filled after the agent exists.

Start the service role manually for the first test:

```powershell
mcp_server\.venv\Scripts\python.exe mcp_server\main.py
```

In a second terminal, start the desktop helper inside the interactive session:

```powershell
mcp_server\.venv\Scripts\python.exe mcp_server\main.py --desktop
```

Check both:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://127.0.0.1:8766/health
```

The default permission mode is `read`. Do not switch to `full` until the agent,
Telegram, and the permission-code flow are verified.

## 7. Create the agent

Follow `agent/README.md`. In short:

1. Add the NVIDIA model credential.
2. Create an agent named `OTHVM` with the model
   `nvidia/nvidia/nemotron-3-ultra-550b-a55b`.
3. Paste `agent/INSTRUCTIONS.md`.
4. Add the five skills from `agent/skills/`.
5. Add the 15 workflow tools, using `agent/agent.example.json` for names and
   descriptions and the imported workflow IDs.
6. Add the Supabase vector store and the Gemini embedding credential.
7. Enable observational memory with Gemini Flash Lite.
8. Connect the Telegram bot in private mode with the operator's numeric user ID.

After saving, copy the agent ID into `mcp_server/config.json` (`agent_id`) and
build `main_telegram_thread_id` in the format n8n shows for the agent thread.
Restart the MCP service after changing the config.

## 8. Telegram, Caddy, and FastPath

1. Create a bot with **@BotFather** and copy the token.
2. Copy the FastPath config and fill it in:

```powershell
Copy-Item deploy\webhook_fastpath\config.example.json deploy\webhook_fastpath\config.json
```

   Set `telegram_bot_token` to the same bot, `telegram_secret` to a random
   secret, and `allowed_user_id` / `allowed_chat_id` to the operator's numeric
   IDs. Use the same secret when configuring the Telegram webhook.
3. Start FastPath and Caddy manually:

```powershell
mcp_server\.venv\Scripts\python.exe deploy\webhook_fastpath\main.py
caddy run --config deploy\webhook_fastpath\Caddyfile --adapter caddyfile
```

4. Start the tunnel and point it at Caddy on port **5680**:

```powershell
ngrok http 5680
```

   Use a reserved/static domain if you want a stable webhook URL. Put the HTTPS
   origin in `mcp_server/config.json` as `public_base_url` and in n8n as
   `WEBHOOK_URL`.
5. Re-save the Telegram integration in the agent so n8n registers the public
   webhook. Telegram sends `X-Telegram-Bot-Api-Secret-Token`; FastPath rejects a
   mismatch.
6. Test `/start`, a normal question, and a wake command.

Caddy routes the agent webhook path to FastPath and everything else to n8n. If
FastPath returns 502/503/504, Caddy retries the same request against n8n.

## 9. Install the Windows services

Run an **elevated** PowerShell:

```powershell
mcp_server\install_service.cmd
powershell -ExecutionPolicy Bypass -File android_agent\install_service.ps1
telegram_personal\install_service.cmd
powershell -ExecutionPolicy Bypass -File deploy\webhook_fastpath\install_services.ps1
powershell -ExecutionPolicy Bypass -File deploy\scripts\ops\install_tasks.ps1
powershell -ExecutionPolicy Bypass -File guard\install_task.ps1
```

Start the desktop helper in the interactive session (double-click or run):

```powershell
wscript.exe mcp_server\start_desktop.vbs
```

The desktop helper cannot run as a Windows service because services cannot see
the interactive desktop. The `OTHVM-MCP-Desktop-Watchdog` task or the VBS
watchdog restarts it.

Service names:

| Service | Port |
| --- | --- |
| `n8n` | 5678 |
| `OTHVM-MCP` | 8765 |
| `OTHVM-AndroidAgent` | 8767 |
| `OTHVM-TelegramPersonal` | 8768 |
| `OTHVM-FastPath` | 5681 |
| `OTHVM-Caddy` | 5680 |

## 10. Desktop pet and voice (optional)

The pet is a local view over the existing agent. It does not create a second
agent, tool registry, or memory store. It uses the MCP bridge (`/agent/chat`),
which calls the published n8n agent and reads its real message log.

1. Install its Python dependencies into the existing MCP virtualenv:

```powershell
mcp_server\.venv\Scripts\python.exe -m pip install -r desktop_pet\requirements.txt
Copy-Item desktop_pet\config.example.json desktop_pet\config.json
```

2. Confirm `mcp_server\config.json` has `agent_id`, `n8n_mcp_key`, and a readable
   `n8n_db_path`. Then check:

```powershell
$cfg = Get-Content mcp_server\config.json -Raw | ConvertFrom-Json
Invoke-RestMethod http://127.0.0.1:8765/agent/status `
  -Headers @{ 'X-API-Key' = $cfg.api_key }
```

3. Start it in the **interactive desktop session**:

```powershell
mcp_server\.venv\Scripts\python.exe desktop_pet\app.py --debug
```

   Use `pythonw.exe` without `--debug` for normal use. The pet cannot render a
   layered window from session 0.

4. Optional: preload the offline STT model (about 70 MB, one time):

```powershell
mcp_server\.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8', download_root='desktop_pet/cache/whisper'); print('model ready')"
```

   Gemini STT is the primary backend; local Whisper is the fallback. Edge TTS is
   the primary Arabic voice, with Windows SAPI as the offline fallback.

5. Optional autostart for the current user:

```powershell
mcp_server\.venv\Scripts\python.exe desktop_pet\autostart.py install --hidden
```

`desktop_pet\config.json` is untracked and must contain no secrets. The pet reads
the MCP and Gemini keys from `mcp_server\config.json` or environment variables.
See [`desktop_pet/README.md`](../desktop_pet/README.md) for controls and
troubleshooting.

## 11. Android (optional)

1. Enable **Developer options → USB debugging**.
2. Connect USB once and run `adb tcpip 5555`, or use Android 11+ wireless
   debugging and `POST /device/pair`.
3. Put the phone on the same Tailscale tailnet as the laptop.
4. Fill `android_agent/config.json` and start `OTHVM-AndroidAgent`.
5. Check `GET http://127.0.0.1:8767/health` and confirm `connected=true` before
   claiming phone control works.

## 12. Telegram Personal (optional)

1. Create an app at <https://my.telegram.org> and copy `api_id` / `api_hash`.
2. Fill `telegram_personal/config.json`.
3. Run `telegram_personal\.venv\Scripts\python.exe telegram_personal\login.py`
   and complete the code/2FA prompt.
4. Start `OTHVM-TelegramPersonal` and check
   `GET http://127.0.0.1:8768/health`.

This component is optional and may be blocked on networks that cannot reach
Telegram MTProto. The bot path does not depend on it.

## 13. First-run checks

```powershell
# Ports
foreach ($p in 5678,5680,5681,8765,8766,8767,8768) {
  $c = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
  "{0}: {1}" -f $p, ($(if ($c) { "listening pid=$($c[0].OwningProcess)" } else { "not listening" }))
}

# Health
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://127.0.0.1:8766/health
Invoke-RestMethod http://127.0.0.1:5681/health
Invoke-WebRequest http://127.0.0.1:5678/healthz

# Agent bridge
$cfg = Get-Content mcp_server\config.json -Raw | ConvertFrom-Json
$headers = @{ 'X-API-Key' = $cfg.api_key }
Invoke-RestMethod http://127.0.0.1:8765/agent/status -Headers $headers

# Memory graph
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/tools/db `
  -Headers $headers -ContentType 'application/json' `
  -Body (@{ operation = 'memory.graph_stats' } | ConvertTo-Json)

# Workflow mirror vs live
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py check

# Secret scan
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py scan
```

Then send the bot a normal message, a memory query, and a wake command. A fresh
install is complete only when the agent replies, a tool call succeeds, and
`memory.stats` reports zero missing embeddings. If the pet is installed, also
send one panel message and confirm a real response; a healthy `/agent/status`
alone is not proof that the agent answered.

See `docs/validation.md` for the checks that were actually executed on this
repository and the ones that require hardware or a second machine.
