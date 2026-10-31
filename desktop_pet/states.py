"""Pet state machine driven only by real telemetry.

States change when the MCP server or the n8n bridge reports an event.  There is
no timer that invents progress: a timeout only returns the pet to idle after a
real event has gone stale.
"""
from __future__ import annotations

import time
from typing import Any, Callable

IDLE = "IDLE"
LISTENING = "LISTENING"
THINKING = "THINKING"
RESEARCHING = "RESEARCHING"
CODING = "CODING"
EXECUTING = "EXECUTING"
WORKING = "WORKING"
SPEAKING = "SPEAKING"
SUCCESS = "SUCCESS"
ERROR = "ERROR"
APPROVAL = "APPROVAL"

ALL_STATES = (
    IDLE, LISTENING, THINKING, RESEARCHING, CODING, EXECUTING,
    WORKING, SPEAKING, SUCCESS, ERROR, APPROVAL,
)

STATE_META: dict[str, dict[str, Any]] = {
    IDLE: {"label_ar": "جاهز", "label_en": "idle", "color": "#6ec6ff", "effect": "breathe"},
    LISTENING: {"label_ar": "أسمعك", "label_en": "listening", "color": "#59e0b0", "effect": "mic"},
    THINKING: {"label_ar": "أفكر", "label_en": "thinking", "color": "#a78bfa", "effect": "dots"},
    RESEARCHING: {"label_ar": "أبحث", "label_en": "researching", "color": "#38bdf8", "effect": "orbit"},
    CODING: {"label_ar": "أكتب كود", "label_en": "coding", "color": "#22d3ee", "effect": "code"},
    EXECUTING: {"label_ar": "أنفّذ", "label_en": "executing", "color": "#f59e0b", "effect": "sparks"},
    WORKING: {"label_ar": "أشتغل", "label_en": "working", "color": "#818cf8", "effect": "gear"},
    SPEAKING: {"label_ar": "أتحدث", "label_en": "speaking", "color": "#f472b6", "effect": "waves"},
    SUCCESS: {"label_ar": "تم", "label_en": "done", "color": "#34d399", "effect": "sparkle"},
    ERROR: {"label_ar": "خطأ", "label_en": "error", "color": "#f87171", "effect": "alert"},
    APPROVAL: {"label_ar": "موافقة مطلوبة", "label_en": "approval", "color": "#fbbf24", "effect": "approval"},
}

TOOL_STATES = {RESEARCHING, CODING, EXECUTING, WORKING}
TRANSIENT_STATES = {SUCCESS, ERROR}


def _state_or_default(value: Any) -> str:
    state = str(value or "").strip().upper()
    return state if state in ALL_STATES else WORKING


class StateMachine:
    """Small, deterministic state machine with explicit provenance.

    ``last_reason`` records the event that caused the current state so the
    control panel can explain *why* the pet is animating.
    """

    def __init__(self, on_change: Callable[[str, str, str], None] | None = None):
        self.state = IDLE
        self.previous = IDLE
        self.last_reason = "startup"
        self.request_active = False
        self.tool_state: str | None = None
        self.tool_until = 0.0
        self.transient_until = 0.0
        self.approval: dict | None = None
        self.pending_completion = False
        self._on_change = on_change

    # ------------------------------------------------------------------ helpers
    def _set(self, state: str, reason: str, ttl: float = 0.0) -> str:
        state = _state_or_default(state)
        if state != self.state:
            if self.state not in TRANSIENT_STATES:
                self.previous = self.state
            self.state = state
            self.last_reason = reason
            if self._on_change:
                try:
                    self._on_change(state, self.previous, reason)
                except Exception:
                    pass
        elif reason:
            self.last_reason = reason
        if ttl:
            self.transient_until = time.monotonic() + ttl
        return self.state

    def _resume(self, reason: str) -> str:
        now = time.monotonic()
        if self.approval is not None:
            return self._set(APPROVAL, reason)
        if self.tool_state and now < self.tool_until:
            return self._set(self.tool_state, reason)
        if self.request_active:
            return self._set(THINKING, reason)
        return self._set(IDLE, reason)

    # ------------------------------------------------------------------ events
    def handle_event(self, event: dict, source: str = "agent") -> str:
        """Apply one real event.  Returns the resulting state."""
        if not isinstance(event, dict):
            return self.state
        etype = str(event.get("type") or "").lower()
        now = time.monotonic()

        if etype in ("accepted",):
            self.request_active = True
            self.approval = None
            self.tool_state = None
            self.pending_completion = False
            return self._set(THINKING, "request accepted")

        if etype in ("thinking", "reasoning"):
            return self._set(THINKING, "agent reasoning")

        if etype in ("tool", "action"):
            if self.state == APPROVAL:
                return self.state
            state = _state_or_default(event.get("state"))
            ok = event.get("ok")
            if state == ERROR or ok is False:
                self.tool_state = None
                return self._set(ERROR, "tool failed", ttl=4.0)
            if state not in TOOL_STATES:
                state = WORKING
            self.tool_state = state
            self.tool_until = now + (6.0 if source == "global" else 12.0)
            reason = str(event.get("reason") or event.get("tool")
                         or event.get("action") or "tool call")
            return self._set(state, f"tool: {reason}")

        if etype in ("assistant_text", "response"):
            if self.state == APPROVAL:
                return self.state
            return self._set(WORKING if self.request_active else IDLE,
                             "assistant text")

        if etype in ("approval", "approval_required"):
            self.request_active = True
            self.approval = event
            return self._set(APPROVAL, "approval required")

        if etype in ("done", "completed"):
            status = str(event.get("status") or "").lower()
            if status in ("suspended", "approval_required") or self.approval is not None:
                return self._set(APPROVAL, "waiting for approval")
            self.request_active = False
            self.tool_state = None
            if status in ("completed", "success", "ok"):
                if self.state == SPEAKING:
                    # TTS owns the animation; show success when it finishes.
                    self.pending_completion = True
                    return self._set(SPEAKING, "request completed during speech")
                return self._set(SUCCESS, "request completed", ttl=2.8)
            return self._resume("request done")

        if etype in ("error", "failed"):
            self.request_active = False
            self.approval = None
            self.tool_state = None
            return self._set(ERROR, str(event.get("message") or "error"), ttl=4.5)

        if etype in ("listening", "mic_start"):
            return self._set(LISTENING, "microphone")

        if etype in ("mic_stop", "transcribing"):
            return self._set(THINKING, "transcribing")

        if etype in ("speaking_start",):
            return self._set(SPEAKING, "tts")

        if etype in ("speaking_end",):
            if self.pending_completion:
                self.pending_completion = False
                return self._set(SUCCESS, "request completed", ttl=2.8)
            return self._resume("tts finished")

        if etype in ("idle", "reset"):
            self.request_active = False
            self.approval = None
            self.tool_state = None
            self.pending_completion = False
            return self._set(IDLE, "reset")

        return self.state

    def approve(self) -> None:
        self.approval = None
        self.request_active = True
        self._set(THINKING, "approval granted")

    def deny(self) -> None:
        self.approval = None
        self.request_active = False
        self._set(IDLE, "approval denied")

    def tick(self, now: float | None = None) -> str:
        """Expire stale states; never invents a new activity."""
        now = now or time.monotonic()
        if self.state == APPROVAL:
            return self.state
        if self.state in TRANSIENT_STATES and now >= self.transient_until:
            if self.state == ERROR:
                return self._resume("error cleared")
            return self._resume("success cleared")
        if (self.state in TOOL_STATES and self.tool_state
                and now >= self.tool_until):
            self.tool_state = None
            return self._resume("tool activity stale")
        return self.state
