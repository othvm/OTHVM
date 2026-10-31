"""OTHVM desktop pet application.

The pet is a transparent Win32 character plus a small Tkinter control panel.
It is a *view* over the existing n8n agent:

* text/voice messages go to ``POST /agent/chat``;
* execution states come from real tool-call records in n8n's message log;
* background activity comes from the MCP server's ``/events`` stream;
* approvals are resumed through the same bridge, never re-implemented here.

Run:
    pythonw desktop_pet/app.py
    python  desktop_pet/app.py --hidden
    python  desktop_pet/app.py --no-pet      # panel only, for debugging
"""
from __future__ import annotations

import argparse
import logging
import queue
import re
import sys
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Allow ``python desktop_pet/app.py`` as well as ``python -m desktop_pet.app``.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_pet.agent_client import AgentClient  # noqa: E402
from desktop_pet.config import (  # noqa: E402
    ARTIFACT_DIR, ASSET_DIR, CACHE_DIR, LOG_DIR, api_key, ensure_dirs,
    gemini_key, load_config, save_config,
)
from desktop_pet.effects import PetRenderer  # noqa: E402
from desktop_pet.layered import LayeredPet, enable_dpi_awareness  # noqa: E402
from desktop_pet.states import (  # noqa: E402
    APPROVAL, ERROR, IDLE, SPEAKING, STATE_META, SUCCESS, StateMachine,
)
from desktop_pet.voice import (  # noqa: E402
    AudioRecorder, SpeechEngine, SpeechToText, VoiceError,
)

log = logging.getLogger("othvm.pet.app")

# ------------------------------------------------------------------ palette
BG = "#0b0f19"
PANEL = "#121826"
CARD = "#0f1522"
CARD_2 = "#171f30"
TEXT = "#e8eef9"
MUTED = "#8b98ad"
ACCENT = "#6ec6ff"
GREEN = "#34d399"
RED = "#f87171"
YELLOW = "#fbbf24"
PINK = "#f472b6"

FONT = "Segoe UI"
FONT_BOLD = (FONT, 10, "bold")
FONT_SMALL = (FONT, 8)
FONT_STATE = (FONT, 18, "bold")
FONT_TITLE = (FONT, 17, "bold")


def _button(parent: tk.Misc, text: str, command, *, color: str = ACCENT,
            fg: str = BG, width: int | None = None) -> tk.Button:
    return tk.Button(
        parent, text=text, command=command, bg=color, fg=fg,
        activebackground=color, activeforeground=fg, relief="flat", bd=0,
        padx=12, pady=6, cursor="hand2", font=FONT_BOLD,
        width=width, highlightthickness=0,
    )


def _check(parent: tk.Misc, text: str, variable: tk.BooleanVar,
           command=None) -> tk.Checkbutton:
    return tk.Checkbutton(
        parent, text=text, variable=variable, command=command,
        bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
        selectcolor=CARD_2, font=(FONT, 9), anchor="w",
        highlightthickness=0, bd=0,
    )


# ---------------------------------------------------------------------------
# Shared runtime (window thread reads; Tk thread writes)
# ---------------------------------------------------------------------------
class PetRuntime:
    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.RLock()
        self.state = IDLE
        self.reason = "startup"
        self.bubble = ""
        self.bubble_until = 0.0
        self.events: queue.Queue = queue.Queue()
        self.error = ""
        self.sm = StateMachine(on_change=self._on_state)

        master = ASSET_DIR / "pet_master.png"
        if not master.exists():
            master = ASSET_DIR / "pet_idle.png"
        if not master.exists():
            raise FileNotFoundError(
                f"missing pet asset: {ASSET_DIR / 'pet_master.png'}")
        width = int(config.get("window_width") or 380)
        height = int(config.get("window_height") or 470)
        self.renderer = PetRenderer(
            master, width=width, height=height,
            scale=float(config.get("pet_scale") or 1.0),
            show_state_label=bool(config.get("show_state_label", True)),
        )
        position = config.get("position") if isinstance(config.get("position"), dict) else {}
        self.window = LayeredPet(
            self.render_frame, width, height,
            x=position.get("x"), y=position.get("y"),
            always_on_top=bool(config.get("always_on_top", True)),
            event_queue=self.events,
            frame_ms=int(config.get("frame_ms") or 40),
        )
        self.started = False

    def _on_state(self, state: str, previous: str, reason: str) -> None:
        log.debug("state %s -> %s (%s)", previous, state, reason)
        with self.lock:
            self.state = state
            self.reason = reason

    def render_frame(self):
        with self.lock:
            state = self.state
            bubble = self.bubble if time.monotonic() < self.bubble_until else ""
        return self.renderer.render(state, bubble, time.monotonic())

    def set_bubble(self, text: str, seconds: float = 6.0) -> None:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        with self.lock:
            self.bubble = value[:260]
            self.bubble_until = time.monotonic() + max(0.5, float(seconds)) if value else 0.0

    def start_window(self) -> None:
        self.window.start()
        self.started = True

    def stop(self) -> None:
        try:
            self.window.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
class PetApp:
    def __init__(self, root: tk.Tk, config: dict, *, hidden: bool = False,
                 no_pet: bool = False):
        self.root = root
        self.config = config
        self.no_pet = bool(no_pet)
        self._closing = False
        self._hidden = bool(hidden or config.get("start_hidden"))
        self._recording = False
        self._pet_visible = bool(config.get("pet_visible", True))
        self._approval_continuation: dict | None = None
        self._approval_visible = False
        self._chat_sessions: dict[str, float] = {}
        self._last_assistant_text = ""
        self._last_state = ""
        self._last_conn: tuple[bool, str] | None = None
        self._save_after: str | None = None
        self._stt_thread: threading.Thread | None = None
        self.ui_queue: queue.Queue = queue.Queue()

        self.runtime = PetRuntime(config)
        self.client = AgentClient(
            str(config.get("mcp_base_url") or "http://127.0.0.1:8765"),
            api_key(),
            on_event=self._client_event,
            on_connection=self._client_connection,
        )
        self.recorder = AudioRecorder(
            samplerate=16000, channels=1,
            device=config.get("input_device") or None,
        )
        self.stt = SpeechToText(config.get("stt") or {}, gemini_key())
        self.tts = SpeechEngine(
            config.get("tts") or {}, CACHE_DIR, on_event=self._voice_event)

        self._build_ui()
        self._bind_root()
        self._start_background()
        if self._hidden:
            self.root.withdraw()
        else:
            self.show_panel()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.root.title("OTHVM — Desktop Pet")
        self.root.configure(bg=BG)
        self.root.geometry("510x720")
        self.root.minsize(450, 620)
        try:
            icon = tk.PhotoImage(file=str(ASSET_DIR / "pet_icon.png"))
            self.root.iconphoto(True, icon)
            self._icon = icon
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.hide_panel)

        outer = tk.Frame(self.root, bg=BG, padx=14, pady=12)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", pady=(0, 10))
        tk.Label(header, text="OTHVM", font=FONT_TITLE, bg=BG, fg=TEXT,
                 anchor="w").pack(side="left")
        tk.Label(header, text="DESKTOP PET", font=(FONT, 8, "bold"),
                 bg=BG, fg=MUTED, anchor="w").pack(side="left", padx=(8, 0),
                                                     pady=(6, 0))
        self.conn_dot = tk.Label(header, text="●", font=(FONT, 12), bg=BG,
                                 fg=YELLOW)
        self.conn_dot.pack(side="right", padx=(6, 0))
        self.conn_label = tk.Label(header, text="events: connecting",
                                   font=FONT_SMALL, bg=BG, fg=MUTED)
        self.conn_label.pack(side="right")

        card = tk.Frame(outer, bg=CARD, padx=14, pady=10,
                        highlightthickness=1, highlightbackground="#1e2a40")
        card.pack(fill="x", pady=(0, 10))
        self.state_label = tk.Label(card, text="جاهز · idle", font=FONT_STATE,
                                    bg=CARD, fg=ACCENT, anchor="w")
        self.state_label.pack(fill="x")
        self.reason_label = tk.Label(card, text="startup", font=(FONT, 9),
                                     bg=CARD, fg=MUTED, anchor="w",
                                     justify="left", wraplength=440)
        self.reason_label.pack(fill="x", pady=(2, 0))
        tk.Label(card,
                 text="Ctrl+Alt+Space talk  ·  Ctrl+Alt+H panel  ·  Ctrl+Alt+O pet",
                 font=(FONT, 8), bg=CARD, fg="#5f6f86", anchor="w").pack(
            fill="x", pady=(4, 0))

        chat_head = tk.Frame(outer, bg=BG)
        chat_head.pack(fill="x")
        tk.Label(chat_head, text="المحادثة", font=(FONT, 9, "bold"),
                 bg=BG, fg=TEXT, anchor="w").pack(side="left")
        self.clear_btn = tk.Button(
            chat_head, text="مسح", command=self._clear_transcript,
            bg=BG, fg=MUTED, activebackground=BG, activeforeground=TEXT,
            relief="flat", bd=0, cursor="hand2", font=FONT_SMALL)
        self.clear_btn.pack(side="right")

        text_frame = tk.Frame(outer, bg=CARD)
        text_frame.pack(fill="both", expand=True, pady=(4, 8))
        scroll = tk.Scrollbar(text_frame, orient="vertical")
        scroll.pack(side="right", fill="y")
        self.transcript = tk.Text(
            text_frame, height=13, wrap="word", bg=CARD, fg=TEXT,
            insertbackground=TEXT, relief="flat", bd=0, padx=10, pady=8,
            font=(FONT, 9), spacing1=2, spacing3=4,
            yscrollcommand=scroll.set, state="disabled",
        )
        self.transcript.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.transcript.yview)
        self.transcript.tag_configure("user", foreground="#7dd3fc",
                                      font=(FONT, 9, "bold"))
        self.transcript.tag_configure("assistant", foreground=TEXT)
        self.transcript.tag_configure("system", foreground=MUTED,
                                      font=(FONT, 8, "italic"))
        self.transcript.tag_configure("error", foreground=RED,
                                      font=(FONT, 9, "bold"))
        self.transcript.tag_configure("approval", foreground=YELLOW,
                                      font=(FONT, 9, "bold"))

        entry_row = tk.Frame(outer, bg=BG)
        entry_row.pack(fill="x", pady=(0, 8))
        self.entry = tk.Entry(
            entry_row, bg=CARD_2, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0, font=(FONT, 10),
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 6))
        self.entry.bind("<Return>", lambda _e: self._send())
        self.send_btn = _button(entry_row, "إرسال", self._send, width=6)
        self.send_btn.pack(side="left", padx=(0, 4))
        self.talk_btn = _button(entry_row, "🎤 تكلم", self._toggle_record,
                                color=GREEN, width=8)
        self.talk_btn.pack(side="left")

        self.approval_frame = tk.Frame(
            outer, bg="#2a2110", padx=12, pady=10,
            highlightthickness=1, highlightbackground=YELLOW)
        self.approval_label = tk.Label(
            self.approval_frame, text="", bg="#2a2110", fg=YELLOW,
            font=(FONT, 9), anchor="w", justify="left", wraplength=430)
        self.approval_label.pack(fill="x")
        approval_buttons = tk.Frame(self.approval_frame, bg="#2a2110")
        approval_buttons.pack(fill="x", pady=(6, 0))
        self.approve_btn = _button(approval_buttons, "موافقة",
                                   lambda: self._approve(True), color=GREEN,
                                   width=8)
        self.approve_btn.pack(side="left", padx=(0, 6))
        self.deny_btn = _button(approval_buttons, "رفض",
                                lambda: self._approve(False), color=RED,
                                fg=TEXT, width=8)
        self.deny_btn.pack(side="left")

        controls = tk.Frame(outer, bg=BG)
        controls.pack(fill="x", pady=(0, 6))
        self.tts_var = tk.BooleanVar(
            value=bool((self.config.get("tts") or {}).get("enabled", True)))
        self.top_var = tk.BooleanVar(
            value=bool(self.config.get("always_on_top", True)))
        _check(controls, "نطق الردود", self.tts_var,
               self._toggle_tts).pack(side="left")
        _check(controls, "فوق النوافذ", self.top_var,
               self._toggle_top).pack(side="left", padx=(8, 0))
        self.pet_btn = _button(controls, "إخفاء الشخصية", self._toggle_pet,
                               color=CARD_2, fg=TEXT, width=12)
        self.pet_btn.pack(side="right")
        self.new_btn = _button(controls, "محادثة جديدة", self._new_session,
                               color=CARD_2, fg=TEXT, width=12)
        self.new_btn.pack(side="right", padx=(0, 6))

        status = tk.Frame(outer, bg=BG)
        status.pack(fill="x", pady=(4, 0))
        self.agent_label = tk.Label(
            status, text="agent: checking…", font=FONT_SMALL, bg=BG,
            fg=MUTED, anchor="w")
        self.agent_label.pack(fill="x")
        self.voice_label = tk.Label(
            status, text=self._voice_description(), font=FONT_SMALL,
            bg=BG, fg=MUTED, anchor="w")
        self.voice_label.pack(fill="x")
        self.status_label = tk.Label(
            status, text="", font=FONT_SMALL, bg=BG, fg=MUTED, anchor="w",
            justify="left", wraplength=460)
        self.status_label.pack(fill="x")

        self._append("system", "جاهز. اكتب رسالة أو اضغط 🎤 تكلم.")
        if not api_key():
            self._append("error",
                         "مفتاح MCP غير موجود. اضبط mcp_server/config.json أولًا.")

    def _bind_root(self) -> None:
        self.root.bind("<Control-Return>", lambda _e: self._send())
        self.root.report_callback_exception = self._tk_error

    def _tk_error(self, exc_type, exc, tb) -> None:
        log.error("Tk callback failed", exc_info=(exc_type, exc, tb))
        try:
            self._append("error", f"UI error: {exc_type.__name__}: {exc}")
        except Exception:
            pass

    def _voice_description(self) -> str:
        stt = self.stt.describe()
        tts = self.tts.describe()
        return f"STT: {stt}  ·  TTS: {tts}"

    # ------------------------------------------------------------------ start
    def _start_background(self) -> None:
        if not self.no_pet:
            try:
                self.runtime.start_window()
                if not self._pet_visible:
                    self.runtime.window.set_visible(False)
                    self.pet_btn.config(text="إظهار الشخصية")
            except Exception as exc:  # noqa: BLE001
                log.exception("pet window failed")
                self.runtime.error = f"{type(exc).__name__}: {exc}"
                self._append("error",
                             f"تعذّر إنشاء نافذة الشخصية: {self.runtime.error}")
                self.pet_btn.config(state="disabled")
        else:
            self.pet_btn.config(state="disabled")
        try:
            if self.stt.preload():
                log.info("local STT preload started")
        except Exception:  # noqa: BLE001
            log.exception("local STT preload could not start")
        if not api_key():
            self.agent_label.config(text="agent: MCP key missing", fg=RED)
        else:
            threading.Thread(target=self._status_worker, name="othvm-pet-status",
                             daemon=True).start()
            self.client.start()
        self.root.after(80, self._drain)
        self.root.after(100, self._tick)

    def _status_worker(self) -> None:
        try:
            data = self.client.status()
        except Exception as exc:  # noqa: BLE001
            self.ui_queue.put(("agent_status", {
                "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}))
            return
        self.ui_queue.put(("agent_status", data))

    # ------------------------------------------------------------------ queues
    def _client_event(self, source: str, event: dict) -> None:
        self.ui_queue.put(("event", (source, event)))

    def _client_connection(self, source: str, connected: bool,
                           detail: str) -> None:
        self.ui_queue.put(("connection", (source, connected, detail)))

    def _voice_event(self, kind: str, data: dict) -> None:
        self.ui_queue.put(("voice", (kind, data)))

    def _drain(self) -> None:
        if self._closing:
            return
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "event":
                    source, event = payload
                    self._handle_event(source, event)
                elif kind == "connection":
                    source, connected, detail = payload
                    self._handle_connection(source, connected, detail)
                elif kind == "voice":
                    voice_kind, data = payload
                    self._handle_voice(voice_kind, data)
                elif kind == "stt":
                    self._handle_stt(payload)
                elif kind == "stt_error":
                    self._append("error", f"STT: {payload}")
                    self.runtime.sm.handle_event({"type": "idle"})
                    self.runtime.set_bubble("تعذّر تفريغ الصوت", 4)
                elif kind == "agent_status":
                    self._handle_agent_status(payload)
            except Exception:
                log.exception("queue handler failed")
        self.root.after(70, self._drain)

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        if self._closing:
            return
        self.runtime.sm.tick()
        state = self.runtime.sm.state
        if state != self._last_state:
            self._last_state = state
            self._update_state_ui(state)
        if self._recording:
            duration = self.recorder.duration
            self.talk_btn.config(text=f"■ إيقاف {duration:.0f}s")
            max_seconds = int((self.config.get("stt") or {}).get("max_seconds") or 30)
            if duration >= max_seconds:
                self._stop_recording()
        while True:
            try:
                kind, payload = self.runtime.events.get_nowait()
            except queue.Empty:
                break
            self._handle_window_event(kind, payload)
        self.root.after(100, self._tick)

    def _update_state_ui(self, state: str) -> None:
        meta = STATE_META.get(state) or STATE_META[IDLE]
        color = str(meta.get("color") or ACCENT)
        label_ar = str(meta.get("label_ar") or state)
        label_en = str(meta.get("label_en") or state.lower())
        self.state_label.config(text=f"{label_ar} · {label_en}", fg=color)
        reason = str(self.runtime.sm.last_reason or "")
        self.reason_label.config(text=reason[:240] or "—")

    # ------------------------------------------------------------------ events
    def _handle_event(self, source: str, event: dict) -> None:
        if not isinstance(event, dict):
            return
        # Metadata only: never log message text or tool payloads.
        log.debug("event source=%s %s", source, {
            key: event.get(key) for key in
            ("type", "session_id", "tool", "state", "reason", "action",
             "category", "status", "ok")
            if event.get(key) is not None
        })
        if source == "global":
            self._handle_global_event(event)
        else:
            self._handle_chat_event(event)

    def _session_seen(self, event: dict) -> str:
        sid = str(event.get("session_id") or "").strip()
        if sid:
            self._chat_sessions[sid] = time.time()
            self.client.session_id = sid
        return sid

    def _handle_chat_event(self, event: dict) -> None:
        etype = str(event.get("type") or "").lower()
        self._session_seen(event)

        if etype == "accepted":
            self.runtime.sm.handle_event(event)
            self._last_assistant_text = ""
        elif etype == "thread":
            sid = str(event.get("session_id") or "")
            if sid:
                self._append("system", f"session: {sid[:12]}")
        elif etype == "thinking":
            self.runtime.sm.handle_event(event)
        elif etype == "tool":
            self.runtime.sm.handle_event(event)
            state = self.runtime.sm.state
            reason = str(event.get("reason") or event.get("tool") or "tool")
            if state == ERROR:
                self.runtime.set_bubble(f"فشل: {reason}", 4)
            else:
                self.runtime.set_bubble(reason[:120], 2.4)
        elif etype == "assistant_text":
            text = str(event.get("text") or "").strip()
            self.runtime.sm.handle_event(event)
            if text and text != self._last_assistant_text:
                self._append("assistant", text)
                self._last_assistant_text = text
            if text:
                self.runtime.set_bubble(text[:220], 6)
        elif etype == "response":
            text = str(event.get("text") or "").strip()
            self.runtime.sm.handle_event(event)
            if text:
                if text != self._last_assistant_text:
                    self._append("assistant", text)
                self._last_assistant_text = text
                self.runtime.set_bubble(text[:240], 10)
                self._speak(text)
            self._hide_approval()
        elif etype == "approval":
            self._approval_continuation = (
                event.get("continuation")
                if isinstance(event.get("continuation"), dict) else None)
            self._show_approval(event)
            self.runtime.sm.handle_event(event)
            prompt = str(event.get("prompt") or "موافقة مطلوبة")
            self.runtime.set_bubble(prompt[:240], 20)
            self._speak("موافقة مطلوبة. " + prompt)
        elif etype == "approval_required":
            self._approval_continuation = None
            self._show_approval(event)
            self.runtime.sm.handle_event(event)
            note = str(event.get("note") or
                       "الموافقة لازم تتم من n8n Preview chat.")
            self._append("approval", note)
            self.runtime.set_bubble(note[:240], 20)
        elif etype == "done":
            status = str(event.get("status") or "").lower()
            # The state machine defers SUCCESS until TTS finishes, so the
            # completion event must always be applied.
            self.runtime.sm.handle_event(event)
            if status == "completed":
                self._hide_approval()
                self.runtime.set_bubble("تم", 3)
            elif status not in ("suspended", "approval_required"):
                self._hide_approval()
            self._schedule_session_cleanup(str(event.get("session_id") or ""))
        elif etype in ("error", "failed"):
            self.runtime.sm.handle_event(event)
            message = str(event.get("message") or "خطأ غير معروف")
            self._append("error", message)
            self.runtime.set_bubble(message[:240], 8)
            self._hide_approval()
            self._last_assistant_text = ""
        elif etype == "transport_error":
            self.runtime.sm.handle_event({"type": "error",
                                          "message": event.get("message") or "transport"})

    def _handle_global_event(self, event: dict) -> None:
        if not bool(self.config.get("show_global_activity", True)):
            return
        etype = str(event.get("type") or "").lower()
        if etype != "tool":
            return
        sid = str(event.get("session_id") or "")
        if sid and sid in self._chat_sessions:
            if time.time() - self._chat_sessions[sid] < 90:
                return
        # A foreground chat is authoritative; do not let unrelated background
        # tool calls overwrite its state.
        if self.runtime.sm.request_active or self.runtime.sm.state == APPROVAL:
            return
        self.runtime.sm.handle_event(event, source="global")
        state = self.runtime.sm.state
        action = str(event.get("action") or event.get("category") or "tool")
        if state == ERROR:
            self.runtime.set_bubble(f"فشل: {action}", 4)
        else:
            self.runtime.set_bubble(action[:120], 2.0)

    def _handle_connection(self, source: str, connected: bool,
                           detail: str) -> None:
        if source != "global":
            return
        state = (bool(connected), detail if not connected else "")
        if self._last_conn == state:
            return
        self._last_conn = state
        if connected:
            self.conn_dot.config(fg=GREEN)
            self.conn_label.config(text="events: connected", fg=GREEN)
        else:
            self.conn_dot.config(fg=RED)
            self.conn_label.config(text="events: reconnecting", fg=RED)
            if detail:
                self.status_label.config(text=f"events: {detail}")

    def _handle_agent_status(self, data: dict) -> None:
        if not isinstance(data, dict) or not data.get("ok"):
            error = str((data or {}).get("error") or "unavailable")
            self.agent_label.config(text=f"agent: {error}"[:160], fg=RED)
            return
        configured = bool(data.get("agent_configured"))
        db_ok = bool(data.get("db_readable"))
        color = GREEN if configured else YELLOW
        self.agent_label.config(
            text=(f"agent: {'ready' if configured else 'not configured'}"
                  f"  ·  log: {'readable' if db_ok else 'unreadable'}"
                  f"  ·  id: {str(data.get('agent_id') or '')[:12]}"),
            fg=color)
        if not configured:
            self._append("error",
                         "الـ agent غير مهيأ: راجع agent_id وn8n_mcp_key.")

    def _handle_voice(self, kind: str, data: dict) -> None:
        log.debug("voice %s %s", kind, {
            key: (data or {}).get(key) for key in ("engine", "stopped", "message")
            if (data or {}).get(key) is not None
        })
        if kind == "speaking_start":
            self.runtime.sm.handle_event({"type": "speaking_start"})
        elif kind == "speaking_end":
            self.runtime.sm.handle_event({"type": "speaking_end"})
        elif kind == "tts_error":
            message = str((data or {}).get("message") or "TTS failed")
            self._append("error", f"TTS: {message}")
            if self.runtime.sm.state == SPEAKING:
                self.runtime.sm.handle_event({"type": "speaking_end"})

    def _schedule_session_cleanup(self, sid: str) -> None:
        if not sid:
            return

        def cleanup() -> None:
            self._chat_sessions.pop(sid, None)

        self.root.after(95_000, cleanup)

    # ------------------------------------------------------------------ chat
    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._send_text(text)

    def _send_text(self, text: str, prefix: str = "") -> None:
        text = str(text or "").strip()
        if not text:
            return
        if self._approval_continuation is not None:
            self._append("system",
                         "فيه موافقة معلّقة. وافق أو ارفض الأول.")
            return
        if self.client.busy:
            self._append("system", "الطلب السابق ما زال قيد التنفيذ.")
            return
        if not api_key():
            self._append("error", "مفتاح MCP غير موجود.")
            return
        if not self.client.chat(text):
            return
        self._append("user", f"{prefix}{text}")
        self._last_assistant_text = ""
        self.runtime.set_bubble("", 0)

    def _new_session(self) -> None:
        if self.client.busy:
            self._append("system", "استنى الطلب الحالي يخلص الأول.")
            return
        self.client.session_id = None
        self._last_assistant_text = ""
        self.runtime.sm.handle_event({"type": "idle"})
        self._append("system", "محادثة جديدة.")

    # ------------------------------------------------------------------ voice
    def _toggle_record(self) -> None:
        if self._recording:
            self._stop_recording()
            return
        if self.client.busy:
            self._append("system", "استنى الرد الحالي يخلص.")
            return
        if self.tts.speaking:
            self.tts.stop()
        try:
            self.recorder.start()
        except VoiceError as exc:
            self._append("error", str(exc))
            return
        self._recording = True
        self.talk_btn.config(text="■ إيقاف 0s", bg=RED)
        self.runtime.sm.handle_event({"type": "listening"})
        self.runtime.set_bubble("أتسمعك…", 45)

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self.talk_btn.config(text="🎤 تكلم", bg=GREEN)
        audio = self.recorder.stop()
        if not audio:
            self._append("system", "لم يتم تسجيل صوت.")
            self.runtime.sm.handle_event({"type": "idle"})
            self.runtime.set_bubble("لم أسمع حاجة", 3)
            return
        self.runtime.sm.handle_event({"type": "transcribing"})
        self.runtime.set_bubble("أفرّغ الصوت…", 30)
        self._stt_thread = threading.Thread(
            target=self._stt_worker, args=(audio,), name="othvm-pet-stt",
            daemon=True)
        self._stt_thread.start()

    def _stt_worker(self, audio: bytes) -> None:
        try:
            result = self.stt.transcribe(audio)
        except Exception as exc:  # noqa: BLE001
            self.ui_queue.put(("stt_error", f"{type(exc).__name__}: {exc}"[:400]))
            return
        log.info("stt engine=%s seconds=%s fallback=%s chars=%s",
                 result.get("engine"), result.get("seconds"),
                 bool(result.get("fallback_from")), len(result.get("text") or ""))
        self.ui_queue.put(("stt", result))

    def _handle_stt(self, result: dict) -> None:
        if not isinstance(result, dict):
            return
        text = str(result.get("text") or "").strip()
        engine = str(result.get("engine") or "stt")
        if not text:
            self._append("system", f"لم أسمع كلامًا واضحًا ({engine}).")
            self.runtime.sm.handle_event({"type": "idle"})
            self.runtime.set_bubble("لم أسمعك…", 3)
            return
        wake = self.config.get("wake_word") or {}
        if bool(wake.get("enabled")):
            phrase = str(wake.get("phrase") or "othvm").strip()
            if phrase and phrase.casefold() not in text.casefold():
                self._append("system",
                             f"تم تجاهل الصوت (كلمة التنبيه '{phrase}' غير موجودة).")
                self.runtime.sm.handle_event({"type": "idle"})
                return
            if phrase:
                text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    self.runtime.sm.handle_event({"type": "idle"})
                    return
        self._append("system", f"🎤 {text}")
        if bool((self.config.get("stt") or {}).get("auto_send", True)):
            self._send_text(text, prefix="🎤 ")
        else:
            self.entry.delete(0, "end")
            self.entry.insert(0, text)
            self.entry.focus_set()
            self.runtime.sm.handle_event({"type": "idle"})
            self.runtime.set_bubble("النص جاهز — عدّله أو أرسله", 5)

    def _speak(self, text: str) -> None:
        if not self.tts_var.get():
            return
        try:
            self.tts.speak(text)
        except Exception as exc:  # noqa: BLE001
            self._append("error", f"TTS: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ approval
    def _show_approval(self, event: dict) -> None:
        tool = str(event.get("tool") or event.get("category") or "tool")
        prompt = str(event.get("prompt") or event.get("message")
                     or event.get("note") or "موافقة مطلوبة")
        self.approval_label.config(text=f"{tool}\n{prompt}"[:1200])
        has_continuation = self._approval_continuation is not None
        self.approve_btn.config(state="normal" if has_continuation else "disabled")
        self.deny_btn.config(state="normal" if has_continuation else "disabled")
        if not self._approval_visible:
            self.approval_frame.pack(fill="x", pady=(0, 8), before=self.entry.master)
            self._approval_visible = True

    def _hide_approval(self) -> None:
        if self._approval_visible:
            self.approval_frame.pack_forget()
            self._approval_visible = False
        self._approval_continuation = None

    def _approve(self, approved: bool) -> None:
        continuation = self._approval_continuation
        if not isinstance(continuation, dict):
            self._append("system",
                         "الموافقة دي لازم تتم من n8n Preview chat.")
            return
        self._hide_approval()
        if approved:
            self.runtime.sm.approve()
            self._append("system", "تمت الموافقة.")
        else:
            self.runtime.sm.deny()
            self._append("system", "تم رفض الطلب.")
        if not self.client.approve(continuation, approved):
            self.runtime.sm.handle_event({"type": "idle"})

    # ------------------------------------------------------------------ window events
    def _handle_window_event(self, kind: str, payload: Any) -> None:
        if kind == "click":
            self.show_panel()
        elif kind == "double_click":
            self._toggle_record()
        elif kind == "right_click":
            self.show_panel()
        elif kind == "hotkey":
            if int(payload) == 1:
                self._toggle_record()
            elif int(payload) == 2:
                self._toggle_pet()
            elif int(payload) == 3:
                self._toggle_panel()
        elif kind == "moved":
            try:
                x, y = payload
                self.config["position"] = {"x": int(x), "y": int(y)}
                if self._save_after:
                    self.root.after_cancel(self._save_after)
                self._save_after = self.root.after(800, self._save_config)
            except Exception:
                pass

    # ------------------------------------------------------------------ panel
    def show_panel(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.entry.focus_set()
        except Exception:
            pass

    def hide_panel(self) -> None:
        try:
            self.root.withdraw()
        except Exception:
            pass

    def _toggle_panel(self) -> None:
        if self.root.state() == "withdrawn":
            self.show_panel()
        else:
            self.hide_panel()

    def _toggle_pet(self) -> None:
        if self.no_pet:
            return
        self._pet_visible = not self._pet_visible
        self.runtime.window.set_visible(self._pet_visible)
        self.pet_btn.config(text="إظهار الشخصية" if not self._pet_visible
                            else "إخفاء الشخصية")
        self.config["pet_visible"] = self._pet_visible

    def _toggle_tts(self) -> None:
        enabled = bool(self.tts_var.get())
        self.config.setdefault("tts", {})["enabled"] = enabled
        if not enabled:
            self.tts.stop()
        self._save_config()

    def _toggle_top(self) -> None:
        enabled = bool(self.top_var.get())
        self.config["always_on_top"] = enabled
        self.runtime.window.set_always_on_top(enabled)
        self._save_config()

    def _save_config(self) -> None:
        self._save_after = None
        if self._closing:
            return
        try:
            self.config.setdefault("tts", {})["enabled"] = bool(self.tts_var.get())
            self.config["always_on_top"] = bool(self.top_var.get())
            self.config["pet_visible"] = bool(self._pet_visible)
            save_config(self.config)
        except Exception:
            log.exception("could not save config")

    # ------------------------------------------------------------------ transcript
    def _append(self, kind: str, text: str) -> None:
        value = str(text or "").strip()
        if not value:
            return
        prefix = {
            "user": "أنت: ",
            "assistant": "OTHVM: ",
            "system": "• ",
            "error": "⚠ ",
            "approval": "🔐 ",
        }.get(kind, "")
        try:
            self.transcript.configure(state="normal")
            self.transcript.insert("end", prefix + value + "\n", kind)
            self.transcript.see("end")
            self.transcript.configure(state="disabled")
        except Exception:
            pass

    def _clear_transcript(self) -> None:
        try:
            self.transcript.configure(state="normal")
            self.transcript.delete("1.0", "end")
            self.transcript.configure(state="disabled")
        except Exception:
            pass

    # ------------------------------------------------------------------ quit
    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._save_config()
        try:
            self.tts.stop()
        except Exception:
            pass
        try:
            self.client.stop()
        except Exception:
            pass
        try:
            self.runtime.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def setup_logging(debug: bool = False) -> None:
    ensure_dirs()
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return
    handler = RotatingFileHandler(
        LOG_DIR / "pet.log", maxBytes=1_000_000, backupCount=3,
        encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OTHVM desktop pet")
    parser.add_argument("--hidden", action="store_true",
                        help="start with the control panel hidden")
    parser.add_argument("--no-pet", action="store_true",
                        help="run only the control panel (no layered window)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    enable_dpi_awareness()
    setup_logging(args.debug)
    config = load_config()
    log.info("starting desktop pet (hidden=%s no_pet=%s)", args.hidden, args.no_pet)

    root = tk.Tk()
    try:
        app = PetApp(root, config, hidden=args.hidden, no_pet=args.no_pet)
    except Exception as exc:  # noqa: BLE001
        log.exception("startup failed")
        root.withdraw()
        import tkinter.messagebox as messagebox
        messagebox.showerror("OTHVM Desktop Pet",
                             f"تعذّر بدء التشغيل:\n{type(exc).__name__}: {exc}")
        root.destroy()
        return 1
    root.protocol("WM_DELETE_WINDOW", app.hide_panel)
    try:
        root.mainloop()
    finally:
        app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
