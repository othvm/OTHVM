# Telegram fast path

A small FastAPI service and Caddy router that handle two deterministic Telegram
commands without invoking the LLM:

- **Wake/alarm** — "صحيني بعد ساعتين", "wake me at 06:30", "اعمل منبه الساعة 6".
- **Desktop file** — "اعمل ملف على الديسكتوب اسمه test.txt".

Everything else is forwarded to n8n unchanged. The full design is in
[`docs/architecture.md`](../../docs/architecture.md).

## Why it exists

The agent is reliable but not instant. Waking up or creating a desktop file does
not need reasoning, so it bypasses the model. The fast path also keeps working
when n8n is slow; Caddy falls back to n8n only when FastPath is actually down
(502/503/504).

## Routing

```
Telegram
   │  POST /rest/projects/<project>/agents/v2/<agent>/webhooks/telegram
   ▼
Caddy :5680
   ├─ agent Telegram path ──► FastPath :5681
   └─ everything else     ──► n8n :5678
```

`Caddyfile` binds to `127.0.0.1:5680` and accepts any Host header so the tunnel
can forward the public hostname. The tunnel (ngrok or equivalent) points at
`5680`, not at n8n.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | `ok`, service name, counters, dedupe size |
| `POST /rest/projects/{project_id}/agents/v2/{agent_id}/webhooks/telegram` | Telegram webhook |
| `POST/GET /{path}` (catch-all) | Forward to n8n |

The webhook validates `X-Telegram-Bot-Api-Secret-Token` against `telegram_secret`
and accepts only `allowed_user_id` / `allowed_chat_id`. A matching wake or file
command is executed through MCP; every other update is forwarded to n8n.

## Configuration

Copy `config.example.json` to `config.json` and fill it in. The file is ignored
by git.

| Key | Required | Purpose |
| --- | --- | --- |
| `host` | No | Bind address; keep `127.0.0.1`. |
| `port` | No | `5681`. |
| `mcp_base` | Yes | `http://127.0.0.1:8765`. |
| `mcp_api_key` | Yes | Same value as MCP `api_key`. |
| `telegram_bot_token` | Yes | The bot that owns the webhook. |
| `telegram_secret` | Yes | Telegram `secret_token`; also used for validation. |
| `allowed_user_id` | Yes | Numeric Telegram user ID allowlist. |
| `allowed_chat_id` | Yes | Chat that receives fast replies. |
| `n8n_base` | Yes | `http://127.0.0.1:5678`. |
| `desktop_dir` | No | Directory used by the file command. |
| `dedupe_seconds` | No | In-memory update dedupe window (default 600). |
| `log_path` | No | JSONL fast-path log (default `fastpath.log`). |

Telegram allows one webhook per bot. Use the same bot token as the n8n Telegram
credential; FastPath sits in front of n8n for the agent webhook path.

## Run

```powershell
# Manual
mcp_server\.venv\Scripts\python.exe deploy\webhook_fastpath\main.py

# As services (elevated)
powershell -ExecutionPolicy Bypass -File deploy\webhook_fastpath\install_services.ps1

# Health
Invoke-RestMethod http://127.0.0.1:5681/health
Invoke-WebRequest http://127.0.0.1:5680/healthz
```

The installer registers `OTHVM-FastPath` and `OTHVM-Caddy` with NSSM, writes
logs next to this directory, and frees ports 5680/5681 first. It accepts
`PYTHON_EXE`, `NSSM_EXE`, and `CADDY_EXE` overrides.

## Tests

`test_parsers.py` covers relative/absolute wake parsing, Arabic digits, filename
extraction, destructive-command rejection, and missing-time behavior.

```powershell
mcp_server\.venv\Scripts\python.exe -m pytest deploy\webhook_fastpath\test_parsers.py
```

If `pytest` is not installed, run the file directly; it has a `__main__` test
runner.

## Dedupe and failure behavior

- `update_id` is recorded only after a successful fast-path action or a
  successful n8n forward (`status < 400`). A failed forward or handler is
  retryable.
- The dedupe cache is in memory with a TTL (`dedupe_seconds`); restarting the
  service clears it.
- If FastPath is down, Caddy retries the Telegram path against n8n.
- The fast path never invents a result: if MCP fails, it returns an explicit
  error and does not record the update as handled.

## Logs

`fastpath.log` is JSONL. Each entry records the update ID, whether it was handled
locally or forwarded, timings, and the error when one occurred. It is ignored by
git.
