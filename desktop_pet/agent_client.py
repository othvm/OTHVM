"""HTTP/SSE client for the OTHVM agent bridge.

The desktop pet is a *view* over the existing agent.  It never reasons, calls
tools, or writes memory itself: it posts a message to the MCP server, which
calls n8n's published ``OTHVM`` agent, and consumes the real event stream that
the bridge derives from n8n's message log.

Two streams are used:

* ``POST /agent/chat`` -- the request-scoped stream (accepted, tool, response,
  approval, done).
* ``GET /events`` -- the server-wide stream for other real tool activity, used
  to animate background execution.

The server republishes chat events on ``/events`` as well, so consumers must
deduplicate by ``session_id``.  This client tags every event with its source;
the application does the deduplication.

No API keys are logged.  Errors are reduced to a short message.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import httpx

log = logging.getLogger("othvm.pet.client")

EventCallback = Callable[[str, dict], None]
ConnectionCallback = Callable[[str, bool, str], None]


class AgentClientError(RuntimeError):
    """Raised for a transport or protocol failure."""


def _iter_sse(response: httpx.Response):
    """Yield JSON payloads from an SSE response.

    Handles multi-line ``data:`` fields, comments/keepalives, and the named
    ``event:`` field.  The server always includes ``type`` in the JSON body, so
    the event name is only a fallback.
    """
    event_name = ""
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {"raw": raw[:4000]}
                if isinstance(payload, dict):
                    payload.setdefault("type", event_name or "message")
                else:
                    payload = {"type": event_name or "message", "data": payload}
                yield payload
            event_name = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw[:4000]}
        if isinstance(payload, dict):
            payload.setdefault("type", event_name or "message")
        else:
            payload = {"type": event_name or "message", "data": payload}
        yield payload


class AgentClient:
    """Threaded client for the MCP agent bridge."""

    def __init__(self, base_url: str, api_key: str,
                 on_event: EventCallback,
                 on_connection: ConnectionCallback | None = None,
                 connect_timeout: float = 15.0):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self._on_event = on_event
        self._on_connection = on_connection or (lambda *_: None)
        self.connect_timeout = float(connect_timeout)
        self.session_id: str | None = None
        self._stop = threading.Event()
        self._chat_lock = threading.Lock()
        self._global_thread: threading.Thread | None = None
        self._global_connected = False

    # ------------------------------------------------------------------ helpers
    def _headers(self) -> dict:
        return {
            "X-API-Key": self.api_key,
            "Accept": "application/json, text/event-stream",
        }

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.connect_timeout, read=None, write=30.0, pool=15.0)

    def _emit(self, source: str, event: dict) -> None:
        try:
            self._on_event(source, event)
        except Exception:
            log.exception("event callback failed")

    def _connection(self, source: str, connected: bool, detail: str = "") -> None:
        try:
            self._on_connection(source, connected, detail)
        except Exception:
            log.exception("connection callback failed")

    # ------------------------------------------------------------------ status
    def status(self, timeout: float = 12.0) -> dict:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(self.base_url + "/agent/status",
                                  headers=self._headers())
        if response.status_code != 200:
            raise AgentClientError(
                f"status HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        if not isinstance(data, dict):
            raise AgentClientError("status returned an unexpected payload")
        return data

    # ------------------------------------------------------------------ global
    def start(self) -> None:
        """Start the background ``/events`` listener."""
        if self._global_thread and self._global_thread.is_alive():
            return
        self._stop.clear()
        self._global_thread = threading.Thread(
            target=self._global_loop, name="othvm-pet-events", daemon=True)
        self._global_thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._global_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _global_loop(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                with httpx.Client(timeout=self._timeout()) as client:
                    with client.stream("GET", self.base_url + "/events",
                                       headers=self._headers()) as response:
                        if response.status_code != 200:
                            body = response.read().decode("utf-8", "replace")[:300]
                            if response.status_code in (401, 403):
                                self._global_connected = False
                                self._connection("global", False,
                                                 f"HTTP {response.status_code}: {body}")
                                return
                            raise AgentClientError(
                                f"events HTTP {response.status_code}: {body}")
                        self._global_connected = True
                        self._connection("global", True, "")
                        delay = 1.0
                        for event in _iter_sse(response):
                            if self._stop.is_set():
                                break
                            self._emit("global", event)
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                self._global_connected = False
                self._connection("global", False, f"{type(exc).__name__}: {exc}"[:300])
                log.warning("events stream disconnected: %s", exc)
            if self._stop.is_set():
                break
            self._stop.wait(delay)
            delay = min(delay * 2.0, 30.0)

    # -------------------------------------------------------------------- chat
    @property
    def busy(self) -> bool:
        return self._chat_lock.locked()

    def chat(self, message: str, session_id: str | None = None) -> bool:
        """Start a chat request in a worker thread.  Returns False if busy."""
        message = str(message or "").strip()
        if not message:
            return False
        if not self._chat_lock.acquire(blocking=False):
            self._emit("chat", {
                "type": "error",
                "message": "الطلب السابق ما زال قيد التنفيذ.",
                "local": True,
            })
            return False
        payload = {"message": message}
        sid = session_id or self.session_id
        if sid:
            payload["session_id"] = sid
        thread = threading.Thread(
            target=self._chat_worker, args=(payload,), name="othvm-pet-chat",
            daemon=True)
        thread.start()
        return True

    def _chat_worker(self, payload: dict) -> None:
        try:
            self._stream("POST", "/agent/chat", payload)
        finally:
            self._chat_lock.release()

    def approve(self, continuation: dict, approved: bool) -> bool:
        """Resume a suspended agent request with an explicit approval."""
        if not isinstance(continuation, dict):
            self._emit("chat", {
                "type": "error",
                "message": "لا توجد بيانات موافقة صالحة.",
                "local": True,
            })
            return False
        if not self._chat_lock.acquire(blocking=False):
            self._emit("chat", {
                "type": "error",
                "message": "هناك طلب قيد التنفيذ بالفعل.",
                "local": True,
            })
            return False
        payload = {
            "request_type": "approval",
            "continuation": continuation,
            "approved": bool(approved),
        }
        thread = threading.Thread(
            target=self._approve_worker, args=(payload,),
            name="othvm-pet-approval", daemon=True)
        thread.start()
        return True

    def _approve_worker(self, payload: dict) -> None:
        try:
            self._stream("POST", "/agent/chat", payload)
        finally:
            self._chat_lock.release()

    def _stream(self, method: str, path: str, payload: dict | None) -> None:
        url = self.base_url + path
        try:
            with httpx.Client(timeout=self._timeout()) as client:
                with client.stream(method, url, headers=self._headers(),
                                   json=payload) as response:
                    if response.status_code != 200:
                        body = response.read().decode("utf-8", "replace")[:500]
                        self._emit("chat", {
                            "type": "error",
                            "message": f"HTTP {response.status_code}: {body}",
                            "transport": True,
                        })
                        return
                    for event in _iter_sse(response):
                        if self._stop.is_set():
                            break
                        etype = str(event.get("type") or "")
                        sid = event.get("session_id")
                        if sid:
                            self.session_id = str(sid)
                        self._emit("chat", event)
                        if etype in ("done", "error") and not event.get("transport"):
                            # The server closes the stream after these; keep
                            # reading is harmless but unnecessary.
                            pass
        except Exception as exc:  # noqa: BLE001
            if self._stop.is_set():
                return
            log.warning("chat stream failed: %s", exc)
            self._emit("chat", {
                "type": "error",
                "message": f"انقطع الاتصال بالخادم: {type(exc).__name__}: {exc}"[:400],
                "transport": True,
            })

    # ------------------------------------------------------------------- health
    def wait_until_connected(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._global_connected:
                return True
            time.sleep(0.1)
        return self._global_connected
