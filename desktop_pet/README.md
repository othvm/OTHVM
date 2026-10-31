# OTHVM Desktop Pet

A transparent desktop character and voice layer for the existing OTHVM agent.
It is **not** a second agent: every message goes through the already-published
n8n `OTHVM` agent, and every animation state comes from real telemetry (n8n
tool-call records or MCP server action events). The pet never invents progress
from an LLM narration.

The authoritative character is `assets/pet.jpg`. The pet's derived transparent
PNGs live under `desktop_pet/assets/`; the original JPEG is never modified.

## What it does

- Per-pixel transparent, always-on-top Win32 window (`UpdateLayeredWindow`), so
  the character has no rectangular background.
- Transparent pixels are click-through; the visible character can be dragged.
- 11 execution-aware states: idle, listening, thinking, researching, coding,
  executing, working, speaking, success, error, approval.
- Text chat and push-to-talk voice input.
- Arabic-first Edge TTS (`ar-EG-SalmaNeural`) with a Windows SAPI fallback.
- Gemini speech-to-text reusing the key already in `mcp_server/config.json`,
  with a local `faster-whisper` fallback that works offline after the model is
  downloaded.
- Approval cards that resume the **same** suspended n8n request; approvals are
  never re-implemented by the pet.
- A small Tkinter control panel; the character and panel are separate windows.
- Optional autostart in the current user's `Run` key (no administrator rights).

## Requirements

- Windows 10/11, Python 3.11+ (the repository uses Python 3.12).
- The OTHVM MCP service running on `http://127.0.0.1:8765`.
- The n8n instance and the published `OTHVM` agent configured in
  `mcp_server/config.json`.
- A microphone for voice input; speakers for TTS.
- Network access for Edge TTS and Gemini STT. The local Whisper fallback and
  SAPI work offline; the default Windows SAPI voices are English-only.
- About 70 MB of disk for the local Whisper `base` model (larger models need
  more), downloaded once into `desktop_pet/cache/whisper` (git-ignored).

## Install

Use the existing MCP virtualenv so the pet and server share one runtime:

```powershell
cd D:\projects\Ai\OTHVM
mcp_server\.venv\Scripts\python.exe -m pip install -r desktop_pet\requirements.txt
Copy-Item desktop_pet\config.example.json desktop_pet\config.json
```

`desktop_pet/config.json` is git-ignored and must contain **no secrets**. The
pet reads the MCP API key and Gemini key from the git-ignored
`mcp_server/config.json`, or from `OTHVM_MCP_API_KEY` / `GEMINI_API_KEY`.
An optional OpenAI-compatible STT key is read from `OPENAI_API_KEY`, never from
the pet config.

## Run

```powershell
# normal start
mcp_server\.venv\Scripts\pythonw.exe desktop_pet\app.py

# visible console for diagnostics
mcp_server\.venv\Scripts\python.exe desktop_pet\app.py --debug

# panel only (no layered window), useful when testing in a service session
mcp_server\.venv\Scripts\python.exe desktop_pet\app.py --no-pet
```

The pet must run in the interactive desktop session. It cannot render a
layered window from Windows session 0.

## Controls

| Action | Control |
| --- | --- |
| Open/focus the panel | Click the character, or `Ctrl+Alt+H` |
| Start/stop push-to-talk | `Ctrl+Alt+Space`, double-click the character, or 🎤 |
| Show/hide the character | `Ctrl+Alt+O` |
| Drag the character | Hold the left mouse button on visible pixels |
| Send a message | Enter in the panel entry |
| Approve/deny | Buttons on the approval card |
| New conversation | **محادثة جديدة** |
| Hide the panel | Window close button (the character stays) |

The character's right-click also opens the panel.

## Voice

- Recording is push-to-talk at 16 kHz mono; the clip is capped by
  `stt.max_seconds` (default 30 s).
- STT tries backends in order and reports which one actually produced the text:
  1. Gemini (`stt.model`, default `gemini-3.8-flash`) using the existing key.
     `stt.gemini_timeout_seconds` (default 15) and `stt.gemini_retries`
     (default 1) bound the wait so a hanging cloud call cannot block voice.
  2. Local `faster-whisper` (`stt.local_model`, default `base`, CPU `int8`).
     The model is warmed in the background at startup and loaded lazily if
     needed. `stt.local_fallback` can disable it; `stt.local_model` may be
     changed to `small` for better accuracy at higher CPU cost.
  3. Optional OpenAI-compatible `/audio/transcriptions`
     (`stt.openai_base_url` + `OPENAI_API_KEY`).
- The transcript carries `engine` and, when a backend failed first,
  `fallback_from`. The panel logs the engine but never presents a fallback as
  the primary backend. Silence that a backend successfully returns as empty is
  not re-interpreted by the next backend.
- TTS defaults to Edge TTS. If Edge TTS is unavailable, the pet falls back to
  Windows SAPI when `tts.allow_sapi_fallback` is true.
- Wake word is optional and **disabled by default** (`wake_word.enabled`).
  When enabled, clips without the phrase are ignored and reported in the panel.
- Speech is only a layer around the existing agent. There is no local
  reasoning model and no second tool registry.

The local model is downloaded by `faster-whisper` on first use. To preload it
explicitly:

```powershell
mcp_server\.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8', download_root='desktop_pet/cache/whisper'); print('model ready')"
```

The first download is about 70 MB for `base`; later starts load the cached
model in roughly 2 seconds on the verified host.

## Telemetry and approvals

The MCP bridge (`mcp_server/agent_bridge.py`) calls n8n's MCP `call_agent` tool
and tails n8n's local `agents_messages` SQLite log. Only real records are turned
into events:

- `reasoning` part -> thinking
- `tool-call` part -> researching / coding / executing / working, classified
  from the actual tool name and a reduced input summary
- `text` part -> assistant text
- suspended result -> approval card with the real continuation

The pet also subscribes to `GET /events` for MCP tool activity from other
workflows. Chat-scoped events are deduplicated by `session_id`, and background
events never overwrite an active foreground request.

## Memory

The pet does not own memory. It reuses the agent's memory tools. The memory
schema now includes entities, explicit relations, episodes, provenance links,
hybrid retrieval, and soft deletion in addition to `memory_items`; see
[`docs/memory.md`](../docs/memory.md). Voice and text requests reach those tools
through the same agent.

## Autostart

```powershell
mcp_server\.venv\Scripts\python.exe desktop_pet\autostart.py install --hidden
mcp_server\.venv\Scripts\python.exe desktop_pet\autostart.py status
mcp_server\.venv\Scripts\python.exe desktop_pet\autostart.py remove
```

The `Run` value points at `pythonw.exe` and this repository's `app.py`; it
contains no keys.

## Assets and regeneration

```powershell
mcp_server\.venv\Scripts\python.exe desktop_pet\derive_assets.py
mcp_server\.venv\Scripts\python.exe -c "from pathlib import Path; from desktop_pet.effects import render_contact_sheet; render_contact_sheet(Path('desktop_pet/assets/pet_master.png'), Path('desktop_pet/artifacts'))"
```

`derive_assets.py` reads `assets/pet.jpg` read-only and writes
`desktop_pet/assets/pet_master.png`, `pet_idle.png`, and `pet_icon.png`.
`artifacts/contact_sheet.png` is the visual review sheet for all 11 states.

## Troubleshooting

- **`events: reconnecting`** — the MCP service is down or the API key is
  wrong. Check `GET /agent/status` with the `X-API-Key` header.
- **`agent: not configured`** — `agent_id` or `n8n_mcp_key` is missing from
  `mcp_server/config.json`.
- **`log: unreadable`** — `n8n_db_path` is not readable by the MCP service.
  The service normally runs as LocalSystem; the interactive user may not be
  able to read that path directly.
- **No Arabic SAPI voice** — expected on a default Windows install; Edge TTS is
  the primary Arabic voice and needs network access.
- **`STT: Gemini` only, no local fallback** — `faster-whisper` is not installed
  in the MCP venv, or `stt.local_fallback` is false. Install
  `desktop_pet/requirements.txt`; the panel's status line lists the active
  chain.
- **Local STT says the model timed out** — the first download is slow. Let the
  preload finish (watch `desktop_pet/logs/pet.log` for
  `local Whisper model ready`), then retry. The model stays in
  `desktop_pet/cache/whisper`.
- **`metadata_errors` / PyAV error** — the pet decodes WAV with `soundfile` and
  passes a float array to Whisper, so this should not occur; if it does,
  reinstall `soundfile` from the requirements.
- **Character is invisible** — verify the app is running in the interactive
  session and that `desktop_pet/assets/pet_master.png` exists.
- **Hotkeys do nothing** — another application may own `Ctrl+Alt+Space`; use
  the panel buttons instead.
- **`events: reconnecting` after a service restart** — the client backs off up
  to 30 seconds and reconnects automatically. If the service stays down, start
  it from an elevated shell (`Start-Service OTHVM-MCP`); `nssm restart` from
  inside the service can leave it stopped.

## Verification status

This file distinguishes implemented, tested, and blocked work. The full
evidence table is in [`docs/validation.md`](../docs/validation.md).

Tested live on the current host (2026-10-30):

- Layered window: per-pixel transparency (corner pixel delta `0.0` against the
  hidden desktop, center delta `105.3`), click-through on transparent pixels,
  hit-test on opaque pixels, always-on-top, animation frames change, and
  `Ctrl+Alt+O` hides/shows the pet.
- Dragging: the window followed the message-associated cursor position; the
  logged delta matched the window delta after the `GetMessagePos` fix.
- Real chat stream through the same client the panel uses:
  `accepted → thread → thinking → assistant_text → response → done`
  (`BRIDGE_CHAT_OK`), plus a real `computer` tool call and a real `memory` tool
  call whose reply matched the stored value exactly.
- Panel transcript, `events: connected`, `agent: ready`, state `SPEAKING`, and
  the deferred `SUCCESS`/`IDLE` transition after TTS.
- Real error path: NVIDIA API timeout surfaced in the transcript and returned
  to idle; no fabricated success.
- Voice: real microphone capture (16 kHz mono WAV), Edge TTS Arabic playback,
  SAPI fallback, local Whisper Arabic transcription, and Gemini-timeout →
  local fallback.
- Autostart: `status → install → status → remove → status` round-trip.
- Wake-word filtering/auto-send and foreground/background event dedup
  (unit-level, real handlers).
- SSE reconnect: exponential backoff observed on a real service restart, and
  reconnect verified against a controlled SSE server.
- Approval card: synthetic approve/deny/informational paths and the exact
  continuation payload. **Live approval resume is blocked**: the published
  agent has no approval-gated tools, and changing its policy is out of scope.

Not verified: a clean install on a second machine, and a live suspended
approval from the published agent.
