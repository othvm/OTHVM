# Security

OTHVM is a single-operator system with unusually broad local privileges. The
agent can read files, run shell commands, control the desktop, and call Google
APIs. Treat the host as a trusted administrative boundary and everything else as
untrusted input.

## Threat model

| Asset | Exposure | Main risk |
| --- | --- | --- |
| Telegram bot and webhook | Public HTTPS hostname | Forged updates, replay, bot takeover |
| n8n | Loopback `:5678` | Workflow/credential theft, arbitrary code in Code nodes |
| MCP server | Loopback `:8765` / `:8766` | Full host compromise through `system_command` |
| Supabase | Public HTTPS + service-role key | Memory/knowledge exfiltration or deletion |
| n8n SQLite + encryption key | Local disk | Credential decryption if both are copied |
| Local config files | Local disk | API keys, bot token, service-role key |
| Workflow mirror | Git repository | Secret leakage through exports |
| Agent context | Telegram messages, email, web pages, documents | Prompt injection into tool calls |

### Trust boundaries

- **Public:** only the tunnel hostname. Caddy accepts the Telegram agent path
  and proxies everything else to n8n.
- **Loopback:** n8n, MCP, FastPath, Android, and Telegram Personal bind to
  `127.0.0.1`. API keys are still required; loopback is not an authorization
  boundary against a compromised local process.
- **SYSTEM:** n8n, MCP service, FastPath, Caddy, Android, and Telegram Personal
  run as LocalSystem. A vulnerability in any of them is a host compromise.
- **Operator session:** the MCP desktop helper and Chrome profile run as the
  interactive user.
- **Supabase:** the service-role key bypasses RLS. RLS is not a boundary for the
  MCP server or n8n.

### Prompt injection

Email, Notion pages, GitHub issues, web pages, and documents are untrusted
content. The agent must not follow instructions found in tool output that
conflict with its system instructions. High-risk operations remain gated by the
MCP permission mode and Telegram confirmation. Keep the default mode at `read`
unless a task genuinely needs writes.

## Secret inventory

All live values stay in untracked files. The tracked `config.example.json`
contains only placeholders.

| Secret | Where it lives | Used for | Rotation impact |
| --- | --- | --- | --- |
| MCP API key | `mcp_server/config.json` (`api_key`), FastPath config (`mcp_api_key`) | `X-API-Key` on every MCP call | Restart MCP, FastPath, and any caller |
| MCP permission codes | `mcp_server/pending_permission.json` | Mode-change approval | Ephemeral; expire in 5 minutes |
| Telegram bot token | MCP, FastPath, guard configs; n8n Telegram credential | Bot API and webhook | Re-register webhook; update all three configs and the n8n credential |
| Telegram webhook secret | FastPath and guard configs; Telegram `setWebhook` | Validate `X-Telegram-Bot-Api-Secret-Token` | Re-register webhook and restart FastPath/guard |
| Telegram user/chat IDs | Configs and workflow placeholders | Allowlist and delivery | Not secret, but sanitized as personal data |
| Gemini API key | `mcp_server/config.json`, n8n `googlePalmApi` credential | Embeddings, vision, summaries | Update MCP config and n8n credential |
| Supabase URL | MCP config, n8n credential | Project endpoint | Update both |
| Supabase service-role key | MCP config, n8n Supabase credential | Full database access | Rotate in Supabase; update both; restart |
| n8n public API key | `mcp_server/config.json` (`n8n_api_key`) | Workflow and execution management | Revoke/create in n8n; update MCP |
| n8n MCP token | `mcp_server/config.json` (`n8n_mcp_key`) | Optional MCP bridge | Revoke/create; update MCP |
| Android agent API key | `android_agent/config.json` (`api_key`) | `X-API-Key` on local ADB bridge calls | Generate a new random key; update config and restart the Android agent |
| n8n encryption key | n8n user folder (`config`) | Decrypt stored credentials | Rotating it requires re-entering every credential |
| Google OAuth client/refresh tokens | n8n credentials | Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, Contacts, YouTube, Books | Reconnect each credential in n8n |
| GitHub OAuth token | n8n `githubOAuth2Api` credential | Repo and workflow operations | Reconnect credential |
| Notion/Linear/Todoist/Airtable/Stripe/NASA/Discord keys | n8n credentials | Optional integrations | Reconnect the affected credential |
| Groq key | Voice workflow Code/HTTP node | Speech transcription | Edit the workflow or move it to a credential |
| OpenAI-compatible STT key | `OPENAI_API_KEY` environment variable only | Optional desktop-pet transcription | Update the environment and restart the pet; never put it in `desktop_pet/config.json` |
| NVIDIA key | n8n `nvidiaApi` credential | Agent model | Reconnect credential |
| ngrok authtoken | `%LOCALAPPDATA%\ngrok\ngrok.yml` | Public tunnel | Rotate in ngrok; update the file and restart |
| Telethon session | `telegram_personal/*.session` | Personal Telegram login | Revoke the session in Telegram; log in again |
| SSH/private keys | Not part of this repository | — | Never add them |

Never commit a live `config.json`, `.session`, `.env`, `credentials.json`,
`import_report.json`, database file, log, or browser profile. `.gitignore`
covers these paths; verify with `git status` before every push.
`desktop_pet/config.json` is preferences only and is ignored; the pet reads keys
from `mcp_server/config.json` or environment variables. The local Whisper cache
under `desktop_pet/cache/` is also ignored and must not be committed.

## Sanitization

`deploy/workflows/sync_workflows.py export` reads live workflows and replaces:

- credential IDs with `REPLACE_WITH_CREDENTIAL_ID` (names stay);
- Telegram chat/user IDs with `REPLACE_WITH_TELEGRAM_CHAT_ID`;
- bot tokens with `REPLACE_WITH_TELEGRAM_BOT_TOKEN`;
- MCP API keys with `REPLACE_WITH_MCP_API_KEY`;
- command webhook secrets with `REPLACE_WITH_COMMAND_SECRET`;
- Groq keys with `REPLACE_WITH_GROQ_API_KEY`;
- GitHub owners with `REPLACE_WITH_GITHUB_OWNER`;
- emails, Supabase URLs, ngrok hosts, JWTs, Google API keys, and generic tokens
  by pattern;
- absolute repository/user paths;
- the operator's name in English and Arabic when `OTHVM_OPERATOR_NAME` /
  `OTHVM_OPERATOR_NAME_AR` are set. Names are deliberately not hardcoded, so run
  both `export` and `scan` with the same environment.

`sync_workflows.py scan` fails when a known live value or suspicious pattern
remains. Run it before every commit that touches `deploy/workflows/`.

The exporter also removes instance-only fields that can carry internal state:
`activeVersion`, `shared`, `pinData`, `staticData`, `meta`, `parentFolderId`,
`versionId`, `versionCounter`, `triggerCount`, `nodeGroups`, and `isArchived`.

Sanitization is not a substitute for rotation. If a live secret was ever
committed, rotate it. Removing it from the current tree does not remove it from
Git history, clones, forks, caches, or CI logs.

## Publication checklist

Run from a clean checkout:

```powershell
# 1. No live config or session files are tracked
git ls-files | Select-String -Pattern 'config\.json$|\.session$|\.env$|permission\.json$'

# 2. No secret-looking values in tracked files
git grep -n -I -E 'AIza[0-9A-Za-z_-]{20,}|ghp_[0-9A-Za-z]{20,}|xox[baprs]-|-----BEGIN (RSA |OPENSSH )?PRIVATE KEY-----|eyJhbGciOi'

# 3. Workflow mirror is clean
mcp_server\.venv\Scripts\python.exe deploy\workflows\sync_workflows.py scan

# 4. Working tree has no unintended files
git status --short
```

Also confirm:

- the remote is `https://github.com/othvm/OTHVM.git` and the branch is `master`;
- the initial root commit is clean and the follow-up commits do not reintroduce a
  live key;
- `git log -p` does not contain a live key;
- GitHub secret scanning and push protection are enabled for the public
  repository;
- the repository is public by explicit decision; do not add private data just
  because the history was rewritten.

## Git history purge

The first pushed root contained a live MCP API key inside workflow exports. A
later audit found two more hardcoded values in that root: the Android agent API
key and the Telegram command webhook secret. All three were removed, the
workflow sanitizer was tightened, and the root commit was amended before the
feature push; later commits build on the sanitized root. The live Android and
command-webhook values were not rotated automatically (the Android restart needs
elevation and the command caller is external). Rotate them now. To verify:

```powershell
git log --oneline --all
git rev-list --all --count
git grep -n -F '<old-key>' $(git rev-list --all)  # no output
```

If a secret ever reaches a remote:

1. Revoke and rotate the secret first. History rewriting does not invalidate it.
2. Remove the remote or make it private.
3. Rebuild the history (`git checkout --orphan clean`, commit the sanitized
   tree, move the branch, expire reflogs, `git gc --prune=now`).
4. Force-push only after every collaborator has been told to re-clone.
5. Ask the hosting provider to purge cached views and forks if the repository was
   public.

## Rotation procedure

1. Create the new value in the provider.
2. Update every untracked config that references it.
3. Update the matching n8n credential or workflow node.
4. Restart the affected service (`n8n`, `OTHVM-MCP`, `OTHVM-FastPath`,
   `OTHVM-Caddy`, `guard` task).
5. Verify health, one Telegram message, one tool call, and one memory query.
6. Revoke the old value.
7. Record the rotation date in your own password manager, not in this repository.

For the MCP key specifically, update `mcp_server/config.json` and
`deploy/webhook_fastpath/config.json` together; FastPath calls MCP with the same
key.

## Incident response

### Suspected bot or webhook compromise

1. `getWebhookInfo` and check for an unexpected URL or `last_error_message`.
2. Revoke the bot token with BotFather and create a new one.
3. Update MCP, FastPath, guard, and the n8n Telegram credential.
4. Re-register the webhook with a new `secret_token`.
5. Review `mcp_server/logs/actions.log` and n8n executions for unauthorized
   calls.

### Suspected MCP compromise

1. Stop `OTHVM-MCP` and the desktop helper.
2. Set `permission.json` to `none` or create `mcp_server/EMERGENCY_STOP`.
3. Review `actions.log` for shell, file, and credential access.
4. Rotate the MCP key, n8n API key, Supabase service key, and any credential the
   process could read.
5. Rebuild the host if `system_command` ran with untrusted input.

### Suspected Supabase compromise

1. Rotate the service-role key and the database password.
2. Review Supabase logs and `memory_events` for unexpected writes or deletes.
3. Restore from the latest export if data was lost.
4. Re-run `memory.embed` and verify retrieval.

### Lost laptop or phone

1. Revoke the Telegram session and bot webhook.
2. Revoke Tailscale devices and ngrok tunnels.
3. Rotate every credential stored on the host.
4. Disable the n8n API key and Supabase service key.
5. Do not restore the n8n user folder to a new machine until credentials are
   re-created.

## Least-privilege recommendations

- Keep `permission.json` at `read` by default. Use `write` only for a specific
  task and return to `read` afterward.
- Keep the n8n public API key scoped to the minimum needed; do not reuse it for
  unrelated automation.
- Use a restricted Stripe key, a read-only NASA key, and a Notion integration
  shared only with the pages it needs.
- Do not run the MCP service as LocalSystem if you can give it a dedicated
  low-privilege service account; the current install uses LocalSystem for
  simplicity and desktop access.
- Keep Caddy, MCP, FastPath, Android, and Telegram Personal bound to loopback.
- Do not expose the n8n editor publicly. The tunnel routes only the Telegram
  webhook path; keep it that way.
- Review n8n executions and `actions.log` after any agent behavior that looks
  unexpected.
