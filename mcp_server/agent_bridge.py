"""Bridge between the OTHVM desktop/voice layer and the existing n8n agent.

The bridge deliberately does not implement reasoning, tools, permissions, or
memory.  It calls n8n's own MCP tool ``call_agent`` for the already-published
``OTHVM`` agent and reads n8n's local agent message log to turn real tool-call
records into animation events.

Only the local n8n SQLite database is read.  No message payloads are written to
logs; tool inputs are reduced to a small, non-sensitive summary.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

try:  # optional; the poll fallback works without it
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except Exception:  # pragma: no cover - optional dependency
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]


class AgentBridgeError(RuntimeError):
    """Raised when n8n cannot run the agent."""


def _utc_sqlite(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def parse_parts(raw: Any) -> list[dict]:
    """Return the content parts of an n8n agent message."""
    obj = _parse_json(raw)
    if not isinstance(obj, dict):
        return []
    content = obj.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    return []


def message_text(raw: Any) -> str:
    parts = parse_parts(raw)
    out = []
    for part in parts:
        if part.get("type") == "text" and part.get("text"):
            out.append(str(part["text"]))
    return "\n".join(out).strip()


def _safe_summary(value: Any, depth: int = 0) -> Any:
    """Reduce a tool input to names/operations, never full payloads."""
    if depth > 2:
        return "..."
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lk = str(key).lower()
            if lk in ("text", "content", "body", "html", "message", "query", "sql",
                      "password", "token", "secret", "api_key", "authorization",
                      "cookie", "phone", "email", "chat_id"):
                if lk in ("query", "message", "text"):
                    out[key] = str(item)[:120]
                else:
                    out[key] = "<omitted>"
            elif isinstance(item, (dict, list)):
                out[key] = _safe_summary(item, depth + 1)
            else:
                out[key] = item
        return out
    if isinstance(value, list):
        return [_safe_summary(v, depth + 1) for v in value[:5]]
    return value


RESEARCH_HINTS = (
    "search", "browser", "web", "research", "knowledge", "fetch", "scrape",
    "crawl", "news", "weather", "maps", "youtube", "notion", "drive", "docs",
    "wikipedia", "lookup", "read",
)
CODING_HINTS = (
    "code", "coding", "dev", "development", "git", "github", "file", "files",
    "write", "edit", "patch", "repo", "project", "script", "python", "node",
)
EXECUTE_HINTS = (
    "system", "desktop", "computer", "android", "phone", "telegram", "automation",
    "loop", "alarm", "reminder", "finance", "vision", "startup", "service",
    "task", "window", "app", "shell", "powershell", "cmd", "exec", "run",
    "send", "control", "screenshot", "record", "clipboard", "volume", "brightness",
)


def classify_tool(tool_name: str, tool_input: Any = None) -> tuple[str, str]:
    """Map a real tool call to one of the pet's execution states.

    Returns ``(state, reason)`` where state is one of RESEARCHING, CODING,
    EXECUTING, WORKING.  Unknown tools intentionally fall back to WORKING.
    """
    name = str(tool_name or "").strip().lower()
    summary = _safe_summary(tool_input or {})
    blob = json.dumps(summary, ensure_ascii=False).lower() if summary else ""
    text = f"{name} {blob}"

    # Refine common compound tools by their operation.
    if name in ("files", "file"):
        op = str((summary or {}).get("operation") or (summary or {}).get("action") or "").lower()
        if any(h in op for h in ("write", "edit", "create", "delete", "move", "copy")):
            return "CODING", f"files.{op or 'write'}"
        return "RESEARCHING", f"files.{op or 'read'}"
    if name in ("browser", "web"):
        op = str((summary or {}).get("operation") or (summary or {}).get("action") or "").lower()
        if any(h in op for h in ("search", "navigate", "read", "extract", "fetch")):
            return "RESEARCHING", f"browser.{op}"
        return "EXECUTING", f"browser.{op or 'control'}"
    if name in ("memory", "knowledge"):
        return "WORKING", name
    if name in ("development", "dev", "code"):
        return "CODING", name
    if name in ("automation", "loops", "telegram", "android", "computer", "desktop"):
        return "EXECUTING", name

    if any(h in text for h in RESEARCH_HINTS):
        return "RESEARCHING", name
    if any(h in text for h in CODING_HINTS):
        return "CODING", name
    if any(h in text for h in EXECUTE_HINTS):
        return "EXECUTING", name
    return "WORKING", name


class SqliteChangeSignal:
    """Event-driven signal for changes to the n8n SQLite database.

    Uses watchdog when available and falls back to a low-frequency local poll.
    This is filesystem-level change detection, not network/API polling.
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.event = threading.Event()
        self._observer = None
        self._handler = None
        if Observer is not None:
            try:
                signal = self

                class _Handler(FileSystemEventHandler):  # type: ignore[misc,valid-type]
                    def on_any_event(self, event):  # noqa: ANN001
                        try:
                            name = Path(str(getattr(event, "src_path", ""))).name
                        except Exception:
                            name = ""
                        if name.startswith("database.sqlite"):
                            signal.event.set()

                self._handler = _Handler()
                self._observer = Observer()
                self._observer.schedule(self._handler, str(self.db_path.parent), recursive=False)
                self._observer.daemon = True
                self._observer.start()
            except Exception:
                self._observer = None
                self._handler = None

    def wait(self, timeout: float = 0.4) -> bool:
        changed = self.event.wait(timeout)
        self.event.clear()
        return changed

    def close(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=1)
            except Exception:
                pass
            self._observer = None


class AgentBridge:
    """Call the existing n8n agent and emit real execution events."""

    def __init__(self, n8n_base: str, n8n_mcp_key: str, agent_id: str,
                 db_path: str, timeout: float = 300.0):
        self.n8n_base = n8n_base.rstrip("/")
        self.n8n_mcp_key = n8n_mcp_key
        self.agent_id = agent_id
        self.db_path = db_path
        self.timeout = timeout

    # ------------------------------------------------------------------ n8n MCP
    async def _mcp_call(self, name: str, arguments: dict, timeout: float) -> dict:
        url = self.n8n_base + "/mcp-server/http"
        headers = {
            "Authorization": "Bearer " + self.n8n_mcp_key,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            raise AgentBridgeError(f"n8n MCP HTTP {resp.status_code}: {resp.text[:300]}")
        data = None
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
        if not data:
            raise AgentBridgeError("n8n MCP returned no data")
        if data.get("error"):
            raise AgentBridgeError(f"n8n MCP error: {data['error']}")
        result = data.get("result") or {}
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for content in result.get("content") or []:
            if content.get("type") == "text":
                parsed = _parse_json(content.get("text"))
                if isinstance(parsed, dict):
                    return parsed
        raise AgentBridgeError("n8n MCP returned an unexpected tool result")

    # --------------------------------------------------------------- DB reader
    def _connect(self) -> sqlite3.Connection:
        path = str(self.db_path).replace("\\", "/")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def discover_thread(self, since: str, text: str) -> str | None:
        """Find the draft-chat thread created for this request."""
        wanted = re.sub(r"\s+", " ", str(text or "")).strip()[:240]
        con = self._connect()
        try:
            rows = con.execute(
                """
                SELECT m.threadId AS thread_id, m.content AS content, m.createdAt AS created_at
                FROM agents_messages m
                JOIN agents_threads t ON t.id = m.threadId
                WHERE m.role = 'user'
                  AND m.createdAt >= ?
                  AND t.resourceId LIKE 'draft-chat:%'
                ORDER BY m.createdAt DESC
                LIMIT 40
                """,
                (since,),
            ).fetchall()
        finally:
            con.close()
        fallback = None
        for row in rows:
            fallback = fallback or str(row["thread_id"])
            got = re.sub(r"\s+", " ", message_text(row["content"])).strip()
            if got and (got == wanted or got.startswith(wanted[:120])):
                return str(row["thread_id"])
        return fallback

    def _rows_after(self, thread_id: str, since: str) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                """
                SELECT id, threadId, role, content, createdAt, updatedAt
                FROM agents_messages
                WHERE threadId = ?
                  AND (createdAt >= ? OR updatedAt >= ?)
                ORDER BY createdAt ASC, updatedAt ASC
                LIMIT 300
                """,
                (thread_id, since, since),
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- watcher
    async def watch_thread(self, thread_id: str | None, since: str,
                           emit: Callable[[str, dict], Awaitable[None]],
                           stop: asyncio.Event, text: str = "") -> str | None:
        """Tail the local n8n message log and emit only real activity."""
        seen: set[str] = set()
        seen_tools: set[str] = set()
        seen_text: set[str] = set()
        signal = SqliteChangeSignal(self.db_path)
        resolved = thread_id
        try:
            while not stop.is_set():
                if not resolved:
                    resolved = await asyncio.to_thread(self.discover_thread, since, text)
                    if resolved:
                        await emit("thread", {"session_id": resolved})
                if resolved:
                    rows = await asyncio.to_thread(self._rows_after, resolved, since)
                    for row in rows:
                        msg_id = str(row.get("id") or "")
                        if not msg_id:
                            continue
                        digest = hashlib.sha1(
                            (msg_id + "|" + str(row.get("updatedAt") or "") + "|" + str(row.get("content") or ""))
                            .encode("utf-8")
                        ).hexdigest()
                        if digest in seen:
                            continue
                        seen.add(digest)
                        role = str(row.get("role") or "")
                        if role == "user":
                            continue
                        parts = parse_parts(row.get("content"))
                        for part in parts:
                            ptype = str(part.get("type") or "")
                            if ptype == "reasoning" and msg_id not in seen_text:
                                seen_text.add(msg_id)
                                await emit("thinking", {
                                    "session_id": resolved,
                                    "message_id": msg_id,
                                })
                            elif ptype == "tool-call":
                                call_id = str(part.get("toolCallId") or "")
                                if call_id and call_id in seen_tools:
                                    continue
                                if call_id:
                                    seen_tools.add(call_id)
                                tool_name = str(part.get("toolName") or "tool")
                                state, reason = classify_tool(tool_name, part.get("input"))
                                output = part.get("output") if isinstance(part.get("output"), dict) else {}
                                ok = None
                                if isinstance(output, dict) and output.get("status"):
                                    ok = str(output.get("status")).lower() in ("success", "ok", "completed")
                                await emit("tool", {
                                    "session_id": resolved,
                                    "message_id": msg_id,
                                    "tool_call_id": call_id,
                                    "tool": tool_name,
                                    "state": state,
                                    "reason": reason,
                                    "ok": ok,
                                    "input": _safe_summary(part.get("input") or {}),
                                })
                            elif ptype == "text":
                                value = str(part.get("text") or "").strip()
                                if value and value not in seen_text:
                                    seen_text.add(value)
                                    await emit("assistant_text", {
                                        "session_id": resolved,
                                        "message_id": msg_id,
                                        "text": value[:4000],
                                    })
                if stop.is_set():
                    break
                await asyncio.to_thread(signal.wait, 0.4)
        finally:
            signal.close()
        return resolved

    # -------------------------------------------------------------------- chat
    async def chat(self, message: str, session_id: str | None,
                   emit: Callable[[str, dict], Awaitable[None]],
                   timeout: float | None = None) -> None:
        request_id = "req_" + uuid.uuid4().hex[:12]
        started_dt = datetime.now(timezone.utc)
        started = _utc_sqlite(started_dt - timedelta(seconds=2))
        timeout = float(timeout or self.timeout)
        await emit("accepted", {
            "request_id": request_id,
            "session_id": session_id,
            "at": started_dt.isoformat(),
        })

        stop = asyncio.Event()
        watcher = asyncio.create_task(
            self.watch_thread(session_id, started, emit, stop, text=message)
        )

        request = {"type": "message", "message": message}
        if session_id:
            request["sessionId"] = session_id
        call = asyncio.create_task(self._mcp_call(
            "call_agent",
            {"agentId": self.agent_id, "request": request},
            timeout + 10,
        ))

        result: dict | None = None
        error: Exception | None = None
        try:
            result = await asyncio.wait_for(call, timeout)
        except asyncio.TimeoutError:
            call.cancel()
            error = AgentBridgeError(f"agent timed out after {int(timeout)}s")
        except Exception as exc:  # noqa: BLE001
            error = exc
        finally:
            stop.set()
            try:
                await asyncio.wait_for(watcher, 3)
            except Exception:
                watcher.cancel()

        if error is not None:
            await emit("error", {
                "request_id": request_id,
                "message": str(error)[:500],
                "timeout": isinstance(error, asyncio.TimeoutError) or "timed out" in str(error),
            })
            return

        result = result or {}
        status = str(result.get("status") or "").lower()
        resolved_session = str(result.get("sessionId") or session_id or "")
        if status == "completed":
            response = str(result.get("response") or "")
            await emit("response", {
                "request_id": request_id,
                "session_id": resolved_session,
                "execution_id": result.get("executionId"),
                "text": response,
            })
            await emit("done", {
                "request_id": request_id,
                "session_id": resolved_session,
                "status": "completed",
                "execution_id": result.get("executionId"),
            })
            return

        if status == "suspended" and isinstance(result.get("approvals"), list) and result["approvals"]:
            approval = result["approvals"][0]
            await emit("approval", {
                "request_id": request_id,
                "session_id": resolved_session,
                "tool": approval.get("toolName"),
                "prompt": approval.get("prompt") or approval.get("message") or "Approval required",
                "continuation": approval.get("continuation"),
            })
            await emit("done", {
                "request_id": request_id,
                "session_id": resolved_session,
                "status": "suspended",
            })
            return

        if status == "approval_required":
            await emit("approval_required", {
                "request_id": request_id,
                "session_id": resolved_session,
                "suspensions": result.get("suspensions") or [],
                "note": "This approval must be completed in the n8n Preview chat.",
            })
            await emit("done", {
                "request_id": request_id,
                "session_id": resolved_session,
                "status": "approval_required",
            })
            return

        await emit("error", {
            "request_id": request_id,
            "message": str(result.get("message") or result.get("error") or "agent did not complete")[:500],
            "code": result.get("code"),
        })

    async def approve(self, continuation: dict, approved: bool,
                      emit: Callable[[str, dict], Awaitable[None]],
                      timeout: float | None = None) -> None:
        request_id = "req_" + uuid.uuid4().hex[:12]
        started_dt = datetime.now(timezone.utc)
        started = _utc_sqlite(started_dt - timedelta(seconds=2))
        timeout = float(timeout or self.timeout)
        session_id = str(continuation.get("sessionId") or "")
        await emit("accepted", {
            "request_id": request_id,
            "session_id": session_id,
            "approval": True,
        })
        stop = asyncio.Event()
        watcher = asyncio.create_task(
            self.watch_thread(session_id or None, started, emit, stop, text="")
        )
        request = {
            "type": "approval",
            "approved": bool(approved),
            "continuation": continuation,
        }
        call = asyncio.create_task(self._mcp_call(
            "call_agent",
            {"agentId": self.agent_id, "request": request},
            timeout + 10,
        ))
        try:
            result = await asyncio.wait_for(call, timeout)
        except Exception as exc:  # noqa: BLE001
            stop.set()
            try:
                await asyncio.wait_for(watcher, 3)
            except Exception:
                watcher.cancel()
            await emit("error", {"request_id": request_id, "message": str(exc)[:500]})
            return
        stop.set()
        try:
            await asyncio.wait_for(watcher, 3)
        except Exception:
            watcher.cancel()
        status = str((result or {}).get("status") or "").lower()
        if status == "completed":
            await emit("response", {
                "request_id": request_id,
                "session_id": str((result or {}).get("sessionId") or session_id),
                "execution_id": (result or {}).get("executionId"),
                "text": str((result or {}).get("response") or ""),
            })
            await emit("done", {"request_id": request_id, "status": "completed"})
        else:
            await emit("error", {
                "request_id": request_id,
                "message": str((result or {}).get("message") or "approval resume failed")[:500],
            })
