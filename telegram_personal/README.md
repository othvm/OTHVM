# OTHVM — Telegram Personal (Telethon)

Local REST bridge to a **personal** Telegram account. It complements
`@othvm_bot`: the bot talks to OTHVM, while this service reads the operator's
real conversations, contacts and dialogs and can send on their behalf after
confirmation.

**Base URL:** `http://127.0.0.1:8768`
**Auth:** `X-API-Key: <api_key in config.json>`.

> **Port note:** the laptop desktop helper already listens on **8766**, so this
> service uses **8768** to avoid breaking screenshots/browser control. The n8n
> workflow `Personal Telegram (Founder)` points at 8768.

---

## One-time setup

1. Open <https://my.telegram.org> → **API development tools**, create an app and
   copy `api_id` / `api_hash`.
2. Copy `config.example.json` to `config.json` and fill in the values:
   ```json
   { "api_id": 123456, "api_hash": "abcdef...", "phone": "+10000000000" }
   ```
3. Create the session interactively:
   ```powershell
   .\.venv\Scripts\python.exe login.py
   ```
   Enter the code Telegram sends (and the 2FA password if enabled). This writes
   the configured session file — keep it private, it is a full login.
4. Start the API:
   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```
5. Install the Windows service (as Administrator):
   ```powershell
   .\install_service.cmd
   ```
   Service name: **OTHVM-TelegramPersonal**, auto-start, NSSM, runs as LocalSystem.

Health: `GET http://127.0.0.1:8768/health` (no auth).

---

## Operations

`POST /tools/telegram_personal` with `{"action": "...", ...}`.

| Action | Params | Permission |
|---|---|---|
| `send_message` | `contact`, `message`, `confirmed:true`, `confirm_unknown?` | confirmed |
| `send_file` | `contact`, `file_path` or `file_url`, `caption?`, `confirmed:true` | confirmed |
| `get_dialogs` | `limit` (30) | read-only |
| `get_messages` | `contact`, `limit` (20) | read-only |
| `search_messages` | `query`, `limit` (50), `chats_limit` (40) | read-only |
| `get_contacts` | `refresh?` | read-only |
| `forward_message` | `from_contact`, `message_id`, `to_contact`, `confirmed:true` | confirmed |

Contact can be `@username`, a phone number, a numeric id, or a contact/chat name.

---

## Hard safety rules (enforced in Python, not only in prompts)

- **Rate limit:** max **10 outgoing messages per rolling hour** (send_message,
  send_file, forward_message). State is derived from `send_log.json`, so it
  survives restarts.
- **Founder confirmation:** every send requires `confirmed:true`. Without it the
  API returns `needs_confirmation` and sends nothing.
- **Unknown contacts:** if the target is not in contacts/recent dialogs, the API
  returns `needs_confirmation` + `confirm_unknown_required`; retry only after the
  founder explicitly approves, with `confirm_unknown:true`.
- **Audit log:** every send attempt (success or failure) is appended to
  `send_log.json` with timestamp, contact, preview, message_id and error.
- **Flood wait:** `FloodWaitError` / `SlowModeWaitError` pauses all sending
  (`state.json`), sends a warning to the founder via the OTHVM bot, and returns
  `retry_after_seconds`.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI + Telethon service |
| `login.py` | one-time interactive login |
| `config.example.json` | sanitized configuration template |
| `config.json` | API id/hash/phone, port, local API key, bot notification target (untracked) |
| `<session_name>.session` | Telethon session (created by `login.py`, never commit) |
| `send_log.json` | audit log of every send attempt (untracked) |
| `state.json` | flood-wait pause state (untracked) |
| `install_service.cmd` | NSSM install (auto-start) |
| `logs/` | service stdout/stderr (untracked) |
