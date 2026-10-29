# Operations

This is the runbook for the Windows host. All commands are PowerShell unless
noted. Run service and task changes from an **elevated** shell.

## Service control

```powershell
# Status
Get-Service n8n, OTHVM-MCP, OTHVM-AndroidAgent, OTHVM-TelegramPersonal,
            OTHVM-FastPath, OTHVM-Caddy | Format-Table Name,Status,StartType

# Start / stop / restart
Start-Service OTHVM-MCP
Stop-Service OTHVM-MCP
Restart-Service OTHVM-MCP
```

With NSSM:

```powershell
$nssm = 'C:\ProgramData\chocolatey\lib\NSSM\tools\nssm.exe'
& $nssm status OTHVM-MCP
& $nssm restart OTHVM-MCP
& $nssm get OTHVM-MCP AppParameters
```

The desktop helper is not a service. Start it in the interactive session:

```powershell
wscript.exe <repo>\mcp_server\start_desktop.vbs
```

If port 8766 is down, the `OTHVM-MCP-Desktop-Watchdog` task relaunches it.

## Scheduled tasks

| Task | Schedule | Purpose |
| --- | --- | --- |
| `OTHVM Guard` | Every 3 minutes | Telegram webhook self-heal, queue dedupe, stale checkpoint expiry |
| `OTHVM-MCP-Watchdog-5m` | Every 5 minutes | Restart `OTHVM-MCP` if `/health` fails or heartbeat is stale |
| `OTHVM-Weekly-Maintenance` | Monday 04:00 | Prune n8n executions, export Supabase, consolidate memory |
| `OTHVM-MCP-Desktop-Watchdog` | Repeating | Relaunch the desktop helper if 8766 is down |

Only `OTHVM-MCP-Desktop-Watchdog` was visible from a non-elevated shell during
validation. The other tasks are created by `install_tasks.ps1` and
`guard/install_task.ps1`; confirm them with an elevated
`Get-ScheduledTask -TaskName 'OTHVM*'`.

```powershell
Get-ScheduledTask -TaskName 'OTHVM*' | Format-Table TaskName,State
Start-ScheduledTask -TaskName 'OTHVM Guard'
Get-ScheduledTaskInfo -TaskName 'OTHVM Guard'
```

Install the reliability tasks with:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\scripts\ops\install_tasks.ps1
powershell -ExecutionPolicy Bypass -File guard\install_task.ps1
```

## Health checks

| Check | Command | Expected |
| --- | --- | --- |
| MCP service | `Invoke-RestMethod http://127.0.0.1:8765/health` | `ok: true`, `role: service`, `mode` |
| MCP desktop | `Invoke-RestMethod http://127.0.0.1:8766/health` | `ok: true`, `role: desktop` |
| FastPath | `Invoke-RestMethod http://127.0.0.1:5681/health` | `ok: true`, counters |
| n8n | `Invoke-WebRequest http://127.0.0.1:5678/healthz` | HTTP 200 |
| Caddy | `Invoke-WebRequest http://127.0.0.1:5680/healthz` | Proxied n8n response |
| Android | `Invoke-RestMethod http://127.0.0.1:8767/health` | `connected` matches reality |
| Telegram Personal | `Invoke-RestMethod http://127.0.0.1:8768/health` | `ok: true` when running |
| Public webhook | Telegram `getWebhookInfo` | HTTPS URL, no `last_error_message` |

Port check:

```powershell
foreach ($p in 5678,5680,5681,8765,8766,8767,8768) {
  $c = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
  "{0}: {1}" -f $p, ($(if ($c) { "listening pid=$($c[0].OwningProcess)" } else { "not listening" }))
}
```

## Logs

| Component | Path |
| --- | --- |
| MCP service | `mcp_server/logs/service.out.log`, `service.err.log` |
| MCP desktop | `mcp_server/logs/desktop.out.log`, `desktop.err.log` |
| MCP audit | `mcp_server/logs/actions.log` (JSONL) |
| MCP watchdog | `mcp_server/logs/watchdog.log` |
| FastPath | `deploy/webhook_fastpath/fastpath.log`, `service.*.log` |
| Caddy | `deploy/webhook_fastpath/caddy_access.log`, `caddy.*.log` |
| Guard | `guard/guard.log`, `guard/guard.out.log` |
| Maintenance | `mcp_server/logs/maintenance.log` |
| n8n | NSSM stdout/stderr and n8n's own logs under its user folder |
| Android | `android_agent/logs/service.*.log` |
| Telegram Personal | `telegram_personal/logs/service.*.log` |

Logs rotate at the sizes configured in the install scripts. They are untracked.

## Daily checks

1. All expected services are `Running`.
2. `/health` on 8765, 8766, 5681, and n8n is OK.
3. Telegram answers a normal message.
4. `memory.stats` shows `missing_embeddings=0`.
5. No new `failed` executions in n8n for scheduled workflows.
6. Disk free space is sufficient for n8n executions and backups.

## Weekly maintenance

`deploy/scripts/ops/maintenance.py` does three things:

- prunes n8n executions older than `--days` while keeping the newest `--keep`;
- exports every public Supabase table to `OTHVM-backups/<date>/supabase/`
  (vector columns skipped);
- runs `memory.consolidate` at 0.97 (dry-run unless `--dry-run` is absent).

```powershell
# Safe preview
mcp_server\.venv\Scripts\python.exe deploy\scripts\ops\maintenance.py --dry-run

# Real run
mcp_server\.venv\Scripts\python.exe deploy\scripts\ops\maintenance.py

# Skip parts
mcp_server\.venv\Scripts\python.exe deploy\scripts\ops\maintenance.py --skip-prune
mcp_server\.venv\Scripts\python.exe deploy\scripts\ops\maintenance.py --skip-memory
```

The default backup root is `<repo parent>\OTHVM-backups`, outside the repository.
Override it with `OTHVM_BACKUP_DIR`.

## Backups

| What | How | Frequency |
| --- | --- | --- |
| Supabase tables | `maintenance.py` export | Weekly |
| Workflow definitions | `sync_workflows.py export` + git commit | After every live change |
| n8n credentials + encryption key | Copy the n8n user folder while n8n is stopped | Before upgrades |
| Local configs | Copy the untracked `config.json` files to an encrypted location | After changes |
| Telethon session | Copy only to a secure offline location | After login |

The n8n SQLite database alone is not a complete backup: credentials are encrypted
with a key stored in the n8n user folder.

## Restore

### Restore workflows

```powershell
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py `
  --dry-run --update-existing
mcp_server\.venv\Scripts\python.exe deploy\workflows\import_workflows.py `
  --apply --update-existing --activate
```

### Restore Supabase

1. Create the project and apply migrations in order.
2. Import the table exports with the Supabase dashboard or `psql`.
3. Run `memory.embed` until `remaining=0`.
4. Run `knowledge.stats` and re-ingest any source that is missing.
5. Verify `memory.stats` and one semantic query.

### Restore n8n

1. Stop the `n8n` service.
2. Restore the n8n user folder, including `database.sqlite` and the encryption
   key.
3. Start the service and verify workflows and credentials.
4. If credentials cannot be decrypted, recreate them and re-import workflows with
   a fresh credential map.

## Upgrades

### n8n

```powershell
npm install -g n8n@<version>
Restart-Service n8n
```

Back up the n8n user folder first. After the upgrade, run
`sync_workflows.py check`; if n8n migrated workflow fields, re-export and review
the diff.

### Python dependencies

```powershell
mcp_server\.venv\Scripts\python.exe -m pip install -r mcp_server\requirements.txt
mcp_server\.venv\Scripts\python.exe -m playwright install chromium
Restart-Service OTHVM-MCP
```

Telegram Personal has its own venv and requirements.

### Workflows

Change n8n, validate live, then:

```powershell
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py export
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py check
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py scan
git add deploy/workflows
git commit -m "Sync workflows from live"
```

## Incident playbooks

### MCP is down

1. `Get-Service OTHVM-MCP`; start it.
2. If it fails, read `mcp_server/logs/service.err.log`.
3. Check the port is not held by an orphan process:
   `Get-NetTCPConnection -LocalPort 8765`.
4. Start the desktop helper separately; the service does not provide GUI actions.
5. Run `Invoke-RestMethod http://127.0.0.1:8765/health`.

### n8n is down

1. `Get-Service n8n`; start it.
2. Check `http://127.0.0.1:5678/healthz`.
3. Inspect the NSSM stdout/stderr logs.
4. Confirm the n8n user folder and database are readable by LocalSystem.
5. Check disk space; n8n fails when the execution database cannot grow.

### Telegram stops answering

1. Check `getWebhookInfo` for `last_error_message`.
2. Confirm the public tunnel is running and points at port 5680.
3. Confirm Caddy and FastPath are running.
4. Test FastPath directly: `POST` a signed update, or check its counters.
5. If FastPath is down, Caddy should fall back to n8n; if Caddy is down, nothing
   answers.
6. Re-save the Telegram integration to re-register the webhook.

### Queue is stuck

The guard exists for this. It:

- restores `allowed_updates` to include `edited_message` and `callback_query`;
- deduplicates unclaimed queue rows by platform message ID;
- expires stale checkpoints older than 5 minutes with no running execution.

Run it manually and read the log:

```powershell
node guard\guard.js
Get-Content guard\guard.log -Tail 20
```

If the queue is still stuck, inspect `agent_message_queue` and
`agent_checkpoints` in the n8n SQLite database **after stopping n8n**. Never edit
the database while n8n is running.

### Memory retrieval is wrong or empty

1. `memory.stats` — check active count and missing embeddings.
2. `memory.embed` until `remaining=0`.
3. `memory.recall` with a known key and with a natural-language query.
4. If both fail, check the Gemini key and the Supabase service key.
5. If only semantic search fails, keyword fallback still works; fix embeddings
   before trusting similarity.

### Supabase is unreachable

1. Check the project status in the Supabase dashboard.
2. Confirm DNS and outbound HTTPS from the host.
3. The MCP returns real errors; it does not invent results.
4. Memory writes fail closed. Do not retry destructive operations blindly.
5. Restore from the latest export if data is lost.

### Disk is full

1. Prune n8n executions with `maintenance.py --skip-backup --skip-memory`.
2. Clear old logs, exports, and trash after reviewing them.
3. Move backups to another drive with `OTHVM_BACKUP_DIR`.
4. Never delete the n8n database or Supabase exports to free space.

### Android is offline

1. `GET :8767/health` and read `connected` plus the last error.
2. Check Tailscale on both devices.
3. Check USB debugging / wireless debugging.
4. Use `POST /device/reconnect`; if the wireless port changed, pair again.
5. Report the real cause; do not claim phone control works.

### Telegram Personal is offline

1. Check `:8768/health` and the service log.
2. MTProto may be blocked on the network; this is a known limitation.
3. Re-run `login.py` only if the session is invalid.
4. The bot path is independent; keep using it.

## Security operations

- Rotate the MCP API key, FastPath secret, n8n API key, Telegram bot token, and
  Supabase service key on a schedule and after any suspected exposure.
- After rotation, update every config that references the value and restart the
  services.
- Run `sync_workflows.py scan` before every commit.
- Keep `permission.json` at the least privilege that still works.
- Review `mcp_server/logs/actions.log` for unexpected writes.

See `docs/security.md` for the full inventory and incident response.
