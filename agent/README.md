# OTHVM agent

The OTHVM agent is the n8n AI Agent that the operator talks to through Telegram.
It is not a workflow: n8n stores it in the `agents` table and exposes it only in
the UI, not through the public API. This directory is the sanitized, versioned
copy of that definition.

## Files

| File | Purpose |
| --- | --- |
| `INSTRUCTIONS.md` | Full system prompt. Paste it into the agent's instructions field. |
| `agent.example.json` | Reference copy of the live agent settings, model, tools, skills, memory, and vector store. Descriptive only; not directly importable. |
| `skills/*.md` | The five agent skills, sanitized. Create one n8n skill per file. |

All credentials, workflow IDs, skill IDs, chat IDs, and personal identifiers are
placeholders. Resolve them on the target instance.

## Live definition (verified 2026-10-28)

| Setting | Value |
| --- | --- |
| Name | `OTHVM` |
| Model | `nvidia/nvidia/nemotron-3-ultra-550b-a55b` |
| Reasoning | `low` |
| Tool-call concurrency | 5 |
| Max iterations | 100 |
| Built-in web search | disabled (web search is the `info` workflow tool) |
| Agent memory | n8n storage, observational memory on |
| Observer / reflector model | `google/gemini-3.5-flash-lite` |
| Vector store | Supabase, table `knowledge_base`, RPC `match_knowledge` |
| Embedding model | `google/gemini-embedding-001` |
| Channel | Telegram, private mode, one allowed user |
| Tools | 15 workflow tools |
| Skills | 5 |

## Tools

Each tool is an n8n workflow exposed to the agent. IDs differ on every install;
match by workflow name using `deploy/workflows/manifest.json`.

| Tool | Workflow | Scope |
| --- | --- | --- |
| `mail` | Gmail | Gmail messages, labels, drafts, threads |
| `calendar` | Calendar | Google Calendar events and calendars |
| `files` | OTHVM — Files | Drive, Sheets, Slides, Docs, document export |
| `tasks` | OTHVM — Tasks | Google Tasks, Todoist, Linear, local todos |
| `notes` | OTHVM — Notes | Notion and local Supabase notes |
| `contacts` | Contacts | Google Contacts |
| `phone` | Android | Android over ADB/Tailscale |
| `computer` | Laptop | Windows control via the MCP server |
| `info` | OTHVM — Info | Weather, prayer, search, wiki, translate, YouTube, Books, NASA |
| `automation` | OTHVM — Automation | n8n workflows, progress, streaks, sleep, loops, reminders, status |
| `memory` | OTHVM — Memory | Unified memory, knowledge search, Airtable, context |
| `dev` | OTHVM — Dev | Code runner, code sessions, GitHub |
| `finance` | OTHVM — Finance | Expenses, Stripe, Shopify |
| `telegram` | OTHVM — Telegram | Bot, personal account, Discord, voice, TTS |
| `vision` | OTHVM — Vision | Image analysis only |

Tool descriptions in `agent.example.json` are the live descriptions and define
the operations each tool accepts.

## Skills

| Skill | Use when |
| --- | --- |
| `daily-plan` | Daily plan, morning briefing, prioritization, review of today |
| `wake-loop` | Wake-up alarm, recurring check-in, commitment loop |
| `device-ops` | Windows or Android operations and diagnostics |
| `dev-ship` | Build, fix, refactor, test, review, or ship code |
| `research-memory` | Research, fact-check, compare, or remember something |

## Recreate the agent

1. Install and import the workflows first (`docs/workflows.md`). Confirm the 15
   tool workflows exist and are active.
2. Create the model credential (NVIDIA API) and the Google Gemini credential used
   for embeddings and observational memory.
3. Create the Supabase credential and apply `supabase/migrations/` so
   `knowledge_base` and `match_knowledge` exist (`docs/memory.md`).
4. In n8n, open **Agents → New agent**:
   - Name: `OTHVM`.
   - Model: `nvidia/nvidia/nemotron-3-ultra-550b-a55b` with the NVIDIA credential.
   - Instructions: paste `INSTRUCTIONS.md`.
   - Config: reasoning `low`, concurrency `5`, max iterations `100`.
5. Add the five skills from `agent/skills/` (name and description are the first
   heading and the "Use when" line; instructions are the file body).
6. Add the 15 workflow tools. Use the names and descriptions from
   `agent.example.json`, and pick the workflow IDs from the target instance.
7. Add the Supabase vector store (`knowledge_base` / `match_knowledge`) with the
   Gemini embedding credential.
8. Enable observational memory with Gemini Flash Lite as observer and reflector.
9. Connect Telegram with the bot credential, private mode, and the operator's
   numeric user ID.
10. Send a test message, then verify tool calls and `/status`.

## Verification

- `agent.example.json` parses as JSON and contains 15 tools and 5 skills.
- The live agent was read from n8n's SQLite `agents` / `agent_history` tables on
  2026-10-28; the public API has no agent endpoint.
- Instructions and skills were sanitized: no names, chat IDs, credential IDs, or
  tokens from the live instance remain.
