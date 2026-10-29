#!/usr/bin/env python3
"""OTHVM Telegram webhook fast-path.

Deterministic, model-free handling for the two highest-value requests:
  * wake-up / alarm scheduling  -> MCP `loops.ensure_alarm`
  * desktop file creation       -> MCP `computer` `files` `file_write`

Every other update is proxied to n8n unchanged, so the full agent keeps
working exactly as before. The service only replies directly when it has
fully understood and completed the request.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STARTED_AT = time.time()

CFG: dict[str, Any] = {}
CLIENT: httpx.AsyncClient | None = None
COUNTERS = {"updates": 0, "fast": 0, "forwarded": 0, "errors": 0, "duplicates": 0}
SEEN: dict[int, float] = {}


def load_config() -> dict[str, Any]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 5681)
    cfg.setdefault("mcp_base", "http://127.0.0.1:8765")
    cfg.setdefault("n8n_base", "http://127.0.0.1:5678")
    cfg.setdefault("desktop_dir", str(Path.home() / "Desktop"))
    cfg.setdefault("dedupe_seconds", 600)
    cfg.setdefault("log_path", str(BASE_DIR / "fastpath.log"))
    return cfg


def log(event: dict[str, Any]) -> None:
    try:
        path = Path(CFG["log_path"])
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(path.with_suffix(path.suffix + ".1"))
        event = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **event}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _prune_seen() -> None:
    cutoff = time.time() - float(CFG.get("dedupe_seconds", 600))
    for key in [k for k, ts in SEEN.items() if ts < cutoff]:
        SEEN.pop(key, None)


# --------------------------------------------------------------------- parsing

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
NORMALIZE = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي", "ؤ": "و",
    "ـ": "",
})


def normalize(text: str) -> str:
    return (text or "").translate(AR_DIGITS).translate(NORMALIZE).lower().strip()


WAKE_HINTS = ("صحيني", "صحي", "منبه", "نبهني", "وقت الاستيقاظ", "alarm", "wake me", "wake-up")
FILE_HINTS = ("فايل", "ملف", "file")
DESKTOP_HINTS = ("ديسكتوب", "دسكتوب", "سطح المكتب", "desktop")
CREATE_HINTS = ("اعمل", "انشئ", "أنشئ", "create", "make", "اكتب", "write", "حط")
DESTRUCTIVE_HINTS = ("امسح", "احذف", "الغي", "ألغي", "delete", "remove", "cancel", "stop", "اقفل")


def _minutes_from_relative(text: str) -> int | None:
    """Return minutes from a relative Arabic/English duration, else None."""
    t = normalize(text)

    def num(value: str | int) -> int:
        try:
            return max(0, int(value))
        except Exception:
            return 0

    patterns: list[tuple[str, Any]] = [
        (r"(?:نص|نصف)\s*ساع(?:ة|ه)", 30),
        (r"ساع(?:ة|ه)\s*و\s*نص", 90),
        (r"\bhalf\s+an?\s+hour\b", 30),
        (r"\ban?\s+hour\s+and\s+a\s+half\b", 90),
        (r"ساعتين", 120),
        (r"(\d+)\s*ساع(?:ة|ه|ات)", lambda m: num(m.group(1)) * 60),
        (r"(\d+)\s*(?:hours?|hrs?|h)\b", lambda m: num(m.group(1)) * 60),
        (r"ساع(?:ة|ه)", 60),
        (r"\ban?\s+hour\b", 60),
        (r"دقيقتين", 2),
        (r"(\d+)\s*(?:دقيق(?:ة|ه|ات)|دقايق)", lambda m: num(m.group(1))),
        (r"(\d+)\s*(?:minutes?|mins?|m)\b", lambda m: num(m.group(1))),
        (r"دقيق(?:ة|ه)", 1),
        (r"\ba\s+minute\b", 1),
    ]
    for pattern, value in patterns:
        match = re.search(pattern, t)
        if not match:
            continue
        if callable(value):
            return int(value(match))
        return int(value)
    return None


def _minutes_from_absolute(text: str) -> int | None:
    """Return minutes until a clock time (HH:MM or 'الساعة 6'), else None."""
    t = normalize(text)
    match = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    hour = minute = None
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        match = re.search(r"(?:الساعة|الساعه|at)\s*(\d{1,2})\b", t)
        if match:
            hour, minute = int(match.group(1)), 0
    if hour is None or minute is None:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = time.localtime()
    target = now.tm_hour * 60 + now.tm_min
    wanted = hour * 60 + minute
    delta = wanted - target
    if delta <= 0:
        delta += 24 * 60
    return delta


def parse_wake(text: str) -> dict[str, Any] | None:
    t = normalize(text)
    if any(hint in t for hint in DESTRUCTIVE_HINTS):
        return None
    if not any(hint in t for hint in WAKE_HINTS):
        return None
    if not any(word in t for word in ("بعد", "in ", "الساعة", "الساعه", "at ", ":")):
        # "اعمل منبه" with no time at all: leave it to the agent to ask.
        return None
    minutes = _minutes_from_relative(t)
    if minutes is None:
        minutes = _minutes_from_absolute(t)
    if minutes is None:
        return None
    minutes = max(1, min(minutes, 10080))
    return {"kind": "wake", "start_in_minutes": minutes}


FILENAME_RE = re.compile(
    r"[\"'«”]([^\"'»”\n]{1,80}?\.[A-Za-z0-9]{1,10})[\"'»“]"
)
NAME_HINT_RE = re.compile(
    r"(?:اسمه|اسمها|باسم|بإسم|called|named|name it)\s+([^\s،,\n]{1,80})",
    re.IGNORECASE,
)
DOTTED_RE = re.compile(r"\b([\w\u0600-\u06FF\-]{1,60}\.[A-Za-z0-9]{1,10})\b")
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean_filename(raw: str) -> str | None:
    name = (raw or "").strip().strip("'\"«»“”").strip()
    name = BAD_CHARS.sub("", name).strip(" .")
    name = Path(name).name
    if not name or name in (".", "..") or len(name) > 120:
        return None
    return name


def parse_file(text: str) -> dict[str, Any] | None:
    t = normalize(text)
    if any(hint in t for hint in DESTRUCTIVE_HINTS):
        return None
    if not any(hint in t for hint in FILE_HINTS):
        return None
    if not any(hint in t for hint in DESKTOP_HINTS):
        return None
    if not any(hint in t for hint in CREATE_HINTS):
        return None

    raw_name = None
    for pattern in (FILENAME_RE, NAME_HINT_RE, DOTTED_RE):
        match = pattern.search(text)
        if match:
            raw_name = match.group(1)
            break
    name = _clean_filename(raw_name or "")
    if not name:
        return None

    content = ""
    content_match = re.search(
        r"(?:مكتوب\s+فيه|محتواه|content|with text)\s*[:\s]\s*(.{1,400})",
        text, re.IGNORECASE,
    )
    if content_match:
        content = content_match.group(1).strip()
    elif name.lower().endswith(".test"):
        content = "TEST"

    return {"kind": "file", "name": name, "content": content}


# --------------------------------------------------------------------- actions


async def mcp_tool(tool: str, payload: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
    assert CLIENT is not None
    response = await CLIENT.post(
        f"{CFG['mcp_base']}/tools/{tool}",
        headers={"X-API-Key": CFG["mcp_api_key"]},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


async def send_telegram(text: str) -> dict[str, Any]:
    assert CLIENT is not None
    url = f"https://api.telegram.org/bot{CFG['telegram_bot_token']}/sendMessage"
    payload = {
        "chat_id": CFG["allowed_chat_id"],
        "text": text,
        "disable_web_page_preview": True,
    }
    # A stalled Bot API read should fail over quickly; normal calls are ~120 ms
    # on a warm keep-alive connection.
    timeout = httpx.Timeout(connect=2.5, read=4.0, write=4.0, pool=2.0)
    last_error = ""
    for attempt in range(3):
        try:
            response = await CLIENT.post(url, json=payload, timeout=timeout)
            data = response.json()
            if data.get("ok"):
                return {"ok": True, "message_id": (data.get("result") or {}).get("message_id")}
            last_error = str(data.get("description") or data)[:300]
        except Exception as exc:  # noqa: BLE001 - reported to the log
            last_error = str(exc)[:300]
        if attempt < 2:
            await asyncio.sleep(0.25 * (attempt + 1))
    return {"ok": False, "error": last_error}


async def handle_wake(parsed: dict[str, Any], chat_id: str) -> tuple[bool, str, dict[str, int]]:
    minutes = int(parsed["start_in_minutes"])
    started = time.perf_counter()
    try:
        result = await mcp_tool("loops", {
            "action": "ensure_alarm",
            "title": "منبه",
            "prompt": "وقت الاستيقاظ",
            "kind": "alarm",
            "start_in_minutes": minutes,
            "cadence_minutes": 30,
            "max_runs": 10,
            "chat_id": chat_id,
        })
    except Exception as exc:  # noqa: BLE001
        return False, f"⚠️ معرفتش أضبط المنبه: {str(exc)[:120]}", {"mcp_ms": int((time.perf_counter() - started) * 1000)}
    timings = {"mcp_ms": int((time.perf_counter() - started) * 1000)}
    if not result.get("ok"):
        return False, f"⚠️ معرفتش أضبط المنبه: {result.get('error') or 'خطأ غير معروف'}", timings
    reply = str(result.get("reply") or "").strip()
    if not reply:
        reply = "✅ تم ضبط المنبه."
    return True, reply, timings


async def handle_file(parsed: dict[str, Any]) -> tuple[bool, str, dict[str, int]]:
    name = str(parsed["name"])
    target = Path(CFG["desktop_dir"]) / name
    started = time.perf_counter()
    try:
        result = await mcp_tool("files", {
            "action": "file_write",
            "path": str(target),
            "content": str(parsed.get("content") or ""),
        }, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        return False, f"⚠️ معرفتش أعمل الملف: {str(exc)[:120]}", {"mcp_ms": int((time.perf_counter() - started) * 1000)}
    timings = {"mcp_ms": int((time.perf_counter() - started) * 1000)}
    if not result.get("ok"):
        return False, f"⚠️ معرفتش أعمل الملف: {result.get('error') or 'خطأ غير معروف'}", timings
    size = result.get("size_bytes")
    extra = f" ({size} بايت)" if isinstance(size, int) and size else ""
    return True, f"✅ تم إنشاء الملف `{name}` على الديسكتوب{extra}.", timings


# --------------------------------------------------------------------- web app


@asynccontextmanager
async def lifespan(_: FastAPI):
    global CFG, CLIENT
    CFG = load_config()
    CLIENT = httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=120.0),
        timeout=httpx.Timeout(connect=2.5, read=8.0, write=8.0, pool=2.0),
    )
    log({"event": "start", "port": CFG["port"]})
    try:
        yield
    finally:
        if CLIENT is not None:
            await CLIENT.aclose()
        log({"event": "stop"})


app = FastAPI(title="OTHVM Telegram fast-path", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "service": "othvm-telegram-fastpath",
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "counters": COUNTERS,
        "mcp": CFG.get("mcp_base"),
        "n8n": CFG.get("n8n_base"),
    })


async def forward(request: Request) -> Response:
    assert CLIENT is not None
    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() not in ("host", "content-length", "connection", "accept-encoding")
    }
    body = await request.body()
    url = CFG["n8n_base"].rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    try:
        upstream = await CLIENT.request(
            request.method, url, headers=headers, content=body, timeout=120.0,
        )
    except Exception as exc:  # noqa: BLE001
        COUNTERS["errors"] += 1
        log({"event": "forward_error", "path": request.url.path, "error": str(exc)[:300]})
        return Response("Bad Gateway", status_code=502)
    COUNTERS["forwarded"] += 1
    response_headers = {
        key: value for key, value in upstream.headers.items()
        if key.lower() not in ("content-encoding", "content-length", "transfer-encoding", "connection")
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.post("/rest/projects/{project_id}/agents/v2/{agent_id}/webhooks/telegram")
async def telegram_webhook(project_id: str, agent_id: str, request: Request) -> Response:
    COUNTERS["updates"] += 1
    started = time.perf_counter()

    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if CFG.get("telegram_secret") and secret != CFG["telegram_secret"]:
        COUNTERS["errors"] += 1
        return JSONResponse({"ok": False, "error": "bad secret"}, status_code=401)

    try:
        update = json.loads(await request.body() or b"{}")
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    update_id = update.get("update_id")
    _prune_seen()
    if isinstance(update_id, int) and update_id in SEEN:
        COUNTERS["duplicates"] += 1
        return JSONResponse({"ok": True, "duplicate": True})

    def mark_seen() -> None:
        if isinstance(update_id, int):
            SEEN[update_id] = time.time()

    message = update.get("message") or update.get("edited_message") or {}
    text = str(message.get("text") or "").strip()
    sender = str((message.get("from") or {}).get("id") or "")
    chat_id = str((message.get("chat") or {}).get("id") or "")
    allowed_user = str(CFG.get("allowed_user_id") or "")
    allowed_chat = str(CFG.get("allowed_chat_id") or "")

    if not text or not message or (allowed_user and sender != allowed_user) or (
        allowed_chat and chat_id != allowed_chat
    ):
        response = await forward(request)
        if response.status_code < 400:
            mark_seen()
        return response

    parsed = parse_wake(text) or parse_file(text)
    if not parsed:
        response = await forward(request)
        if response.status_code < 400:
            mark_seen()
        return response

    if parsed["kind"] == "wake":
        ok, reply, timings = await handle_wake(parsed, chat_id or allowed_chat)
    else:
        ok, reply, timings = await handle_file(parsed)

    send_started = time.perf_counter()
    sent = await send_telegram(reply)
    timings["send_ms"] = int((time.perf_counter() - send_started) * 1000)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    COUNTERS["fast"] += 1
    if not ok or not sent.get("ok"):
        COUNTERS["errors"] += 1
    if ok:
        mark_seen()
    log({
        "event": "fast_path",
        "update_id": update_id,
        "chat_id": chat_id,
        "kind": parsed["kind"],
        "text": text[:200],
        "ok": ok,
        "sent": bool(sent.get("ok")),
        "message_id": sent.get("message_id"),
        "send_error": sent.get("error"),
        "reply": reply[:300],
        "ms": elapsed_ms,
        **timings,
    })
    return JSONResponse({
        "ok": True,
        "fast": parsed["kind"],
        "sent": bool(sent.get("ok")),
        "ms": elapsed_ms,
        **timings,
    })


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def catch_all(path: str, request: Request) -> Response:
    return await forward(request)


if __name__ == "__main__":
    cfg = load_config()
    uvicorn.run(
        app,
        host=str(cfg["host"]),
        port=int(cfg["port"]),
        log_level="warning",
        access_log=False,
    )
