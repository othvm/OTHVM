"""
OTHVM — Laptop Control MCP Server
=================================
FastAPI server exposing Windows laptop control as simple REST tools.

Two roles (same file):
  * Service  (default)  : port 8765, runs under NSSM as SYSTEM.
  * Desktop  (--desktop): port 8766, runs inside the founder's interactive
                          Windows session. Windows services live in session 0
                          and cannot see the desktop, so every GUI action
                          (screenshot, input, windows, apps, browser) is
                          proxied from the service to this helper.

Auth: X-API-Key header on every tool call (value in config.json).

Permissions: permission.json -> "full" | "write" | "read" | "none"
  none   -> nothing
  read   -> read-only tools
  write  -> read + write tools
  full   -> everything (shell, shutdown, delete, JS execution)
  Changing the mode requires a 6-digit code delivered to the founder on
  Telegram and echoed back by the agent.

Run:  python main.py            (service, 8765)
      python main.py --desktop  (desktop helper, 8766)
"""
from __future__ import annotations

import asyncio
import ast
import base64
import difflib
import fnmatch
import glob as globmod
import hashlib
import html.parser
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import winreg
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import psutil
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
import uvicorn

# --------------------------------------------------------------------------- paths
BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp"
LOG_DIR = BASE_DIR / "logs"
BROWSER_PROFILE = BASE_DIR / "browser_profile"
PERM_FILE = BASE_DIR / "permission.json"
CONFIG_FILE = BASE_DIR / "config.json"
PENDING_FILE = BASE_DIR / "pending_permission.json"
EMERGENCY_FLAG = BASE_DIR / "EMERGENCY_STOP"
CAIRO = ZoneInfo("Africa/Cairo")
PUBLIC_BASE_URL = os.environ.get("OTHVM_PUBLIC_BASE_URL", "").rstrip("/")

for d in (TEMP_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

DESKTOP_MODE = "--desktop" in sys.argv
ROLE = "desktop" if DESKTOP_MODE else "service"

# --------------------------------------------------------------------------- config
DEFAULT_CONFIG = {
    "api_key": "",
    "service_port": 8765,
    "desktop_port": 8766,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "gemini_api_key": "",
    "supabase_url": "",
    "supabase_service_key": "",
    "max_command_timeout": 300,
    "max_record_seconds": 300,
}


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


CFG = dict(DEFAULT_CONFIG)
CFG.update(load_json(CONFIG_FILE, {}))
if not PUBLIC_BASE_URL:
    PUBLIC_BASE_URL = str(CFG.get("public_base_url") or "").rstrip("/")
CFG.setdefault(
    "n8n_db_path",
    os.environ.get("N8N_DB_PATH", r"C:/Windows/System32/config/systemprofile/.n8n/database.sqlite"),
)
CFG.setdefault("agent_id", "")
CFG.setdefault("main_telegram_thread_id", "")
CFG.setdefault("summary_model", "gemini-3.8-flash")
if not CFG.get("api_key"):
    CFG["api_key"] = "othvm_" + secrets.token_urlsafe(24)
    save_json(CONFIG_FILE, CFG)

API_KEY = CFG["api_key"]

# --------------------------------------------------------------------------- permissions
LEVELS = {"none": 0, "read": 1, "write": 2, "full": 3}
DEFAULT_MODE = "read"


def read_mode() -> str:
    data = load_json(PERM_FILE, {})
    mode = str(data.get("mode", DEFAULT_MODE)).lower().strip()
    return mode if mode in LEVELS else DEFAULT_MODE


def write_mode(mode: str, by: str = "agent") -> None:
    save_json(PERM_FILE, {
        "mode": mode,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": by,
    })


if not PERM_FILE.exists():
    write_mode(DEFAULT_MODE, by="bootstrap")


class PermissionDenied(Exception):
    def __init__(self, required: str, action: str):
        self.required = required
        self.action = action
        self.current = read_mode()
        super().__init__(
            f'Action "{action}" requires permission "{required}" but current mode is "{self.current}". '
            f'Ask the founder to approve a mode change (POST /tools/permission set_mode).'
        )


def require(level: str, action: str) -> None:
    if LEVELS[read_mode()] < LEVELS[level]:
        raise PermissionDenied(level, action)


# --------------------------------------------------------------------------- logging
SENSITIVE_KEYS = {"password", "passwd", "pass", "token", "api_key", "apikey", "secret",
                  "authorization", "cookie", "cookies", "credentials", "otp", "pin"}


def _redact(value: Any, key: str = "") -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return "***"
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, key) for v in value]
    if isinstance(value, str) and len(value) >= 300:
        return value[:300] + "…"
    return value


def log_action(category: str, action: str, body: dict, status: str, detail: str = "", ms: int = 0) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": ROLE,
        "category": category,
        "action": action,
        "status": status,
        "ms": ms,
        "params": _redact(body),
    }
    if detail:
        entry["detail"] = str(detail)[:1500]
    try:
        with (LOG_DIR / "actions.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- telegram
async def telegram_send_payload(payload: dict, chat_id: str | None = None) -> bool:
    token = CFG.get("telegram_bot_token")
    chat = chat_id or CFG.get("telegram_chat_id")
    if not token or not chat:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat, **payload})
            return r.status_code == 200
    except Exception:
        return False


async def telegram_send(text: str) -> bool:
    return await telegram_send_payload({"text": text})


# --------------------------------------------------------------------------- pending permission codes
def _load_pending() -> dict:
    return load_json(PENDING_FILE, {})


def _save_pending(data: dict) -> None:
    save_json(PENDING_FILE, data)


def _prune_pending(data: dict) -> dict:
    now = time.time()
    return {k: v for k, v in data.items() if float(v.get("expires", 0)) > now}


PENDING_LOCK = asyncio.Lock()


# --------------------------------------------------------------------------- helpers
def _now_name(prefix: str, ext: str) -> Path:
    return TEMP_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"


def _b64_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _ok(**kw):
    kw.setdefault("ok", True)
    return kw


def _fail(error: str, **kw):
    kw.setdefault("ok", False)
    kw["error"] = error
    return kw


# --------------------------------------------------------------------------- safety denylist + trash
TRASH_DIR = BASE_DIR / "trash"
TRASH_INDEX = TRASH_DIR / "index.json"

DENY_PATH_PARTS = (
    "\\.ssh\\", "\\.aws\\", "\\.gnupg\\", "\\.config\\gcloud\\",
    "\\google\\chrome\\user data\\", "\\microsoft\\edge\\user data\\",
    "\\brave-browser\\user data\\", "\\mozilla\\firefox\\profiles\\",
    "\\.n8n\\config", "\\appdata\\roaming\\n8n\\config",
)
DENY_FILE_NAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials.json",
    "n8n_encryption_key", "login data", "cookies", "web data", "local state",
}
DENY_FILE_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".kdbx")
DENY_COMMAND_PATTERNS = (
    (r"\bformat(?:\.com)?\s+[a-z]:", "disk format"),
    (r"\bdiskpart\b", "diskpart"),
    (r"\bbcdedit\b", "boot configuration edit"),
    (r"\bvssadmin\b[^\n]*\bdelete\b", "shadow copy deletion"),
    (r"\bwbadmin\b[^\n]*\bdelete\b", "backup deletion"),
    (r"\bcipher\b[^\n]*\s/w\b", "free-space wipe"),
    (r"\breg\s+delete\s+hklm\\system\b", "registry SYSTEM hive deletion"),
    (r"\bnet\s+user\s+\S+\s+/delete\b", "account deletion"),
    (r"\b(rd|rmdir)\s+/s\s+/q\s+[a-z]:\\?\s*(?:$|&|;)", "recursive drive-root delete"),
    (r"\bremove-item\b[^\n]*-recurse[^\n]*\b[a-z]:\\?\s*(?:$|;|\))", "recursive drive-root delete"),
    (r"\bdel\b[^\n]*/f[^\n]*/s[^\n]*/q[^\n]*\b[a-z]:\\?\s*(?:$|&|;)", "recursive drive-root delete"),
)


def _deny_path(path: Path) -> str:
    try:
        resolved = str(path.resolve()).replace("/", "\\").lower()
    except Exception:
        resolved = str(path).replace("/", "\\").lower()
    name = path.name.lower()
    if resolved in {str(CONFIG_FILE.resolve()).lower(), str(PERM_FILE.resolve()).lower()}:
        return "this file holds OTHVM credentials and is blocked by policy"
    for part in DENY_PATH_PARTS:
        if part in resolved:
            return "credential or browser data path is blocked by policy"
    if name in DENY_FILE_NAMES or name.endswith(DENY_FILE_SUFFIXES):
        return f'credential file "{path.name}" is blocked by policy'
    if name == ".env" or (name.startswith(".env.") and name not in (
            ".env.example", ".env.sample", ".env.template")):
        return ".env files are blocked by policy"
    return ""


def _deny_command(cmd: str) -> str:
    low = str(cmd or "").lower()
    for pattern, label in DENY_COMMAND_PATTERNS:
        if re.search(pattern, low):
            return f"command blocked by policy: {label}"
    return ""


def _trash_load() -> list:
    data = load_json(TRASH_INDEX, [])
    return data if isinstance(data, list) else []


def _trash_save(items: list) -> None:
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    save_json(TRASH_INDEX, items[-500:])


def _trash_put(path: Path, action: str) -> dict:
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tid = f"{stamp}_{uuid.uuid4().hex[:8]}"
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", path.name)[:80] or "item"
    dest = TRASH_DIR / f"{tid}_{safe}"
    kind = "folder" if path.is_dir() else "file"
    size = None
    if kind == "file":
        try:
            size = path.stat().st_size
        except Exception:
            pass
    shutil.move(str(path), str(dest))
    item = {
        "id": tid, "name": path.name, "original": str(path), "trashed": str(dest),
        "kind": kind, "size_bytes": size, "action": action,
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }
    items = _trash_load()
    items.append(item)
    _trash_save(items)
    return item


TEXT_EXTS = {".txt", ".md", ".markdown", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx",
             ".html", ".htm", ".css", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".log", ".sql",
             ".sh", ".bat", ".ps1", ".env", ".toml", ".rst", ".c", ".cpp", ".h", ".java", ".go",
             ".rs", ".php", ".rb", ".swift", ".kt"}


def read_text_file(path: Path, max_bytes: int = 2_000_000) -> str:
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    for enc in ("utf-8-sig", "utf-8", "cp1256", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- system tools
def op_screenshot(body: dict) -> dict:
    require("read", "screenshot")
    import pyautogui
    path = _now_name("screenshot", ".png")
    img = pyautogui.screenshot()
    img.save(path)
    payload = _ok(action="screenshot", path=str(path), width=img.width, height=img.height,
                  size_bytes=path.stat().st_size, mime="image/png")
    if body.get("include_base64"):
        payload["image_base64"] = _b64_image(path)
    return payload


def op_screen_record(body: dict) -> dict:
    require("write", "screen_record")
    duration = int(body.get("duration", 10))
    duration = max(1, min(duration, int(CFG.get("max_record_seconds", 300))))
    out = _now_name("record", ".mp4")
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return _fail("ffmpeg is not available (install imageio-ffmpeg or ffmpeg)")
    cmd = [ffmpeg, "-y", "-f", "gdigrab", "-framerate", "15", "-i", "desktop",
           "-t", str(duration), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 120,
                          encoding="utf-8", errors="replace")
    if not out.exists() or out.stat().st_size == 0:
        return _fail("screen recording failed", stderr=(proc.stderr or "")[-1500:])
    return _ok(action="screen_record", path=str(out), duration=duration,
               size_bytes=out.stat().st_size, mime="video/mp4")


def op_get_processes(body: dict) -> dict:
    require("read", "get_processes")
    name_filter = str(body.get("name", "")).lower().strip()
    limit = int(body.get("limit", 500))
    rows = []
    for p in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_info", "create_time", "status"]):
        try:
            info = p.info
            name = info.get("name") or ""
            if name_filter and name_filter not in name.lower():
                continue
            mem = info.get("memory_info")
            rows.append({
                "pid": info.get("pid"),
                "name": name,
                "user": info.get("username"),
                "status": info.get("status"),
                "cpu_percent": info.get("cpu_percent"),
                "memory_mb": round((mem.rss / 1024 / 1024), 1) if mem else None,
                "started_at": datetime.fromtimestamp(info["create_time"], timezone.utc).isoformat() if info.get("create_time") else None,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            continue
    rows.sort(key=lambda r: (-(r.get("memory_mb") or 0), r.get("name") or ""))
    rows = rows[:limit]
    return _ok(action="get_processes", count=len(rows), processes=rows)


def op_kill_process(body: dict) -> dict:
    require("write", "kill_process")
    pid = body.get("pid")
    name = str(body.get("name", "")).strip()
    killed = []
    if pid:
        try:
            p = psutil.Process(int(pid))
            nm = p.name()
            p.terminate()
            try:
                p.wait(timeout=5)
            except psutil.TimeoutExpired:
                p.kill()
            killed.append({"pid": int(pid), "name": nm})
        except Exception as e:
            return _fail(f"could not kill pid {pid}: {e}")
    elif name:
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info.get("name") or "").lower() == name.lower():
                    p.terminate()
                    killed.append({"pid": p.info["pid"], "name": p.info["name"]})
            except Exception:
                continue
        if not killed:
            return _fail(f"no running process named {name}")
    else:
        return _fail("pid or name is required")
    return _ok(action="kill_process", killed=killed, count=len(killed))


def op_system_command(body: dict) -> dict:
    require("full", "system_command")
    cmd = str(body.get("command", "")).strip()
    if not cmd:
        return _fail("command is required")
    denied = _deny_command(cmd)
    if denied:
        return _fail(denied, blocked=True, command=cmd)
    timeout = int(body.get("timeout", 60))
    timeout = max(1, min(timeout, int(CFG.get("max_command_timeout", 300))))
    cwd = body.get("cwd") or None
    started = time.time()
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout, cwd=cwd,
            encoding="utf-8", errors="replace",
        )
        return _ok(action="system_command", command=cmd, exit_code=proc.returncode,
                   stdout=(proc.stdout or "")[-20000:], stderr=(proc.stderr or "")[-8000:],
                   duration_ms=int((time.time() - started) * 1000))
    except subprocess.TimeoutExpired:
        return _fail(f"command timed out after {timeout}s", command=cmd)
    except Exception as e:
        return _fail(f"command failed: {e}", command=cmd)


def op_shutdown(body: dict) -> dict:
    require("full", "shutdown")
    delay = max(0, int(body.get("delay", 0)))
    subprocess.run(["shutdown", "/s", "/t", str(delay)], capture_output=True, text=True)
    return _ok(action="shutdown", delay_seconds=delay,
               note="Shutdown scheduled. Cancel with: shutdown /a")


def op_restart(body: dict) -> dict:
    require("full", "restart")
    delay = max(0, int(body.get("delay", 0)))
    subprocess.run(["shutdown", "/r", "/t", str(delay)], capture_output=True, text=True)
    return _ok(action="restart", delay_seconds=delay,
               note="Restart scheduled. Cancel with: shutdown /a")


def op_sleep(body: dict) -> dict:
    require("write", "sleep")
    try:
        subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                       capture_output=True, text=True, timeout=15)
        return _ok(action="sleep")
    except Exception as e:
        return _fail(f"sleep failed: {e}")


def op_get_system_info(body: dict) -> dict:
    require("read", "get_system_info")
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({"device": part.device, "mount": part.mountpoint,
                          "total_gb": round(u.total / 1e9, 1), "used_gb": round(u.used / 1e9, 1),
                          "free_gb": round(u.free / 1e9, 1), "percent": u.percent})
        except Exception:
            continue
    battery = None
    try:
        b = psutil.sensors_battery()
        if b:
            battery = {"percent": b.percent, "plugged": b.power_plugged,
                       "time_left_min": None if b.secsleft in (-1, -2) else round(b.secsleft / 60)}
    except Exception:
        pass
    boot = psutil.boot_time()
    uptime = time.time() - boot
    now = datetime.now(CAIRO)
    return _ok(action="get_system_info", hostname=socket.gethostname(), user=os.getenv("USERNAME"),
               clock={"cairo": now.isoformat(), "cairo_human": now.strftime("%A %Y-%m-%d %H:%M:%S"),
                      "utc": datetime.now(timezone.utc).isoformat(), "timezone": "Africa/Cairo"},
               platform=platform.platform(), python=platform.python_version(),
               cpu={"percent": psutil.cpu_percent(interval=0.4), "cores_logical": psutil.cpu_count(),
                    "cores_physical": psutil.cpu_count(logical=False)},
               ram={"total_gb": round(vm.total / 1e9, 1), "used_gb": round(vm.used / 1e9, 1),
                    "available_gb": round(vm.available / 1e9, 1), "percent": vm.percent,
                    "swap_percent": sm.percent},
               disks=disks, battery=battery,
               uptime={"seconds": int(uptime), "human": str(__import__("datetime").timedelta(seconds=int(uptime))),
                       "boot_time": datetime.fromtimestamp(boot, timezone.utc).isoformat()})


# --------------------------------------------------------------------------- desktop state tools
def _ps_run(script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _ps_json(script: str, timeout: int = 60):
    proc = _ps_run(script, timeout=timeout)
    raw = (proc.stdout or "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            out, idx = [], 0
            while idx < len(raw):
                while idx < len(raw) and raw[idx] in " \r\n\t":
                    idx += 1
                if idx >= len(raw):
                    break
                try:
                    obj, idx = decoder.raw_decode(raw, idx)
                except json.JSONDecodeError:
                    out = []
                    break
                out.append(obj)
            if out:
                return out
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "powershell failed").strip()[:800])
    return []


def _clipboard_retry(fn, attempts: int = 6):
    import win32clipboard as w
    last = None
    for _ in range(attempts):
        try:
            return fn(w)
        except Exception as e:
            last = e
            time.sleep(0.2)
    raise RuntimeError(f"{type(last).__name__}: {last}")


def op_clipboard_get(body: dict) -> dict:
    require("read", "clipboard_get")
    def read(w):
        w.OpenClipboard()
        try:
            if w.IsClipboardFormatAvailable(w.CF_UNICODETEXT):
                return str(w.GetClipboardData(w.CF_UNICODETEXT) or "")
            return ""
        finally:
            w.CloseClipboard()
    try:
        text = _clipboard_retry(read)
        return _ok(action="clipboard_get", text=text, chars=len(text or ""))
    except Exception as e:
        return _fail(f"clipboard read failed: {e}")


def op_clipboard_set(body: dict) -> dict:
    require("write", "clipboard_set")
    text = body.get("text", body.get("content", ""))
    if isinstance(text, (dict, list)):
        text = json.dumps(text, ensure_ascii=False)
    text = str(text)
    def write(w):
        w.OpenClipboard()
        try:
            w.EmptyClipboard()
            w.SetClipboardData(w.CF_UNICODETEXT, text)
        finally:
            w.CloseClipboard()
    try:
        _clipboard_retry(write)
        return _ok(action="clipboard_set", chars=len(text))
    except Exception as e:
        return _fail(f"clipboard write failed: {e}")


def op_service_list(body: dict) -> dict:
    require("read", "service_list")
    pattern = re.sub(r"[^A-Za-z0-9_.*? -]", "", str(body.get("filter") or body.get("name") or ""))[:80]
    where = f" -Name '*{pattern}*'" if pattern else ""
    script = (f"Get-Service{where} | Select-Object Name,DisplayName,Status,StartType | "
              "Sort-Object Name | ConvertTo-Json -Compress")
    try:
        data = _ps_json(script)
    except Exception as e:
        return _fail(f"service list failed: {e}")
    if isinstance(data, dict):
        data = [data]
    return _ok(action="service_list", count=len(data), services=data)


def op_service_control(body: dict) -> dict:
    require("full", "service_control")
    name = str(body.get("name") or body.get("service") or "").strip()
    action = str(body.get("service_action") or body.get("mode") or body.get("op") or "").lower().strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,120}", name):
        return _fail("a valid service name is required")
    if action not in ("start", "stop", "restart"):
        return _fail('service_action must be start, stop, or restart')
    verb = {"start": "Start-Service", "stop": "Stop-Service", "restart": "Restart-Service"}[action]
    script = (f"$ErrorActionPreference='Stop'; {verb} -Name '{name}' -Force; "
              f"(Get-Service -Name '{name}').Status")
    try:
        proc = _ps_run(script, timeout=120)
    except Exception as e:
        return _fail(f"service {action} failed: {e}")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return _fail(f"service {action} failed: {(proc.stderr or out)[:800]}", name=name)
    return _ok(action="service_control", name=name, service_action=action,
               status=out.splitlines()[-1].strip() if out else None)


def _task_name(body: dict) -> str:
    name = str(body.get("name") or body.get("task") or body.get("task_name") or "").strip()
    return name if re.fullmatch(r"[A-Za-z0-9 _.\-]{1,120}", name) else ""


def op_task_list(body: dict) -> dict:
    require("read", "task_list")
    pattern = re.sub(r"[^A-Za-z0-9_.*? -]", "", str(body.get("filter") or body.get("name") or ""))[:80]
    where = f" -TaskName '*{pattern}*'" if pattern else ""
    script = (f"Get-ScheduledTask{where} | Select-Object TaskName,TaskPath,State | "
              "Sort-Object TaskName | ConvertTo-Json -Compress")
    try:
        data = _ps_json(script, timeout=90)
    except Exception as e:
        return _fail(f"task list failed: {e}")
    if isinstance(data, dict):
        data = [data]
    return _ok(action="task_list", count=len(data), tasks=data)


def op_task_create(body: dict) -> dict:
    require("full", "task_create")
    name = _task_name(body)
    command = str(body.get("command") or body.get("program") or "").strip()
    if not name:
        return _fail("a valid task name is required")
    if not command:
        return _fail("command is required")
    denied = _deny_command(command)
    if denied:
        return _fail(denied, blocked=True)
    schedule = str(body.get("schedule") or "once").lower().strip()
    time_str = str(body.get("time") or "23:59").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", time_str):
        return _fail("time must be HH:MM (24-hour)")
    run_as = str(body.get("run_as") or "SYSTEM").strip()
    args = ["schtasks", "/create", "/tn", name, "/tr", command, "/f"]
    if schedule == "once":
        args += ["/sc", "once", "/st", time_str]
    elif schedule == "daily":
        args += ["/sc", "daily", "/st", time_str]
    elif schedule == "minute":
        interval = max(1, min(int(body.get("interval", 5) or 5), 1439))
        args += ["/sc", "minute", "/mo", str(interval)]
    elif schedule in ("onlogon", "onstart"):
        args += ["/sc", schedule]
    else:
        return _fail("schedule must be once, daily, minute, onlogon, or onstart")
    if run_as.upper() == "SYSTEM":
        args += ["/ru", "SYSTEM"]
    else:
        args += ["/ru", run_as]
        if body.get("password"):
            args += ["/rp", str(body.get("password"))]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60,
                              encoding="utf-8", errors="replace")
    except Exception as e:
        return _fail(f"task create failed: {e}")
    if proc.returncode != 0:
        return _fail(f"task create failed: {(proc.stderr or proc.stdout or '').strip()[:800]}", name=name)
    return _ok(action="task_create", name=name, schedule=schedule, time=time_str,
               run_as=run_as, output=(proc.stdout or "").strip()[:500])


def _task_simple(body: dict, verb: str) -> dict:
    require("full", f"task_{verb}")
    name = _task_name(body)
    if not name:
        return _fail("a valid task name is required")
    args = ["schtasks", f"/{verb}", "/tn", name]
    if verb == "delete":
        args.append("/f")
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60,
                              encoding="utf-8", errors="replace")
    except Exception as e:
        return _fail(f"task {verb} failed: {e}")
    if proc.returncode != 0:
        return _fail(f"task {verb} failed: {(proc.stderr or proc.stdout or '').strip()[:800]}", name=name)
    return _ok(action=f"task_{verb}", name=name, output=(proc.stdout or "").strip()[:500])


def op_task_delete(body: dict) -> dict:
    return _task_simple(body, "delete")


def op_task_run(body: dict) -> dict:
    return _task_simple(body, "run")


def op_task_end(body: dict) -> dict:
    return _task_simple(body, "end")


def op_lock(body: dict) -> dict:
    require("write", "lock")
    try:
        proc = subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"],
                              capture_output=True, text=True, timeout=15)
        return _ok(action="lock", exit_code=proc.returncode)
    except Exception as e:
        return _fail(f"lock failed: {e}")


def op_hibernate(body: dict) -> dict:
    require("full", "hibernate")
    try:
        proc = subprocess.run(["shutdown", "/h"], capture_output=True, text=True, timeout=20)
    except Exception as e:
        return _fail(f"hibernate failed: {e}")
    if proc.returncode != 0:
        return _fail(f"hibernate failed: {(proc.stderr or proc.stdout or '').strip()[:500]}")
    return _ok(action="hibernate", note="Hibernation requested.")


def op_cancel_shutdown(body: dict) -> dict:
    require("write", "cancel_shutdown")
    try:
        proc = subprocess.run(["shutdown", "/a"], capture_output=True, text=True, timeout=20)
    except Exception as e:
        return _fail(f"cancel shutdown failed: {e}")
    if proc.returncode != 0:
        return _fail(f"cancel shutdown failed: {(proc.stderr or proc.stdout or '').strip()[:500]}")
    return _ok(action="cancel_shutdown", output=(proc.stdout or "").strip()[:300])


def _volume_endpoint():
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def op_volume_get(body: dict) -> dict:
    require("read", "volume_get")
    try:
        ev = _volume_endpoint()
        return _ok(action="volume_get", percent=round(float(ev.GetMasterVolumeLevelScalar()) * 100),
                   muted=bool(ev.GetMute()))
    except Exception as e:
        return _fail(f"volume read failed: {type(e).__name__}: {e}")


def op_volume_set(body: dict) -> dict:
    require("write", "volume_set")
    raw = body.get("percent", body.get("level", body.get("volume")))
    if raw is None:
        return _fail("percent (0-100) is required")
    try:
        percent = max(0, min(100, int(float(raw))))
        ev = _volume_endpoint()
        ev.SetMasterVolumeLevelScalar(percent / 100.0, None)
        muted = None
        if body.get("mute") is not None or body.get("muted") is not None:
            muted = _truthy(body.get("mute", body.get("muted")))
            ev.SetMute(muted, None)
        return _ok(action="volume_set", percent=percent, muted=muted)
    except Exception as e:
        return _fail(f"volume set failed: {type(e).__name__}: {e}")


def op_mute(body: dict) -> dict:
    require("write", "mute")
    try:
        ev = _volume_endpoint()
        current = bool(ev.GetMute())
        target = body.get("muted", body.get("mute"))
        muted = (not current) if target is None else _truthy(target)
        ev.SetMute(muted, None)
        return _ok(action="mute", muted=muted, was_muted=current)
    except Exception as e:
        return _fail(f"mute failed: {type(e).__name__}: {e}")


def op_brightness_get(body: dict) -> dict:
    require("read", "brightness_get")
    script = ("(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness "
              "-ErrorAction Stop | Select-Object -First 1).CurrentBrightness")
    try:
        proc = _ps_run(script, timeout=30)
    except Exception as e:
        return _fail(f"brightness read failed: {e}")
    if proc.returncode != 0:
        return _fail(f"brightness read failed: {(proc.stderr or '').strip()[:500]}")
    raw = (proc.stdout or "").strip()
    if not raw.isdigit():
        return _fail("brightness is not supported on this display")
    return _ok(action="brightness_get", percent=int(raw))


def op_brightness_set(body: dict) -> dict:
    require("write", "brightness_set")
    raw = body.get("percent", body.get("level", body.get("brightness")))
    if raw is None:
        return _fail("percent (0-100) is required")
    percent = max(0, min(100, int(float(raw))))
    script = ("$m = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods "
              "-ErrorAction Stop; $m | ForEach-Object { $_.WmiSetBrightness(1, " + str(percent) + ") }")
    try:
        proc = _ps_run(script, timeout=30)
    except Exception as e:
        return _fail(f"brightness set failed: {e}")
    if proc.returncode != 0:
        return _fail(f"brightness set failed: {(proc.stderr or '').strip()[:500]}")
    return _ok(action="brightness_set", percent=percent)


def op_audit_log(body: dict) -> dict:
    require("read", "audit_log")
    from collections import deque
    limit = max(1, min(int(body.get("limit", 30) or 30), 500))
    category = str(body.get("filter_category") or "").strip().lower()
    action_filter = str(body.get("action_filter") or body.get("action_contains") or "").strip().lower()
    status = str(body.get("filter_status") or body.get("status") or "").strip().lower()
    since = str(body.get("since") or "").strip()
    path = LOG_DIR / "actions.log"
    if not path.exists():
        return _ok(action="audit_log", count=0, entries=[])
    entries = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in deque(f, maxlen=limit * 8):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if category and str(e.get("category", "")).lower() != category:
                continue
            if action_filter and action_filter not in str(e.get("action", "")).lower():
                continue
            if status and str(e.get("status", "")).lower() != status:
                continue
            if since and str(e.get("ts", "")) < since:
                continue
            entries.append(e)
    entries = entries[-limit:]
    return _ok(action="audit_log", count=len(entries), entries=entries)


# --------------------------------------------------------------------------- file tools
def op_file_read(body: dict) -> dict:
    require("read", "file_read")
    path = Path(str(body.get("path", ""))).expanduser()
    denied = _deny_path(path)
    if denied:
        return _fail(denied, blocked=True, path=str(path))
    if not path.exists():
        return _fail(f"file not found: {path}")
    if not path.is_file():
        return _fail(f"not a file: {path}")
    if path.suffix.lower() not in TEXT_EXTS and body.get("force") is not True:
        return _fail(f"extension {path.suffix} is not in the text whitelist (pass force=true to try anyway)")
    content = read_text_file(path, max_bytes=int(body.get("max_bytes", 2_000_000)))
    return _ok(action="file_read", path=str(path), size_bytes=path.stat().st_size,
               extension=path.suffix, content=content)


def _write(path: Path, content: str, append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8", newline="") as f:
        f.write(content)


def op_file_write(body: dict) -> dict:
    require("write", "file_write")
    path = Path(str(body.get("path", ""))).expanduser()
    denied = _deny_path(path)
    if denied:
        return _fail(denied, blocked=True, path=str(path))
    content = body.get("content", "")
    if isinstance(content, (dict, list)):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    _write(path, str(content), append=False)
    return _ok(action="file_write", path=str(path), size_bytes=path.stat().st_size,
               bytes_written=len(str(content).encode("utf-8")))


def op_file_append(body: dict) -> dict:
    require("write", "file_append")
    path = Path(str(body.get("path", ""))).expanduser()
    denied = _deny_path(path)
    if denied:
        return _fail(denied, blocked=True, path=str(path))
    content = body.get("content", "")
    if isinstance(content, (dict, list)):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    _write(path, str(content), append=True)
    return _ok(action="file_append", path=str(path), size_bytes=path.stat().st_size)


def op_file_delete(body: dict) -> dict:
    require("full", "file_delete")
    path = Path(str(body.get("path", ""))).expanduser()
    denied = _deny_path(path)
    if denied:
        return _fail(denied, blocked=True, path=str(path))
    if not path.exists():
        return _fail(f"path not found: {path}")
    if path.is_dir():
        try:
            next(path.iterdir())
        except StopIteration:
            pass
        except OSError:
            pass
        else:
            return _fail(f"folder is not empty: {path} (use folder_delete)")
    item = _trash_put(path, "file_delete")
    return _ok(action="file_delete", path=str(path), deleted=item["kind"],
               trash_id=item["id"], undo="files restore")


def op_file_move(body: dict) -> dict:
    require("write", "file_move")
    src = Path(str(body.get("source", body.get("path", "")))).expanduser()
    dst = Path(str(body.get("destination", body.get("dest", "")))).expanduser()
    denied = _deny_path(src) or _deny_path(dst)
    if denied:
        return _fail(denied, blocked=True)
    if not src.exists():
        return _fail(f"source not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return _ok(action="file_move", source=str(src), destination=str(dst),
               exists=Path(dst).exists())


def op_file_copy(body: dict) -> dict:
    require("write", "file_copy")
    src = Path(str(body.get("source", body.get("path", "")))).expanduser()
    dst = Path(str(body.get("destination", body.get("dest", "")))).expanduser()
    denied = _deny_path(src) or _deny_path(dst)
    if denied:
        return _fail(denied, blocked=True)
    if not src.exists():
        return _fail(f"source not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
    else:
        shutil.copy2(str(src), str(dst))
    return _ok(action="file_copy", source=str(src), destination=str(dst))


def op_file_list(body: dict) -> dict:
    require("read", "file_list")
    path = Path(str(body.get("path", "."))).expanduser()
    if not path.exists():
        return _fail(f"path not found: {path}")
    if not path.is_dir():
        return _fail(f"not a directory: {path}")
    pattern = str(body.get("pattern", "*"))
    recursive = bool(body.get("recursive", False))
    limit = int(body.get("limit", 500))
    entries = []
    it = path.rglob(pattern) if recursive else path.glob(pattern)
    for p in it:
        try:
            st = p.stat()
            entries.append({
                "name": p.name, "path": str(p),
                "type": "folder" if p.is_dir() else "file",
                "extension": p.suffix.lower(),
                "size_bytes": None if p.is_dir() else st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            })
        except Exception:
            continue
        if len(entries) >= limit:
            break
    entries.sort(key=lambda e: (e["type"] != "folder", e["name"].lower()))
    return _ok(action="file_list", path=str(path), count=len(entries), entries=entries)


def op_file_search(body: dict) -> dict:
    require("read", "file_search")
    root = Path(str(body.get("path", "."))).expanduser()
    if not root.exists() or not root.is_dir():
        return _fail(f"directory not found: {root}")
    name_pattern = str(body.get("name", body.get("pattern", ""))).strip()
    content_query = str(body.get("content", "")).strip()
    limit = int(body.get("limit", 100))
    max_size = int(body.get("max_file_size", 2_000_000))
    results = []

    if content_query:
        for p in root.rglob("*"):
            if len(results) >= limit:
                break
            try:
                if not p.is_file() or p.suffix.lower() not in TEXT_EXTS:
                    continue
                if p.stat().st_size > max_size:
                    continue
                text = read_text_file(p, max_size)
                idx = text.lower().find(content_query.lower())
                if idx >= 0:
                    line_no = text[:idx].count("\n") + 1
                    line = text.splitlines()[line_no - 1] if text.splitlines() else ""
                    results.append({"path": str(p), "line": line_no, "snippet": line[:300]})
            except Exception:
                continue
        return _ok(action="file_search", mode="content", query=content_query,
                   count=len(results), results=results)

    if not name_pattern:
        return _fail("name pattern or content is required")
    pat = name_pattern if any(ch in name_pattern for ch in "*?[") else f"*{name_pattern}*"
    for p in root.rglob(pat):
        if len(results) >= limit:
            break
        try:
            st = p.stat()
            results.append({"path": str(p), "name": p.name,
                            "type": "folder" if p.is_dir() else "file",
                            "size_bytes": None if p.is_dir() else st.st_size,
                            "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
        except Exception:
            continue
    return _ok(action="file_search", mode="name", pattern=pat, count=len(results), results=results)


def op_folder_create(body: dict) -> dict:
    require("write", "folder_create")
    path = Path(str(body.get("path", ""))).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return _ok(action="folder_create", path=str(path), created=True)


def op_folder_delete(body: dict) -> dict:
    require("full", "folder_delete")
    path = Path(str(body.get("path", ""))).expanduser()
    denied = _deny_path(path)
    if denied:
        return _fail(denied, blocked=True, path=str(path))
    if not path.exists():
        return _fail(f"folder not found: {path}")
    if not path.is_dir():
        return _fail(f"not a folder: {path} (use file_delete)")
    item = _trash_put(path, "folder_delete")
    return _ok(action="folder_delete", path=str(path), deleted=True,
               trash_id=item["id"], undo="files restore")


def op_trash_list(body: dict) -> dict:
    require("read", "trash_list")
    items = _trash_load()
    limit = max(1, min(int(body.get("limit", 20) or 20), 200))
    return _ok(action="trash_list", count=len(items), items=list(reversed(items))[:limit])


def op_restore(body: dict) -> dict:
    require("write", "restore")
    items = _trash_load()
    tid = str(body.get("id") or body.get("trash_id") or "").strip()
    if tid:
        matches = [x for x in items if x.get("id") == tid]
    else:
        matches = items[-1:]
    if not matches:
        return _fail("trash is empty" if not items else f"trash item not found: {tid}")
    item = matches[-1]
    src = Path(str(item.get("trashed", "")))
    dst = Path(str(item.get("original", "")))
    if not src.exists():
        return _fail(f"trashed item is missing on disk: {src}", trash_id=item.get("id"))
    denied = _deny_path(dst)
    if denied:
        return _fail(denied, blocked=True)
    if dst.exists():
        dst = dst.with_name(f"{dst.stem}.restored-{item.get('id', '')[:15]}{dst.suffix}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    remaining = [x for x in items if x.get("id") != item.get("id")]
    _trash_save(remaining)
    return _ok(action="restore", trash_id=item.get("id"), restored_to=str(dst),
               name=item.get("name"), kind=item.get("kind"))


def op_trash_empty(body: dict) -> dict:
    require("full", "trash_empty")
    if not _truthy(body.get("confirm") or body.get("confirmed")):
        return _fail("trash_empty needs confirm=true")
    removed = 0
    for entry in _trash_load():
        p = Path(str(entry.get("trashed", "")))
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
            removed += 1
        except Exception:
            continue
    _trash_save([])
    return _ok(action="trash_empty", removed=removed)


# --------------------------------------------------------------------------- application tools
def _win32():
    import win32gui  # noqa
    import win32con  # noqa
    return win32gui, win32con


def _find_windows(title: str):
    win32gui, _ = _win32()
    needle = (title or "").lower().strip()
    found = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            t = win32gui.GetWindowText(hwnd) or ""
            if t and (not needle or needle in t.lower()):
                found.append((hwnd, t))
        except Exception:
            return

    win32gui.EnumWindows(cb, None)
    return found


def op_list_windows(body: dict) -> dict:
    require("read", "list_windows")
    win32gui, _ = _win32()
    windows = []
    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd) or ""
            if not title.strip():
                return
            rect = win32gui.GetWindowRect(hwnd)
            windows.append({
                "handle": hwnd, "title": title,
                "left": rect[0], "top": rect[1],
                "width": rect[2] - rect[0], "height": rect[3] - rect[1],
                "minimized": bool(win32gui.IsIconic(hwnd)),
            })
        except Exception:
            return
    win32gui.EnumWindows(cb, None)
    return _ok(action="list_windows", count=len(windows), windows=windows)


def _force_foreground(hwnd: int) -> bool:
    """Best-effort Windows foreground activation (SetForegroundWindow is locked)."""
    import ctypes
    import win32con
    import win32gui

    if win32gui.GetForegroundWindow() == hwnd:
        return True
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    # Classic unlock: tap ALT, then retry.
    try:
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    return win32gui.GetForegroundWindow() == hwnd


def op_focus_window(body: dict) -> dict:
    require("write", "focus_window")
    title_q = str(body.get("title", body.get("window", "")))
    matches = _find_windows(title_q)
    if not matches:
        return _fail(f'no visible window matching "{title_q}"')
    hwnd, title = matches[0]
    focused = _force_foreground(hwnd)
    if not focused:
        return _fail("window found but Windows refused foreground activation", window=title,
                     handle=hwnd, hint="try again or click the taskbar")
    return _ok(action="focus_window", window=title, handle=hwnd, focused=True)


def op_minimize_window(body: dict) -> dict:
    require("write", "minimize_window")
    win32gui, win32con = _win32()
    matches = _find_windows(str(body.get("title", body.get("window", ""))))
    if not matches:
        return _fail("no matching window")
    hwnd, title = matches[0]
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    return _ok(action="minimize_window", window=title, handle=hwnd)


def op_maximize_window(body: dict) -> dict:
    require("write", "maximize_window")
    win32gui, win32con = _win32()
    matches = _find_windows(str(body.get("title", body.get("window", ""))))
    if not matches:
        return _fail("no matching window")
    hwnd, title = matches[0]
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    return _ok(action="maximize_window", window=title, handle=hwnd)


APP_ALIASES = {
    "notepad": "notepad", "calc": "calc", "calculator": "calc", "code": "code",
    "vscode": "code", "chrome": "chrome", "edge": "msedge", "explorer": "explorer",
    "cmd": "cmd", "terminal": "wt", "powershell": "powershell", "spotify": "spotify",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "paint": "mspaint",
    "settings": "ms-settings:", "whatsapp": "whatsapp", "telegram": "telegram",
}


def op_open_application(body: dict) -> dict:
    require("write", "open_application")
    target = str(body.get("name", body.get("application", body.get("path", "")))).strip()
    args = body.get("args") or []
    if not target:
        return _fail("name or path is required")
    resolved = APP_ALIASES.get(target.lower(), target)
    p = Path(resolved)
    try:
        if p.exists():
            os.startfile(str(p))
            return _ok(action="open_application", opened=str(p))
        cmd = [resolved] + [str(a) for a in args] if args else resolved
        subprocess.Popen(["cmd", "/c", "start", "", cmd if isinstance(cmd, str) else " ".join(cmd)],
                         shell=False, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        return _ok(action="open_application", opened=resolved)
    except Exception as e:
        return _fail(f"could not open {target}: {e}")


def op_close_application(body: dict) -> dict:
    require("write", "close_application")
    title = str(body.get("title", body.get("window", ""))).strip()
    name = str(body.get("name", body.get("process", ""))).strip()
    if title:
        win32gui, win32con = _win32()
        matches = _find_windows(title)
        if not matches:
            return _fail(f'no window matching "{title}"')
        closed = []
        for hwnd, t in matches[:5]:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            closed.append({"handle": hwnd, "title": t})
        return _ok(action="close_application", closed=closed)
    if name:
        procs = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info.get("name") or "").lower() == name.lower() or \
                   (p.info.get("name") or "").lower().startswith(name.lower()):
                    p.terminate()
                    procs.append({"pid": p.info["pid"], "name": p.info["name"]})
            except Exception:
                continue
        if not procs:
            return _fail(f"no process named {name}")
        return _ok(action="close_application", closed=procs)
    return _fail("title or name is required")


# --------------------------------------------------------------------------- input tools
def op_keyboard_type(body: dict) -> dict:
    require("write", "keyboard_type")
    text = str(body.get("text", ""))
    if not text:
        return _fail("text is required")
    import pyautogui
    import pyperclip
    previous = ""
    try:
        previous = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    time.sleep(0.15)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)
    if body.get("restore_clipboard", False):
        try:
            pyperclip.copy(previous)
        except Exception:
            pass
    return _ok(action="keyboard_type", chars=len(text), unicode=True)


def op_keyboard_shortcut(body: dict) -> dict:
    require("write", "keyboard_shortcut")
    raw = body.get("keys") or body.get("shortcut") or ""
    if isinstance(raw, list):
        keys = [str(k).strip().lower() for k in raw]
    else:
        keys = [k.strip().lower() for k in str(raw).replace(" ", "").split("+") if k.strip()]
    if not keys:
        return _fail("keys are required (e.g. ctrl+c)")
    import pyautogui
    pyautogui.hotkey(*keys)
    return _ok(action="keyboard_shortcut", keys=keys)


def op_keyboard_press(body: dict) -> dict:
    require("write", "keyboard_press")
    key = str(body.get("key", "")).strip().lower()
    if not key:
        return _fail("key is required")
    import pyautogui
    pyautogui.press(key, presses=int(body.get("times", 1)), interval=float(body.get("interval", 0.05)))
    return _ok(action="keyboard_press", key=key)


def op_mouse_click(body: dict) -> dict:
    require("write", "mouse_click")
    import pyautogui
    x, y = int(body.get("x", 0)), int(body.get("y", 0))
    button = str(body.get("button", "left")).lower()
    clicks = 2 if (body.get("double") or str(body.get("click_type", "")).lower() == "double") else int(body.get("clicks", 1))
    pyautogui.click(x=x, y=y, clicks=clicks, interval=0.12, button=button)
    return _ok(action="mouse_click", x=x, y=y, button=button, clicks=clicks)


def op_mouse_move(body: dict) -> dict:
    require("write", "mouse_move")
    import pyautogui
    x, y = int(body.get("x", 0)), int(body.get("y", 0))
    pyautogui.moveTo(x, y, duration=float(body.get("duration", 0.25)))
    return _ok(action="mouse_move", x=x, y=y)


def op_mouse_scroll(body: dict) -> dict:
    require("write", "mouse_scroll")
    import pyautogui
    amount = int(body.get("amount", body.get("clicks", -3)))
    x, y = body.get("x"), body.get("y")
    if x is not None and y is not None:
        pyautogui.moveTo(int(x), int(y), duration=0.1)
    pyautogui.scroll(amount)
    return _ok(action="mouse_scroll", amount=amount)


# --------------------------------------------------------------------------- browser tools
class BrowserManager:
    def __init__(self):
        self.pw = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()

    async def ensure_page(self, headless: bool | None = None):
        from playwright.async_api import async_playwright
        if self.page is not None and not self.page.is_closed():
            return self.page
        if self.pw is None:
            self.pw = await async_playwright().start()
        if self.context is None:
            BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
            kw = dict(
                user_data_dir=str(BROWSER_PROFILE),
                headless=bool(headless) if headless is not None else False,
                viewport={"width": 1440, "height": 900},
                args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            )
            try:
                self.context = await self.pw.chromium.launch_persistent_context(channel="chrome", **kw)
            except Exception:
                self.context = await self.pw.chromium.launch_persistent_context(**kw)
            self.context.set_default_timeout(45000)
        pages = [p for p in self.context.pages if not p.is_closed()]
        self.page = pages[0] if pages else await self.context.new_page()
        return self.page

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.pw = None


BROWSER = BrowserManager()


def _selector_or_text(body: dict):
    sel = str(body.get("selector", "")).strip()
    text = str(body.get("text", "")).strip()
    return sel, text


async def op_browser_open(body: dict) -> dict:
    require("write", "browser_open")
    url = str(body.get("url", "")).strip()
    if not url:
        return _fail("url is required")
    async with BROWSER.lock:
        page = await BROWSER.ensure_page(headless=bool(body.get("headless", False)))
        resp = await page.goto(url, wait_until=str(body.get("wait_until", "domcontentloaded")), timeout=int(body.get("timeout", 60000)))
        return _ok(action="browser_open", url=page.url, title=await page.title(),
                   status=resp.status if resp else None)


async def op_browser_navigate(body: dict) -> dict:
    require("write", "browser_navigate")
    url = str(body.get("url", "")).strip()
    if not url:
        return _fail("url is required")
    async with BROWSER.lock:
        page = await BROWSER.ensure_page()
        resp = await page.goto(url, wait_until=str(body.get("wait_until", "domcontentloaded")), timeout=int(body.get("timeout", 60000)))
        return _ok(action="browser_navigate", url=page.url, title=await page.title(),
                   status=resp.status if resp else None)


async def op_browser_click(body: dict) -> dict:
    require("write", "browser_click")
    sel, text = _selector_or_text(body)
    async with BROWSER.lock:
        page = await BROWSER.ensure_page()
        if sel:
            try:
                await page.click(sel, timeout=int(body.get("timeout", 15000)))
                return _ok(action="browser_click", selector=sel, url=page.url)
            except Exception as e:
                if not text:
                    return _fail(f"click failed: {e}", selector=sel)
        if text:
            try:
                loc = page.get_by_text(text, exact=bool(body.get("exact", False))).first
                await loc.click(timeout=int(body.get("timeout", 15000)))
                return _ok(action="browser_click", text=text, url=page.url)
            except Exception as e:
                return _fail(f"click by text failed: {e}", text=text)
        return _fail("selector or text is required")


async def op_browser_type(body: dict) -> dict:
    require("write", "browser_type")
    sel = str(body.get("selector", "")).strip()
    text = str(body.get("text", ""))
    if not sel:
        return _fail("selector is required")
    async with BROWSER.lock:
        page = await BROWSER.ensure_page()
        try:
            await page.fill(sel, text, timeout=int(body.get("timeout", 15000)))
        except Exception:
            await page.click(sel, timeout=int(body.get("timeout", 15000)))
            await page.keyboard.type(text, delay=int(body.get("delay", 20)))
        if body.get("submit"):
            await page.keyboard.press("Enter")
        return _ok(action="browser_type", selector=sel, chars=len(text))


async def op_browser_extract(body: dict) -> dict:
    require("read", "browser_extract")
    sel = str(body.get("selector", "")).strip() or "body"
    mode = str(body.get("mode", "text")).lower()
    async with BROWSER.lock:
        page = await BROWSER.ensure_page()
        try:
            loc = page.locator(sel).first
            if mode in ("html", "inner_html"):
                content = await loc.inner_html(timeout=int(body.get("timeout", 15000)))
            elif mode == "attribute":
                content = await loc.get_attribute(str(body.get("attribute", "href")))
            elif mode == "outer_html":
                content = await loc.evaluate("el => el.outerHTML")
            else:
                content = await loc.inner_text(timeout=int(body.get("timeout", 15000)))
        except Exception as e:
            return _fail(f"extract failed: {e}", selector=sel)
        limit = int(body.get("limit", 50000))
        content = content or ""
        return _ok(action="browser_extract", selector=sel, mode=mode,
                   length=len(content), content=content[:limit], truncated=len(content) > limit)


async def op_browser_screenshot(body: dict) -> dict:
    require("read", "browser_screenshot")
    async with BROWSER.lock:
        page = await BROWSER.ensure_page()
        data = await page.screenshot(full_page=bool(body.get("full_page", False)), type="png")
        path = _now_name("browser", ".png")
        path.write_bytes(data)
        payload = _ok(action="browser_screenshot", path=str(path), url=page.url,
                      size_bytes=len(data), mime="image/png")
        if body.get("include_base64"):
            payload["image_base64"] = base64.b64encode(data).decode("ascii")
        return payload


async def op_browser_execute_js(body: dict) -> dict:
    require("full", "browser_execute_js")
    script = str(body.get("script", body.get("js", "")))
    if not script:
        return _fail("script is required")
    async with BROWSER.lock:
        page = await BROWSER.ensure_page()
        try:
            result = await page.evaluate(script)
        except Exception as e:
            return _fail(f"js execution failed: {e}")
        try:
            json.dumps(result)
        except Exception:
            result = str(result)
        return _ok(action="browser_execute_js", result=result, url=page.url)


async def op_browser_scroll(body: dict) -> dict:
    require("write", "browser_scroll")
    amount = int(body.get("amount", body.get("y", 600)))
    async with BROWSER.lock:
        page = await BROWSER.ensure_page()
        if body.get("selector"):
            await page.locator(str(body["selector"])).first.scroll_into_view_if_needed()
        else:
            await page.evaluate("(y) => window.scrollBy(0, y)", amount)
        await page.wait_for_timeout(120)
        pos = await page.evaluate("() => window.scrollY")
        return _ok(action="browser_scroll", amount=amount, scroll_y=pos)


async def op_browser_close(body: dict) -> dict:
    require("write", "browser_close")
    async with BROWSER.lock:
        await BROWSER.close()
    return _ok(action="browser_close", closed=True)


async def op_browser_get_url(body: dict) -> dict:
    require("read", "browser_get_url")
    async with BROWSER.lock:
        page = await BROWSER.ensure_page()
        return _ok(action="browser_get_url", url=page.url)


async def op_browser_get_title(body: dict) -> dict:
    require("read", "browser_get_title")
    async with BROWSER.lock:
        page = await BROWSER.ensure_page()
        return _ok(action="browser_get_title", title=await page.title(), url=page.url)


# --------------------------------------------------------------------------- browser recipes (C4)
BROWSER_MONITORS = BASE_DIR / "browser_monitors.json"


def _browser_merged(body: dict) -> dict:
    """Browser recipes accept both flat inputs and a nested `params` object.

    n8n workflow tools expose a single `params` (any) input for rich structures
    such as field lists, action lists and credential objects. Flat values win.
    """
    merged: dict = {}
    params = body.get("params")
    if isinstance(params, dict):
        merged.update(params)
    for key, value in body.items():
        if key != "params" and value is not None:
            merged[key] = value
    return merged


def _browser_int(body: dict, name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        value = int(body.get(name, default))
    except Exception:
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _browser_str(body: dict, *names: str, default: str = "") -> str:
    for name in names:
        value = body.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def _css_attr(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _browser_clean_text(text: Any, collapse: bool = True) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip() if collapse else line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


async def _browser_region(page, selector: str, mode: str, timeout: int) -> str:
    loc = page.locator(selector or "body").first
    mode = (mode or "text").lower()
    if mode in ("html", "inner_html", "innerhtml"):
        return str(await loc.inner_html(timeout=timeout) or "")
    if mode in ("outer_html", "outerhtml"):
        return str(await loc.evaluate("el => el.outerHTML") or "")
    return str(await loc.inner_text(timeout=timeout) or "")


async def _browser_first_visible(page, selectors: list[str]):
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                return loc, selector
        except Exception:
            continue
    return None, None


def _browser_fields(body: dict) -> list[dict]:
    raw = body.get("fields", body.get("values", body.get("form")))
    if isinstance(raw, dict):
        return [{"selector": key, "value": value} for key, value in raw.items()]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    selector = _browser_str(body, "selector")
    if selector:
        return [{"selector": selector, "value": body.get("value", body.get("text", ""))}]
    return []


def _browser_field_locator(page, field: dict):
    selector = str(field.get("selector") or "").strip()
    if selector:
        return page.locator(selector).first, selector
    name = str(field.get("name") or "").strip()
    if name:
        css = f'[name="{_css_attr(name)}"]'
        return page.locator(css).first, css
    field_id = str(field.get("id") or "").strip()
    if field_id:
        css = f'#{field_id.lstrip("#")}'
        return page.locator(css).first, css
    label = str(field.get("label") or "").strip()
    if label:
        loc = page.get_by_label(label, exact=bool(field.get("exact", False))).first
        return loc, f'label "{label}"'
    placeholder = str(field.get("placeholder") or "").strip()
    if placeholder:
        loc = page.get_by_placeholder(placeholder, exact=bool(field.get("exact", False))).first
        return loc, f'placeholder "{placeholder}"'
    return None, ""


async def _browser_apply_field(page, field: dict, timeout: int) -> dict:
    loc, descriptor = _browser_field_locator(page, field)
    if loc is None:
        raise ValueError("each field needs selector, name, id, label or placeholder")
    action = str(field.get("action") or field.get("type") or "fill").lower().strip()
    if action not in ("fill", "type", "select", "check", "uncheck", "click", "press", "upload"):
        action = "fill"
    value = field.get("value", field.get("text", ""))
    if action == "fill":
        try:
            await loc.fill(str(value), timeout=timeout)
        except Exception:
            await loc.click(timeout=timeout)
            await page.keyboard.press("Control+A")
            await page.keyboard.type(str(value), delay=int(field.get("delay", 15)))
        if field.get("press_enter"):
            await loc.press("Enter", timeout=timeout)
    elif action == "type":
        await loc.click(timeout=timeout)
        if field.get("clear", True):
            await page.keyboard.press("Control+A")
        await page.keyboard.type(str(value), delay=int(field.get("delay", 20)))
        if field.get("press_enter"):
            await page.keyboard.press("Enter")
    elif action == "select":
        if isinstance(value, dict):
            await loc.select_option(**value, timeout=timeout)
        elif isinstance(value, list):
            await loc.select_option(value, timeout=timeout)
        else:
            try:
                await loc.select_option(value=str(value), timeout=timeout)
            except Exception:
                try:
                    await loc.select_option(label=str(value), timeout=timeout)
                except Exception:
                    await loc.select_option(index=int(value), timeout=timeout)
    elif action == "check":
        await loc.check(timeout=timeout)
    elif action == "uncheck":
        await loc.uncheck(timeout=timeout)
    elif action == "click":
        await loc.click(timeout=timeout)
    elif action == "press":
        await loc.press(str(value or field.get("key") or "Enter"), timeout=timeout)
    elif action == "upload":
        await loc.set_input_files(value, timeout=timeout)
    return {
        "field": descriptor,
        "action": action,
        "ok": True,
        "value": "***" if ("password" in descriptor.lower() or "password" in str(field.get("name", "")).lower()
                           or field.get("secret")) else str(value)[:300],
    }


async def op_browser_fill_form(body: dict) -> dict:
    require("write", "browser_fill_form")
    body = _browser_merged(body)
    url = _browser_str(body, "url")
    fields = _browser_fields(body)
    if not fields:
        return _fail("fields is required (array of {selector|name|label|placeholder, value, action})")
    timeout = _browser_int(body, "timeout", 20000, 1000, 120000)
    wait_after = _browser_int(body, "wait_after_ms", 0, 0, 60000)
    async with BROWSER.lock:
        page = await BROWSER.ensure_page(headless=bool(body.get("headless", False)))
        if url:
            await page.goto(url, wait_until=str(body.get("wait_until", "domcontentloaded")), timeout=timeout)
        wait_for = _browser_str(body, "wait_for", "wait_for_selector")
        if wait_for:
            await page.locator(wait_for).first.wait_for(state="visible", timeout=timeout)
        filled, errors = [], []
        for field in fields:
            try:
                filled.append(await _browser_apply_field(page, field, timeout))
            except Exception as e:
                errors.append({"field": field.get("selector") or field.get("name") or field.get("label") or "?",
                               "error": str(e)[:500]})
        submitted = False
        submit = body.get("submit", body.get("submit_selector"))
        submit_text = _browser_str(body, "submit_text")
        try:
            if submit_text:
                await page.get_by_text(submit_text, exact=bool(body.get("exact", False))).first.click(timeout=timeout)
                submitted = True
            elif isinstance(submit, str) and submit.strip():
                try:
                    await page.locator(submit).first.click(timeout=timeout)
                except Exception:
                    await page.get_by_text(submit, exact=False).first.click(timeout=timeout)
                submitted = True
            elif submit is True:
                await page.keyboard.press(str(body.get("submit_key") or "Enter"))
                submitted = True
        except Exception as e:
            errors.append({"field": "submit", "error": str(e)[:500]})
        if body.get("wait_for_after") or body.get("success_selector"):
            try:
                await page.locator(str(body.get("wait_for_after") or body.get("success_selector"))).first.wait_for(
                    state="visible", timeout=timeout)
            except Exception as e:
                errors.append({"field": "wait_for_after", "error": str(e)[:500]})
        if wait_after:
            await page.wait_for_timeout(wait_after)
        result = _ok(action="browser_fill_form", url=page.url, title=await page.title(),
                     filled=filled, errors=errors, submitted=submitted,
                     filled_count=len(filled), error_count=len(errors))
        if body.get("screenshot"):
            data = await page.screenshot(full_page=bool(body.get("full_page", False)), type="png")
            path = _now_name("form", ".png")
            path.write_bytes(data)
            result["screenshot"] = str(path)
        return result


def _browser_table_rows(raw_table: dict, header_mode: str, limit_rows: int, limit_cols: int) -> dict:
    grid: list[list[str]] = []
    for raw_row in raw_table.get("rows") or []:
        cells = []
        for cell in raw_row or []:
            if isinstance(cell, dict):
                cells.append(str(cell.get("text", ""))[:2000])
            else:
                cells.append(str(cell)[:2000])
        while cells and not cells[-1]:
            cells.pop()
        if any(c.strip() for c in cells):
            grid.append(cells[:limit_cols])
    if not grid:
        return {"rows": [], "objects": [], "headers": [], "row_count": 0, "column_count": 0,
                "truncated": False, "header_row_index": -1}
    detected = int(raw_table.get("headerRow", -1))
    mode = (header_mode or "auto").lower()
    if mode in ("false", "0", "no", "none"):
        header_idx = -1
    elif mode in ("true", "1", "yes", "first"):
        header_idx = 0
    else:
        header_idx = detected if 0 <= detected < len(grid) else 0
    if header_idx >= 0:
        raw_headers = grid[header_idx]
        headers, seen = [], {}
        for i in range(max(len(raw_headers), max((len(r) for r in grid), default=0))):
            base = raw_headers[i].strip() if i < len(raw_headers) and raw_headers[i].strip() else f"Column {i + 1}"
            seen[base] = seen.get(base, 0) + 1
            headers.append(base if seen[base] == 1 else f"{base} ({seen[base]})")
        data_rows = grid[header_idx + 1:]
    else:
        width = max((len(r) for r in grid), default=0)
        headers = [f"Column {i + 1}" for i in range(width)]
        data_rows = grid
    padded = [(row + [""] * len(headers))[:len(headers)] for row in data_rows]
    truncated = len(padded) > limit_rows
    padded = padded[:limit_rows]
    objects = [dict(zip(headers, row)) for row in padded]
    return {
        "headers": headers,
        "rows": padded,
        "objects": objects,
        "row_count": len(padded),
        "column_count": len(headers),
        "truncated": truncated,
        "header_row_index": header_idx,
    }


async def op_browser_extract_table(body: dict) -> dict:
    require("read", "browser_extract_table")
    body = _browser_merged(body)
    url = _browser_str(body, "url")
    selector = _browser_str(body, "selector", default="table")
    row_selector = _browser_str(body, "row_selector")
    cell_selector = _browser_str(body, "cell_selector", default="td,th")
    timeout = _browser_int(body, "timeout", 20000, 1000, 120000)
    limit_rows = _browser_int(body, "limit_rows", 1000, 1, 5000)
    limit_cols = _browser_int(body, "limit_cols", 50, 1, 200)
    max_tables = _browser_int(body, "max_tables", 25, 1, 100)
    header_mode = str(body.get("header", "auto"))
    async with BROWSER.lock:
        page = await BROWSER.ensure_page(headless=bool(body.get("headless", False)))
        if url:
            await page.goto(url, wait_until=str(body.get("wait_until", "domcontentloaded")), timeout=timeout)
        js = """
        (args) => {
          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const cellText = (c) => {
            let text = norm(c.innerText || c.textContent || '');
            if (!text) {
              const input = c.querySelector && c.querySelector('input,select,textarea');
              if (input && 'value' in input) text = norm(input.value);
            }
            return text;
          };
          const tables = [];
          if (args.rowSelector) {
            const rows = Array.from(document.querySelectorAll(args.rowSelector)).slice(0, args.maxRows);
            const grid = rows.map((row) => Array.from(row.querySelectorAll(args.cellSelector))
              .map((c) => ({text: cellText(c), th: c.tagName === 'TH'})));
            tables.push({rows: grid, headerRow: grid.findIndex((r) => r.some((c) => c.th))});
          } else {
            const nodes = Array.from(document.querySelectorAll(args.selector)).slice(0, args.maxTables);
            for (const table of nodes) {
              const grid = Array.from(table.querySelectorAll('tr')).slice(0, args.maxRows)
                .map((tr) => Array.from(tr.querySelectorAll('th,td'))
                  .map((c) => ({text: cellText(c), th: c.tagName === 'TH'})));
              tables.push({rows: grid, headerRow: grid.findIndex((r) => r.some((c) => c.th))});
            }
          }
          return {url: location.href, title: document.title, tables};
        }
        """
        raw = await page.evaluate(js, {
            "selector": selector, "rowSelector": row_selector, "cellSelector": cell_selector,
            "maxTables": max_tables, "maxRows": max(limit_rows + 5, 100),
        })
        tables = raw.get("tables") or []
        if not tables:
            hint = f'selector "{row_selector or selector}" matched no rows/tables'
            return _fail(f"no table found ({hint})", url=page.url)
        parsed = []
        for i, table in enumerate(tables):
            item = _browser_table_rows(table, header_mode, limit_rows, limit_cols)
            item["index"] = i
            parsed.append(item)
        want_all = bool(body.get("all")) or str(body.get("index", "")).lower() == "all"
        if want_all:
            selected = parsed
            primary = parsed[0]
        else:
            try:
                index = int(body.get("index", 0))
            except Exception:
                index = 0
            if index < 0 or index >= len(parsed):
                return _fail(f"table index {index} out of range (found {len(parsed)})")
            primary = parsed[index]
            selected = [primary]
        result = _ok(action="browser_extract_table", url=raw.get("url") or page.url,
                     title=raw.get("title") or await page.title(),
                     table_count=len(parsed), selector=row_selector or selector,
                     headers=primary["headers"], rows=primary["rows"], objects=primary["objects"],
                     row_count=primary["row_count"], column_count=primary["column_count"],
                     truncated=primary["truncated"], header_row_index=primary["header_row_index"])
        if want_all:
            result["tables"] = selected
        return result


def _browser_monitor_name(body: dict, url: str, selector: str) -> str:
    name = _browser_str(body, "name", "monitor")
    if not name:
        name = "mon_" + hashlib.sha1(f"{url}|{selector}".encode()).hexdigest()[:12]
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-._")[:80] or "monitor"


def _browser_diff(before: str, after: str, max_lines: int = 80) -> dict:
    diff_lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                           fromfile="before", tofile="after", n=2, lineterm=""))
    added = [line[1:] for line in diff_lines if line.startswith("+") and not line.startswith("+++")]
    removed = [line[1:] for line in diff_lines if line.startswith("-") and not line.startswith("---")]
    return {
        "diff": "\n".join(diff_lines[:max_lines])[:12000],
        "diff_lines": len(diff_lines),
        "added": added[:20],
        "removed": removed[:20],
        "added_count": len(added),
        "removed_count": len(removed),
    }


async def op_browser_monitor_page(body: dict) -> dict:
    require("read", "browser_monitor_page")
    body = _browser_merged(body)
    url = _browser_str(body, "url")
    explicit_selector = _browser_str(body, "selector")
    explicit_mode = _browser_str(body, "mode").lower()
    timeout = _browser_int(body, "timeout", 30000, 1000, 120000)
    max_chars = _browser_int(body, "max_chars", 200000, 1000, 1000000)
    wait_seconds = _browser_int(body, "wait_seconds", 0, 0, 300)
    interval = _browser_int(body, "interval_seconds", 5, 1, 60)
    collapse = bool(body.get("collapse_whitespace", True))
    monitors = load_json(BROWSER_MONITORS, {})
    if not isinstance(monitors, dict):
        monitors = {}
    name = _browser_monitor_name(body, url, explicit_selector)
    if body.get("delete"):
        existed = name in monitors
        monitors.pop(name, None)
        save_json(BROWSER_MONITORS, monitors)
        return _ok(action="browser_monitor_page", monitor=name, deleted=existed)
    previous = monitors.get(name) or {}
    selector = explicit_selector or str(previous.get("selector") or "body")
    mode = explicit_mode or str(previous.get("mode") or "text").lower()
    if not url:
        url = str(previous.get("url") or "").strip()
    if not url:
        return _fail("url is required the first time a monitor is created")
    async with BROWSER.lock:
        page = await BROWSER.ensure_page(headless=bool(body.get("headless", False)))
        await page.goto(url, wait_until=str(body.get("wait_until", "domcontentloaded")), timeout=timeout)
        deadline = time.time() + wait_seconds
        checks = 0
        timed_out = False
        while True:
            checks += 1
            text = await _browser_region(page, selector, mode, timeout)
            if mode in ("html", "inner_html", "innerhtml", "outer_html", "outerhtml"):
                text = str(text or "")
            else:
                text = _browser_clean_text(text, collapse=collapse)
            text = text[:max_chars]
            digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
            unchanged = bool(previous) and digest == previous.get("hash")
            if not previous or not unchanged or time.time() >= deadline:
                break
            await asyncio.sleep(interval)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=timeout)
            except Exception:
                pass
        if previous and digest == previous.get("hash"):
            timed_out = wait_seconds > 0
        now = datetime.now(timezone.utc).isoformat()
        changed = bool(previous) and digest != previous.get("hash")
        baseline = not previous
        if baseline or changed or body.get("reset"):
            monitors[name] = {
                "url": url, "selector": selector, "mode": mode, "hash": digest,
                "text": text, "updated_at": now, "checks": int(previous.get("checks", 0)) + checks,
            }
            save_json(BROWSER_MONITORS, monitors)
        result = _ok(action="browser_monitor_page", monitor=name, url=page.url,
                     title=await page.title(), selector=selector, mode=mode,
                     changed=changed, baseline=baseline or bool(body.get("reset")),
                     timed_out=timed_out, checks=int(previous.get("checks", 0)) + checks,
                     hash_before=previous.get("hash"), hash_after=digest,
                     length=len(text), checked_at=now)
        if previous and (changed or body.get("include_unchanged_diff")):
            result.update(_browser_diff(str(previous.get("text", "")), text,
                                        _browser_int(body, "max_diff_lines", 80, 5, 500)))
        if body.get("include_text"):
            result["text"] = text[:20000]
        return result


_BROWSER_USER_SELECTORS = [
    'input[autocomplete="username"]', 'input[type="email"]', 'input[name="email"]',
    'input[name="username"]', 'input[id*="email" i]', 'input[id*="user" i]',
    'input[type="text"]',
]
_BROWSER_PASS_SELECTORS = [
    'input[autocomplete="current-password"]', 'input[type="password"]',
    'input[name="password"]', 'input[id*="password" i]',
]


async def _browser_run_actions(page, actions: list, timeout: int, stop_on_error: bool = False) -> list:
    results = []
    for i, item in enumerate(actions or []):
        if not isinstance(item, dict):
            results.append({"index": i, "ok": False, "error": "action must be an object"})
            continue
        action = str(item.get("action") or item.get("type") or "click").lower().strip()
        selector = str(item.get("selector") or "").strip()
        value = item.get("value", item.get("text", ""))
        try:
            detail: dict = {}
            if action in ("goto", "navigate", "open"):
                target = str(item.get("url") or value or "")
                if not target:
                    raise ValueError("url is required")
                await page.goto(target, wait_until=str(item.get("wait_until", "domcontentloaded")), timeout=timeout)
            elif action == "click":
                if selector:
                    await page.locator(selector).first.click(timeout=timeout)
                elif value:
                    await page.get_by_text(str(value), exact=bool(item.get("exact", False))).first.click(timeout=timeout)
                else:
                    raise ValueError("selector or text is required")
            elif action in ("fill", "type"):
                if not selector:
                    raise ValueError("selector is required")
                loc = page.locator(selector).first
                if action == "fill":
                    try:
                        await loc.fill(str(value), timeout=timeout)
                    except Exception:
                        await loc.click(timeout=timeout)
                        await page.keyboard.press("Control+A")
                        await page.keyboard.type(str(value), delay=int(item.get("delay", 15)))
                else:
                    await loc.click(timeout=timeout)
                    if item.get("clear", True):
                        await page.keyboard.press("Control+A")
                    await page.keyboard.type(str(value), delay=int(item.get("delay", 20)))
                if item.get("press_enter"):
                    await loc.press("Enter", timeout=timeout)
            elif action == "select":
                if not selector:
                    raise ValueError("selector is required")
                loc = page.locator(selector).first
                if isinstance(value, dict):
                    await loc.select_option(**value, timeout=timeout)
                elif isinstance(value, list):
                    await loc.select_option(value, timeout=timeout)
                else:
                    try:
                        await loc.select_option(value=str(value), timeout=timeout)
                    except Exception:
                        await loc.select_option(label=str(value), timeout=timeout)
            elif action in ("check", "uncheck"):
                if not selector:
                    raise ValueError("selector is required")
                loc = page.locator(selector).first
                await (loc.check(timeout=timeout) if action == "check" else loc.uncheck(timeout=timeout))
            elif action in ("press", "key"):
                await page.keyboard.press(str(item.get("key") or value or "Enter"))
            elif action in ("wait", "sleep"):
                try:
                    default_ms = int(value or 1000)
                except Exception:
                    default_ms = 1000
                await page.wait_for_timeout(_browser_int(item, "ms", default_ms, 0, 120000))
            elif action in ("wait_for", "wait_for_selector"):
                if not selector:
                    raise ValueError("selector is required")
                await page.locator(selector).first.wait_for(
                    state=str(item.get("state", "visible")), timeout=timeout)
            elif action == "extract":
                content = await _browser_region(page, selector or "body",
                                                str(item.get("mode", "text")), timeout)
                detail["content"] = content[:int(item.get("limit", 5000))]
            elif action == "scroll":
                await page.evaluate("(y) => window.scrollBy(0, y)", _browser_int(item, "amount", 600))
            elif action == "reload":
                await page.reload(wait_until=str(item.get("wait_until", "domcontentloaded")), timeout=timeout)
            elif action in ("back", "go_back"):
                await page.go_back(wait_until=str(item.get("wait_until", "domcontentloaded")), timeout=timeout)
            elif action == "screenshot":
                path = _now_name("action", ".png")
                path.write_bytes(await page.screenshot(full_page=bool(item.get("full_page", False)), type="png"))
                detail["path"] = str(path)
            else:
                raise ValueError(f'unknown action "{action}"')
            results.append({"index": i, "action": action, "ok": True, **detail})
        except Exception as e:
            results.append({"index": i, "action": action, "ok": False, "error": str(e)[:500]})
            if stop_on_error:
                break
    return results


async def op_browser_login_and_action(body: dict) -> dict:
    require("write", "browser_login_and_action")
    body = _browser_merged(body)
    login_url = _browser_str(body, "login_url", "url")
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    timeout = _browser_int(body, "timeout", 30000, 1000, 180000)
    wait_after = _browser_int(body, "wait_after_ms", 1000, 0, 60000)
    user_selector = _browser_str(body, "username_selector", "email_selector")
    pass_selector = _browser_str(body, "password_selector")
    submit_selector = _browser_str(body, "submit_selector")
    submit_text = _browser_str(body, "submit_text")
    success_selector = _browser_str(body, "success_selector", "logged_in_selector")
    success_text = _browser_str(body, "success_text")
    success_url = _browser_str(body, "success_url_contains")
    actions = body.get("actions") or []
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list):
        return _fail("actions must be an array")
    if not login_url:
        return _fail("login_url (or url) is required")
    async with BROWSER.lock:
        page = await BROWSER.ensure_page(headless=bool(body.get("headless", False)))
        await page.goto(login_url, wait_until=str(body.get("wait_until", "domcontentloaded")), timeout=timeout)
        already = False
        if success_selector:
            try:
                loc = page.locator(success_selector).first
                already = await loc.count() > 0 and await loc.is_visible()
            except Exception:
                already = False
        if already and body.get("skip_if_logged_in", True):
            login = {"skipped": True, "reason": "already logged in (success selector visible)"}
            verified = True
        else:
            user_loc, user_used = (page.locator(user_selector).first, user_selector) \
                if user_selector else await _browser_first_visible(page, _BROWSER_USER_SELECTORS)
            pass_loc, pass_used = (page.locator(pass_selector).first, pass_selector) \
                if pass_selector else await _browser_first_visible(page, _BROWSER_PASS_SELECTORS)
            login = {"skipped": False, "username_selector": user_used, "password_selector": pass_used}
            if username:
                if user_loc is None:
                    return _fail("login username field not found", url=page.url,
                                 hint="pass username_selector")
                await user_loc.fill(username, timeout=timeout)
                login["username_filled"] = True
            if password:
                if pass_loc is None:
                    return _fail("login password field not found", url=page.url,
                                 hint="pass password_selector")
                await pass_loc.fill(password, timeout=timeout)
                login["password_filled"] = True
            try:
                if submit_selector:
                    await page.locator(submit_selector).first.click(timeout=timeout)
                elif submit_text:
                    await page.get_by_text(submit_text, exact=bool(body.get("exact", False))).first.click(timeout=timeout)
                else:
                    if pass_loc is None:
                        return _fail("no password field or submit target found", url=page.url)
                    await pass_loc.press("Enter", timeout=timeout)
                login["submitted"] = True
            except Exception as e:
                return _fail(f"login submit failed: {e}", url=page.url)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
            except Exception:
                pass
        if wait_after:
            await page.wait_for_timeout(wait_after)
        verified = bool(already)
        if success_selector:
            try:
                await page.locator(success_selector).first.wait_for(state="visible", timeout=timeout)
                verified = True
            except Exception:
                verified = False
        elif success_text:
            try:
                await page.get_by_text(success_text, exact=False).first.wait_for(state="visible", timeout=timeout)
                verified = True
            except Exception:
                verified = False
        elif success_url:
            verified = success_url in page.url
        else:
            try:
                password_left = await page.locator('input[type="password"]').count()
                verified = password_left == 0
            except Exception:
                verified = False
        if not verified and not body.get("allow_unverified"):
            return _fail("login could not be verified — pass success_selector/success_text/success_url_contains "
                         "or allow_unverified=true", url=page.url, login=login)
        action_url = _browser_str(body, "action_url", "after_url")
        if action_url:
            await page.goto(action_url, wait_until=str(body.get("wait_until", "domcontentloaded")), timeout=timeout)
        action_results = await _browser_run_actions(page, actions, timeout,
                                                    bool(body.get("stop_on_error", False)))
        result = _ok(action="browser_login_and_action", url=page.url, title=await page.title(),
                     logged_in=verified, verified=verified, login=login,
                     session="persistent", profile=str(BROWSER_PROFILE),
                     actions=action_results, action_count=len(action_results),
                     action_errors=sum(1 for a in action_results if not a.get("ok")))
        if body.get("save_state"):
            state_path = Path(str(body.get("save_state")))
            if not state_path.is_absolute():
                state_path = (BASE_DIR / state_path).resolve()
            state_path.parent.mkdir(parents=True, exist_ok=True)
            await BROWSER.context.storage_state(path=str(state_path))
            result["state_file"] = str(state_path)
        if body.get("screenshot"):
            data = await page.screenshot(full_page=bool(body.get("full_page", False)), type="png")
            path = _now_name("login", ".png")
            path.write_bytes(data)
            result["screenshot"] = str(path)
        return result


# --------------------------------------------------------------------------- permission tools
async def op_permission(body: dict) -> dict:
    action = str(body.get("action", "get_mode")).lower()

    if action == "get_mode":
        return _ok(action="get_mode", mode=read_mode(),
                   levels=LEVELS, role=ROLE,
                   capabilities={
                       "read": "view screenshots, list files/windows/processes, read files, extract pages",
                       "write": "type, click, open/close apps, write files, browser actions, sleep",
                       "full": "shell commands, shutdown/restart, delete files/folders, execute JS",
                   })

    if action != "set_mode":
        return _fail(f'unknown permission action "{action}" (use get_mode or set_mode)')

    mode = str(body.get("mode", "")).lower().strip()
    if mode not in LEVELS:
        return _fail(f'invalid mode "{mode}" (full | write | read | none)')

    current = read_mode()
    if mode == current:
        return _ok(action="set_mode", mode=mode, changed=False, note="already in this mode")

    token = str(body.get("token", "")).strip()
    if not token:
        async with PENDING_LOCK:
            pending = _prune_pending(_load_pending())
            if len(pending) >= 3:
                return _fail("too many pending permission requests — wait for the current one to expire")
            code = f"{secrets.randbelow(900000) + 100000}"
            pending[code] = {"mode": mode, "expires": time.time() + 300,
                             "requested_at": datetime.now(timezone.utc).isoformat()}
            _save_pending(pending)
        sent = await telegram_send(
            "🔐 OTHVM Laptop Control\n"
            f"طلب تغيير صلاحية الجهاز من «{current}» إلى «{mode}».\n"
            f"كود التأكيد: {code}\n"
            f"صالح 5 دقائق. لو مش طالب التغيير ده تجاهل الرسالة."
        )
        return _ok(action="set_mode", mode=mode, changed=False, needs_confirmation=True,
                   request_id=code[:3] + "***", expires_in=300,
                   telegram_sent=sent,
                   message="A 6-digit code was sent to the founder on Telegram. Ask him for it, then call set_mode again with token=<code>.")

    async with PENDING_LOCK:
        pending = _prune_pending(_load_pending())
        entry = pending.get(token)
        if not entry:
            return _fail("invalid or expired confirmation code")
        if entry.get("mode") != mode:
            return _fail(f"this code was issued for mode \"{entry.get('mode')}\", not \"{mode}\"")
        del pending[token]
        _save_pending(pending)
    write_mode(mode, by="founder-telegram")
    await telegram_send(f"✅ تم تغيير صلاحية OTHVM Laptop Control إلى «{mode}».")
    return _ok(action="set_mode", mode=mode, changed=True)


# --------------------------------------------------------------------------- emergency stop
def op_emergency_stop(body: dict) -> dict:
    reason = str(body.get("reason", "") or "").strip()
    payload = {
        "active": True,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        EMERGENCY_FLAG.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return _fail(f"could not write emergency flag: {e}")
    return _ok(action="emergency_stop", **payload)


def op_stop_clear(body: dict) -> dict:
    try:
        EMERGENCY_FLAG.unlink(missing_ok=True)
    except Exception as e:
        return _fail(f"could not clear emergency flag: {e}")
    return _ok(action="stop_clear", active=False,
               cleared_at=datetime.now(timezone.utc).isoformat())



# --------------------------------------------------------------------------- knowledge (RAG)
EMBED_MODEL = "gemini-embedding-001"
# The official n8n Agent Knowledge vector store embeds queries at the model default (3072).
EMBED_DIMS = 3072


def _sb_headers() -> dict:
    key = CFG.get("supabase_service_key") or ""
    return {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}


def chunk_text(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    text = re.sub(r"\r\n", "\n", str(text or "")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window_start = start + size // 2
            cut = max(
                text.rfind("\n", window_start, end),
                text.rfind(". ", window_start, end),
                text.rfind(" ", window_start, end),
            )
            if cut > start:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


async def gemini_embed(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    key = str(CFG.get("gemini_api_key") or "").strip()
    if not key:
        raise RuntimeError("gemini_api_key is not configured in mcp_server/config.json")
    model_path = "models/" + EMBED_MODEL
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=120) as client:
        for i in range(0, len(texts), 50):
            batch = [str(t)[:9000] for t in texts[i:i + 50]]
            body = {
                "requests": [
                    {
                        "model": model_path,
                        "content": {"parts": [{"text": t}]},
                        "taskType": task_type,
                        "outputDimensionality": EMBED_DIMS,
                    }
                    for t in batch
                ]
            }
            r = await _gemini_post(
                client,
                f"https://generativelanguage.googleapis.com/v1beta/{model_path}:batchEmbedContents",
                params={"key": key}, json_body=body,
            )
            if r.status_code != 200:
                raise RuntimeError(f"Gemini embedding failed ({r.status_code}): {r.text[:500]}")
            data = r.json()
            for emb in data.get("embeddings", []):
                vectors.append(emb["values"])
    if len(vectors) != len(texts):
        raise RuntimeError(f"Gemini returned {len(vectors)} embeddings for {len(texts)} texts")
    return vectors


async def sb_rpc(fn: str, payload: dict) -> Any:
    url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/rpc/{fn}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, headers=_sb_headers(), json=payload)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase RPC {fn} failed ({r.status_code}): {r.text[:500]}")
    try:
        return r.json()
    except Exception:
        return None


async def sb_insert(rows: list[dict]) -> None:
    url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/knowledge_base?on_conflict=source,source_id"
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(url, headers={**_sb_headers(),
                                      "Prefer": "resolution=merge-duplicates,return=minimal"}, json=rows)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase insert failed ({r.status_code}): {r.text[:600]}")


async def sb_delete(query: str) -> None:
    url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/knowledge_base?{query}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.delete(url, headers=_sb_headers())
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Supabase delete failed ({r.status_code}): {r.text[:500]}")


async def sb_select(query: str) -> list[dict]:
    url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/knowledge_base?{query}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(url, headers=_sb_headers())
    if r.status_code not in (200, 206):
        raise RuntimeError(f"Supabase select failed ({r.status_code}): {r.text[:500]}")
    return r.json()


async def sb_count(query: str = "") -> int:
    url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/knowledge_base?select=id&limit=1"
    if query:
        url += "&" + query
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={**_sb_headers(), "Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"})
    cr = r.headers.get("content-range", "0-0/0")
    try:
        return int(cr.split("/")[-1])
    except Exception:
        return len(r.json()) if isinstance(r.json(), list) else 0


# --------------------------------------------------------------------------- system flags / commands
def _sb_table_url(table: str) -> str:
    return f"{CFG['supabase_url'].rstrip('/')}/rest/v1/{table}"


async def sb_table_get(table: str, query: str = "", timeout: int = 25) -> list[dict]:
    url = _sb_table_url(table)
    if query:
        url += "?" + query
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(url, headers=_sb_headers())
    if r.status_code not in (200, 206):
        raise RuntimeError(f"Supabase {table} select failed ({r.status_code}): {r.text[:400]}")
    data = r.json()
    return data if isinstance(data, list) else [data]


async def sb_table_count(table: str, query: str = "") -> int:
    url = _sb_table_url(table) + "?select=*"
    if query:
        url += "&" + query
    headers = {**_sb_headers(), "Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"}
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get(url, headers=headers)
    cr = r.headers.get("content-range", "0-0/0")
    try:
        return int(cr.split("/")[-1])
    except Exception:
        try:
            data = r.json()
            return len(data) if isinstance(data, list) else 0
        except Exception:
            return 0


# --------------------------------------------------------------------------- ttl cache / retries
_TTL_CACHE: dict[str, tuple[float, Any]] = {}


async def _cached(key: str, ttl: float, fn, *args, **kwargs):
    """Tiny in-process TTL cache for read-only, idempotent calls."""
    now = time.monotonic()
    hit = _TTL_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = await fn(*args, **kwargs)
    _TTL_CACHE[key] = (time.monotonic(), value)
    return value


def _cache_clear(prefix: str = "") -> None:
    for key in [k for k in _TTL_CACHE if not prefix or k.startswith(prefix)]:
        _TTL_CACHE.pop(key, None)


async def sb_count_cached(table: str, query: str = "", ttl: float = 30.0) -> int:
    return await _cached(f"count:{table}:{query}", ttl, sb_table_count, table, query)


GEMINI_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _retry_after_seconds(resp) -> float | None:
    raw = resp.headers.get("retry-after") if resp is not None else None
    if raw:
        try:
            return max(0.0, min(float(raw), 60.0))
        except Exception:
            pass
    try:
        details = resp.json().get("error", {}).get("details") or []
        for item in details:
            delay = str(item.get("retryDelay") or "").strip()
            if delay.endswith("s"):
                return max(0.0, min(float(delay[:-1]), 60.0))
    except Exception:
        pass
    return None


async def _gemini_post(client, url: str, *, params: dict, json_body: dict,
                       attempts: int = 4, base_delay: float = 2.0,
                       max_delay: float = 45.0):
    """POST to Gemini with exponential backoff on 429/5xx (free tier is 5 RPM)."""
    resp = None
    for attempt in range(max(1, attempts)):
        resp = await client.post(url, params=params, json=json_body)
        if resp.status_code == 200 or resp.status_code not in GEMINI_RETRY_STATUS:
            return resp
        if attempt == attempts - 1:
            break
        delay = _retry_after_seconds(resp)
        if delay is None:
            delay = min(max_delay, base_delay * (2 ** attempt))
        log_action("gemini", "retry", {"status": resp.status_code, "attempt": attempt + 1},
                   "retry", f"waiting {delay:.1f}s", int(delay * 1000))
        await asyncio.sleep(delay)
    return resp


async def sb_counts(specs: list[tuple[str, str]], timeout: int = 30) -> dict[str, int]:
    """Run several COUNT(*) subqueries in one Supabase RPC round-trip."""
    parts = []
    for key, subquery in specs:
        safe_key = str(key).replace("'", "''")
        parts.append(f"select '{safe_key}' as k, ({subquery})::bigint as n")
    sql = " union all ".join(parts)
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(_sb_table_url("rpc/exec_sql"), headers=_sb_headers(), json={"query": sql})
    if r.status_code != 200:
        raise RuntimeError(f"Supabase count batch failed ({r.status_code}): {r.text[:300]}")
    rows = (r.json() or {}).get("rows") or []
    return {str(row.get("k")): int(row.get("n") or 0) for row in rows}


async def _safe_counts(key: str, specs: list[tuple[str, str]], ttl: float = 30.0) -> dict:
    try:
        return await _cached(key, ttl, sb_counts, specs)
    except Exception:
        return {}


async def sb_table_upsert(table: str, rows: list[dict], on_conflict: str = "") -> list[dict]:
    url = _sb_table_url(table)
    if on_conflict:
        url += "?on_conflict=" + quote(on_conflict)
    headers = {**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, headers=headers, json=rows)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase {table} upsert failed ({r.status_code}): {r.text[:400]}")
    try:
        data = r.json()
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


async def sb_table_patch(table: str, query: str, payload: dict) -> list[dict]:
    url = _sb_table_url(table) + ("?" + query if query else "")
    headers = {**_sb_headers(), "Prefer": "return=representation"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.patch(url, headers=headers, json=payload)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase {table} patch failed ({r.status_code}): {r.text[:400]}")
    try:
        data = r.json()
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


async def sb_table_delete(table: str, query: str) -> None:
    url = _sb_table_url(table) + ("?" + query if query else "")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.delete(url, headers=_sb_headers())
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Supabase {table} delete failed ({r.status_code}): {r.text[:400]}")


async def get_flag(key: str, default: Any = None) -> Any:
    rows = await sb_table_get("system_flags", f"key=eq.{quote(key)}&select=value")
    return rows[0].get("value") if rows else default


async def set_flag(key: str, value: Any, by: str = "commands") -> None:
    await sb_table_upsert("system_flags", [{
        "key": key,
        "value": value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": by,
    }], on_conflict="key")
    _cache_clear("rows:system_flags")


def _android_config() -> dict:
    path = BASE_DIR.parent / "android_agent" / "config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


async def _android_post(path: str, payload: dict) -> dict:
    cfg = _android_config()
    port = int(cfg.get("server_port") or 8767)
    key = str(cfg.get("api_key") or "")
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(f"http://127.0.0.1:{port}{path}",
                             headers={"X-API-Key": key}, json=payload)
        try:
            return {"status": r.status_code, **r.json()}
        except Exception:
            return {"status": r.status_code, "text": r.text[:800]}
    except Exception as e:
        return {"status": 0, "ok": False, "error": str(e)}


async def _android_health(timeout: float = 6) -> dict:
    cfg = _android_config()
    port = int(cfg.get("server_port") or 8767)
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"http://127.0.0.1:{port}/health")
        data = r.json()
        adb = data.get("adb") or {}
        ts = data.get("tailscale") or {}
        peers = ts.get("android_peers") or []
        return {
            "ok": r.status_code == 200,
            "status": r.status_code,
            "connected": bool(adb.get("connected")),
            "devices": adb.get("devices") or [],
            "stop_flag": bool(data.get("stop_flag")),
            "peer": peers[0] if peers else None,
            "port": port,
        }
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e), "port": port}


async def _android_alarm_create(payload: dict) -> dict:
    """Skip the slow ADB alarm call when the phone is known to be offline."""
    try:
        health = await _cached("android:health", 8.0, _android_health, 2)
    except Exception:
        health = {}
    if not health.get("connected"):
        return {
            "status": 503,
            "ok": False,
            "error": "Android not connected",
            "detail": "No Android device connected via ADB",
        }
    return await _android_post("/device/alarm/create", payload)


# --------------------------------------------------------------------------- telegram commands
# A single, documented registry powers /help, the Telegram menu, and dispatch.
COMMAND_SPECS: list[tuple[str, str, str, str]] = [
    ("help", "core", "/help [قسم|أمر]", "قائمة الأوامر أو تفاصيل قسم/أمر"),
    ("status", "core", "/status", "حالة النظام والذاكرة والموبايل"),
    ("health", "core", "/health", "فحص الخدمات: MCP وn8n وSupabase والموبايل"),
    ("id", "core", "/id", "معرفات المحادثة والوكيل"),
    ("time", "core", "/time", "الوقت الحالي في القاهرة وUTC"),
    ("briefs", "automation", "/briefs", "البريفات المجدولة وحالتها"),
    ("brief", "life", "/brief <الاسم>", "تشغيل بريف الآن"),
    ("briefs_on", "automation", "/briefs_on", "تشغيل البريفات"),
    ("briefs_off", "automation", "/briefs_off", "إيقاف البريفات"),
    ("workflows", "automation", "/workflows [فلتر]", "قائمة الوركفلوهات"),
    ("run", "automation", "/run <الاسم|ID>", "تشغيل وركفلو"),
    ("wf_on", "automation", "/wf_on <الاسم|ID>", "تفعيل وركفلو"),
    ("wf_off", "automation", "/wf_off <الاسم|ID>", "إيقاف وركفلو"),
    ("todo", "life", "/todo <المهمة>", "إضافة مهمة"),
    ("todos", "life", "/todos", "المهام المفتوحة"),
    ("done", "life", "/done <رقم|ID>", "إنجاز مهمة"),
    ("todo_del", "life", "/todo_del <رقم|ID>", "حذف مهمة"),
    ("note", "life", "/note <النص>", "حفظ ملاحظة"),
    ("notes", "life", "/notes [بحث]", "عرض أو بحث الملاحظات"),
    ("note_del", "life", "/note_del <رقم|ID>", "حذف ملاحظة"),
    ("remind", "life", "/remind <وقت> <النص>", "تذكير: +30m أو 18:00 أو tomorrow 09:00"),
    ("reminders", "life", "/reminders", "التذكيرات القادمة"),
    ("remind_del", "life", "/remind_del <رقم|ID>", "إلغاء تذكير"),
    ("expense", "life", "/expense <المبلغ> [التصنيف] [وصف]", "تسجيل مصروف"),
    ("expenses", "life", "/expenses [YYYY-MM]", "ملخص مصروفات الشهر"),
    ("streaks", "growth", "/streaks", "الستريكات النشطة"),
    ("streak_done", "growth", "/streak_done <الاسم>", "تسجيل إنجاز اليوم"),
    ("modes", "growth", "/modes", "الأوضاع المتاحة"),
    ("mode", "growth", "/mode <الاسم>", "تفعيل وضع"),
    ("loops", "growth", "/loops [all]", "اللوبات النشطة أو كل اللوبات"),
    ("wake", "growth", "/wake [ID]", "تأكيد الاستيقاظ وإغلاق لوب المنبه"),
    ("goals", "growth", "/goals", "الأهداف المسجلة"),
    ("memory", "memory", "/memory", "إحصائيات الذاكرة"),
    ("remember", "memory", "/remember <معلومة>", "حفظ معلومة دائمة"),
    ("recall", "memory", "/recall <بحث>", "البحث في الذاكرة"),
    ("knowledge", "memory", "/knowledge <بحث>", "البحث في قاعدة المعرفة"),
    ("weather", "daily", "/weather [مدينة]", "الطقس الآن و3 أيام"),
    ("prayer", "daily", "/prayer", "مواقيت الصلاة والصلاة القادمة"),
    ("alarm", "daily", "/alarm <HH:MM|+دقائق> [وصف]", "ضبط منبه على الموبايل"),
    ("alarms", "daily", "/alarms", "منبهات الموبايل القادمة"),
    ("phone", "device", "/phone", "حالة الموبايل وADB"),
    ("phone_connect", "device", "/phone_connect <host:port>", "اتصال ADB يدوي"),
    ("phone_pair", "device", "/phone_pair <host:port> <code> [connect_port]", "إقران Wireless ADB"),
    ("phone_info", "device", "/phone_info", "بيانات الموبايل"),
    ("phone_battery", "device", "/phone_battery", "بطارية الموبايل"),
    ("phone_notifications", "device", "/phone_notifications", "آخر إشعارات الموبايل"),
    ("sleep", "device", "/sleep", "جلسة النوم الحالية"),
    ("sleep_cancel", "device", "/sleep_cancel", "إلغاء جلسة النوم"),
    ("social", "social", "/social", "آخر المنشورات الاجتماعية"),
    ("stop", "control", "/stop", "إيقاف المهام والبريفات"),
    ("resume", "control", "/resume", "استئناف بعد الإيقاف"),
    ("emergency_stop", "control", "/emergency_stop", "إيقاف طارئ للابتوب والموبايل"),
    ("permission", "control", "/permission [وضع] [كود]", "صلاحية اللابتوب"),
]

# Keep every handler working, but hide admin/niche commands from /help and the menu.
# The visible surface is intentionally small: daily life, worship, habits, memory,
# device status, and safety controls. Workflow administration stays available by
# name for power use, but does not clutter the menu.
COMMAND_HIDDEN = {
    "id", "briefs", "briefs_on", "briefs_off", "workflows", "run", "wf_on", "wf_off",
    "todo_del", "note_del", "remind_del",
    "modes", "mode", "knowledge", "alarms",
    "phone_connect", "phone_pair",
    "phone_info", "phone_battery", "phone_notifications",
    "sleep_cancel", "social",
}

COMMAND_CATEGORIES = {
    "core": "🧭 الأساسيات",
    "life": "✅ اليوم والمهام",
    "daily": "🕌 الصلاة والطقس",
    "growth": "📈 العادات والأهداف",
    "memory": "🧠 الذاكرة",
    "device": "📱 الجهاز والنوم",
    "control": "🛑 التحكم",
    "automation": "⚙️ الأتمتة (متقدمة)",
    "social": "📣 التواصل الاجتماعي",
}

COMMAND_ALIASES = {
    "start": "help",
    "commands": "help",
    "menu": "help",
    "permissions": "permission",
    "emergency_clear": "resume",
    "todo_done": "done",
    "reminder": "remind",
    "reminders_list": "reminders",
    "phone_reconnect": "phone",
    "brief_run": "brief",
    "wf_run": "run",
    "social_posts": "social",
}


def _cmd_spec(name: str) -> tuple[str, str, str, str] | None:
    name = name.strip().lower().lstrip("/")
    name = COMMAND_ALIASES.get(name, name)
    return next((s for s in COMMAND_SPECS if s[0] == name), None)


def _visible_specs() -> list[tuple[str, str, str, str]]:
    return [s for s in COMMAND_SPECS if s[0] not in COMMAND_HIDDEN]


def _cmd_help_text() -> str:
    by_cat: dict[str, list[tuple[str, str, str, str]]] = {}
    visible = _visible_specs()
    for spec in visible:
        by_cat.setdefault(spec[1], []).append(spec)
    lines = [f"🤖 *أوامر OTHVM* — {len(visible)} أمر", ""]
    for cat, title in COMMAND_CATEGORIES.items():
        items = by_cat.get(cat) or []
        if not items:
            continue
        lines.append(f"*{title}*")
        for _, _, usage, desc in items:
            lines.append(f"{usage} — {desc}")
        lines.append("")
    lines.append("للتفاصيل: /help <قسم> أو /help <أمر>. مثال: /help life أو /help الطقس")
    return "\n".join(lines)


def _cmd_category_key(raw: str) -> str:
    """Resolve a category from its key, English word, or Arabic title."""
    s = (raw or "").strip().lower().lstrip("/").replace("_", " ")
    if not s:
        return ""
    if s in COMMAND_CATEGORIES:
        return s
    for key, title in COMMAND_CATEGORIES.items():
        low = title.lower()
        bare = low.split(" ", 1)[1] if " " in low else low
        if s in (low, bare) or s in low:
            return key
    for key in COMMAND_CATEGORIES:
        if len(s) >= 3 and key.startswith(s):
            return key
    return ""


def _cmd_category_text(cat: str) -> str:
    cat = _cmd_category_key(cat)
    if cat not in COMMAND_CATEGORIES:
        return ""
    items = [s for s in _visible_specs() if s[1] == cat]
    if not items:
        return ""
    lines = [f"*{COMMAND_CATEGORIES[cat]}*", ""]
    for _, _, usage, desc in items:
        lines.append(f"{usage} — {desc}")
    lines.append("\nللتفاصيل: /help <أمر>")
    return "\n".join(lines)


def _iso_local(value: Any) -> str:
    if not value:
        return "—"
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CAIRO).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _money(value: Any) -> str:
    try:
        text = f"{float(value):,.2f}"
    except Exception:
        return str(value)
    return text.rstrip("0").rstrip(".")


def _human_until(dt: datetime) -> str:
    seconds = int((dt - datetime.now(CAIRO)).total_seconds())
    if seconds <= 0:
        return "الآن"
    if seconds < 60:
        return "بعد أقل من دقيقة"
    minutes = seconds // 60
    if minutes < 60:
        return f"بعد {minutes} دقيقة"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"بعد {hours} ساعة" + (f" و{mins} دقيقة" if mins else "")
    days, hours = divmod(hours, 24)
    return f"بعد {days} يوم" + (f" و{hours} ساعة" if hours else "")


def _match_row(rows: list[dict], key: str, query: str) -> tuple[dict | None, list[dict]]:
    q = str(query or "").strip().lower()
    if not q:
        return None, []
    exact = next((r for r in rows if str(r.get(key) or "").strip().lower() == q), None)
    if exact:
        return exact, [exact]
    partial = [r for r in rows if q in str(r.get(key) or "").lower()]
    if len(partial) == 1:
        return partial[0], partial
    return None, partial


def _index_row(rows: list[dict], raw: str) -> dict | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        idx = int(s) - 1
        return rows[idx] if 0 <= idx < len(rows) else None
    low = s.lower()
    return next((r for r in rows if str(r.get("id") or "").lower().startswith(low)), None)


async def cmd_help(args: str, body: dict) -> str:
    key = args.strip().lstrip("/").split()[0] if args.strip() else ""
    if key:
        cat_text = _cmd_category_text(key)
        if cat_text:
            return cat_text
        spec = _cmd_spec(key)
        if spec:
            name, cat, usage, desc = spec
            return f"*{usage}*\n{desc}\n\nالقسم: {COMMAND_CATEGORIES.get(cat, cat)}\nالاسم: /{name}"
    return _cmd_help_text()


async def cmd_id(args: str, body: dict) -> str:
    chat_id = str(body.get("chat_id") or CFG.get("telegram_chat_id") or "—")
    return (
        "🆔 *المعرفات*\n\n"
        f"• Chat: `{chat_id}`\n"
        f"• Owner: `{CFG.get('telegram_chat_id') or '—'}`\n"
        f"• Agent: `{CFG.get('agent_id') or 'unknown'}`\n"
        f"• MCP: `127.0.0.1:{CFG.get('service_port', 8765)}`"
    )


async def cmd_time(args: str, body: dict) -> str:
    now = datetime.now(CAIRO)
    return (
        "🕒 *الوقت*\n\n"
        f"• القاهرة: {now.strftime('%A %Y-%m-%d %H:%M:%S')}\n"
        f"• UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"• المنطقة: {CAIRO.key}"
    )


async def _health_n8n() -> tuple[str, bool, str]:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get("http://127.0.0.1:5678/healthz")
        return "n8n", r.status_code < 500, f"HTTP {r.status_code}"
    except Exception as e:
        return "n8n", False, str(e)[:80]


async def _health_supabase() -> tuple[str, bool, str]:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                CFG["supabase_url"].rstrip("/") + "/rest/v1/system_flags?select=key&limit=1",
                headers=_sb_headers(),
            )
        return "Supabase", r.status_code in (200, 206), f"HTTP {r.status_code}"
    except Exception as e:
        return "Supabase", False, str(e)[:80]


async def cmd_health(args: str, body: dict) -> str:
    n8n, sb, android = await asyncio.gather(
        _cached("health:n8n", 8.0, _health_n8n),
        _cached("health:supabase", 8.0, _health_supabase),
        _cached("android:health", 8.0, _android_health),
    )
    checks: list[tuple[str, bool, str]] = [n8n, sb]
    checks.append(("Android agent", bool(android.get("ok")), f"HTTP {android.get('status') or 0}"))
    checks.append(("ADB", bool(android.get("connected")), "متصل" if android.get("connected") else "غير متصل"))
    lines = ["🩺 *فحص الخدمات*", ""]
    for name, ok, detail in checks:
        lines.append(f"{'✅' if ok else '❌'} {name}: {detail}")
    peer = android.get("peer") or {}
    if peer:
        lines.append(f"\n📱 {peer.get('host')} — {'online' if peer.get('online') else 'offline'}")
    return "\n".join(lines)


async def cmd_briefs(args: str, body: dict) -> str:
    rows = await sb_table_get("briefs_registry", "select=*&order=created_at.asc")
    if not rows:
        return "📬 مفيش بريفات مسجلة."
    enabled = await get_flag("briefs_enabled", True)
    lines = [f"📬 *البريفات* — {'مفعلة' if enabled else 'موقوفة'}", ""]
    for r in rows:
        mark = "✅" if r.get("is_active") else "⏸️"
        lines.append(f"{mark} *{r.get('name')}*")
        if r.get("schedule_human"):
            lines.append(f"   🕒 {r.get('schedule_human')}")
        if r.get("last_run"):
            lines.append(f"   آخر تشغيل: {_iso_local(r.get('last_run'))}")
    lines.append("\nتشغيل فوري: /brief <الاسم>")
    return "\n".join(lines)


async def cmd_brief(args: str, body: dict) -> str:
    name = args.strip()
    if not name:
        return "استخدم: /brief <اسم البريف>\nمثال: /brief Morning Briefing"
    rows = await sb_table_get("briefs_registry", "select=name,workflow_id,is_active&order=created_at.asc")
    row, matches = _match_row(rows, "name", name)
    if not row:
        options = "\n".join(f"• {r.get('name')}" for r in (matches or rows)[:10])
        return f"❓ مش لاقي بريف بالاسم ده.\n\n{options}"
    res = await op_workflows({"operation": "briefs.run", "name": row.get("name")})
    if not res.get("ok"):
        return f"❌ تعذر تشغيل {row.get('name')}: {res.get('error') or 'خطأ غير معروف'}"
    return f"▶️ تم تشغيل *{row.get('name')}*.\nExecution: `{res.get('execution_id') or '—'}`"


async def cmd_workflows(args: str, body: dict) -> str:
    res = await op_workflows({"operation": "list", "limit": 250})
    if not res.get("ok"):
        return f"❌ تعذر جلب الوركفلوهات: {res.get('error') or ''}"
    items = res.get("workflows") or []
    flt = args.strip().lower()
    if flt in ("active", "نشط", "مفعل"):
        items = [w for w in items if w.get("active")]
    elif flt in ("inactive", "stopped", "موقوف"):
        items = [w for w in items if not w.get("active")]
    elif flt:
        items = [w for w in items if flt in str(w.get("name") or "").lower()]
    active = sum(1 for w in items if w.get("active"))
    lines = [f"⚙️ *الوركفلوهات* — {len(items)} ({active} نشط)", ""]
    for w in items[:30]:
        mark = "🟢" if w.get("active") else "⚪"
        lines.append(f"{mark} {w.get('name')} — `{w.get('id')}`")
    if len(items) > 30:
        lines.append(f"\n… و{len(items) - 30} غيرهم. استخدم فلتر أدق.")
    lines.append("\nتشغيل: /run <الاسم>")
    return "\n".join(lines)


async def _workflow_row(query: str) -> tuple[dict | None, list[dict]]:
    res = await op_workflows({"operation": "list", "limit": 250})
    items = res.get("workflows") or []
    row, matches = _match_row(items, "name", query)
    if row:
        return row, matches
    by_id = next((w for w in items if str(w.get("id") or "").lower() == query.lower()), None)
    return by_id, matches


async def cmd_run(args: str, body: dict) -> str:
    q = args.strip()
    if not q:
        return "استخدم: /run <اسم أو ID الوركفلو>"
    row, matches = await _workflow_row(q)
    if not row:
        options = "\n".join(f"• {r.get('name')}" for r in matches[:10])
        return f"❓ مش لاقي وركفلو واحد بالاسم ده.\n{options}"
    res = await op_workflows({"operation": "run", "workflow_id": row.get("id")})
    if not res.get("ok"):
        return f"❌ تعذر تشغيل {row.get('name')}: {res.get('error') or ''}"
    return f"▶️ تم تشغيل *{row.get('name')}*.\nExecution: `{res.get('execution_id') or '—'}`"


async def cmd_wf_on(args: str, body: dict) -> str:
    row, matches = await _workflow_row(args.strip())
    if not row:
        return "❓ حدد اسم وركفلو واحد. " + (", ".join(r.get("name", "") for r in matches[:5]) or "استخدم /workflows")
    res = await op_workflows({"operation": "resume", "workflow_id": row.get("id")})
    return f"✅ *{row.get('name')}* اتفعّل." if res.get("ok") else f"❌ {res.get('error') or 'فشل التفعيل'}"


async def cmd_wf_off(args: str, body: dict) -> str:
    row, matches = await _workflow_row(args.strip())
    if not row:
        return "❓ حدد اسم وركفلو واحد. " + (", ".join(r.get("name", "") for r in matches[:5]) or "استخدم /workflows")
    res = await op_workflows({"operation": "pause", "workflow_id": row.get("id")})
    return f"⏸️ *{row.get('name')}* اتوقف." if res.get("ok") else f"❌ {res.get('error') or 'فشل الإيقاف'}"


async def cmd_todo(args: str, body: dict) -> str:
    title = args.strip()
    if not title:
        return "استخدم: /todo <المهمة>\nمثال: /todo أذاكر 30 دقيقة"
    await sb_table_upsert("todos", [{
        "title": title[:500],
        "status": "pending",
        "priority": 2,
        "updated_at": _now_iso(),
    }])
    _cache_clear("rows:todos:")
    return f"✅ اتضافت المهمة: *{title[:200]}*\nلعرضها: /todos"


async def cmd_todos(args: str, body: dict) -> str:
    try:
        rows = await _cached(
            "rows:todos:list", 10.0, sb_table_get,
            "todos", "status=eq.pending&select=*&order=created_at.asc&limit=30",
        )
    except Exception as e:
        return f"⚠️ تعذر جلب المهام: {type(e).__name__}"
    if not rows:
        return "✅ مفيش مهام مفتوحة. تمام!"
    lines = [f"✅ *المهام المفتوحة* — {len(rows)}", ""]
    today = datetime.now(CAIRO).date().isoformat()
    for i, r in enumerate(rows, 1):
        due = str(r.get("due_date") or "")
        late = " ⚠️ متأخرة" if due and due < today else (f" — {due}" if due else "")
        lines.append(f"{i}. {r.get('title') or '(بدون عنوان)'}{late}")
    lines.append("\nلإنجاز مهمة: /done <رقم>")
    return "\n".join(lines)


async def cmd_done(args: str, body: dict) -> str:
    rows = await sb_table_get("todos", "status=eq.pending&select=*&order=created_at.asc&limit=100")
    row = _index_row(rows, args)
    if not row:
        return "❓ رقم أو ID غير موجود. استخدم /todos."
    await sb_table_patch("todos", "id=eq." + quote(str(row.get("id")), safe=""), {
        "status": "done",
        "completed_at": _now_iso(),
        "updated_at": _now_iso(),
    })
    _cache_clear("rows:todos:")
    return f"🎉 تم إنجاز: *{row.get('title')}*"


async def cmd_todo_del(args: str, body: dict) -> str:
    rows = await sb_table_get("todos", "status=eq.pending&select=*&order=created_at.asc&limit=100")
    row = _index_row(rows, args)
    if not row:
        return "❓ رقم أو ID غير موجود. استخدم /todos."
    await sb_table_patch("todos", "id=eq." + quote(str(row.get("id")), safe=""), {
        "status": "deleted",
        "updated_at": _now_iso(),
    })
    _cache_clear("rows:todos:")
    return f"🗑️ اتحذفت: *{row.get('title')}*"


async def cmd_note(args: str, body: dict) -> str:
    text = args.strip()
    if not text:
        return "استخدم: /note <النص>"
    await sb_table_upsert("notes", [{"content": text[:4000], "updated_at": _now_iso()}])
    return "📝 اتسجلت الملاحظة."


async def cmd_notes(args: str, body: dict) -> str:
    q = args.strip()
    if q:
        pattern = quote("*" + q + "*", safe="*")
        rows = await sb_table_get(
            "notes",
            "is_active=eq.true&or=(content.ilike." + pattern + ")&select=*&order=created_at.desc&limit=15",
        )
        title = f"🔎 *نتائج البحث* — {q}"
    else:
        rows = await sb_table_get("notes", "is_active=eq.true&select=*&order=created_at.desc&limit=15")
        title = "📝 *آخر الملاحظات*"
    if not rows:
        return f"{title}\n\nمفيش نتائج."
    lines = [title, ""]
    for i, r in enumerate(rows, 1):
        when = _iso_local(r.get("created_at"))
        lines.append(f"{i}. {str(r.get('content') or '')[:220]}  \n   _{when}_")
    lines.append("\nحذف: /note_del <رقم>")
    return "\n".join(lines)


async def cmd_note_del(args: str, body: dict) -> str:
    rows = await sb_table_get("notes", "is_active=eq.true&select=*&order=created_at.desc&limit=100")
    row = _index_row(rows, args)
    if not row:
        return "❓ رقم أو ID غير موجود. استخدم /notes."
    await sb_table_patch("notes", "id=eq." + quote(str(row.get("id")), safe=""), {
        "is_active": False,
        "updated_at": _now_iso(),
    })
    return "🗑️ اتحذفت الملاحظة."


def _parse_reminder_when(raw: str) -> tuple[datetime, str]:
    s = raw.strip()
    if not s:
        raise ValueError("الوقت مطلوب")
    now = datetime.now(CAIRO)
    # Relative: +30m / +2h / +1d / in 30 minutes
    m = re.match(r"^(?:\+|in\s+)(\d+)\s*(m|min|mins|minute|minutes|h|hr|hour|hours|d|day|days)\b\s*(.*)$", s, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(minutes=n) if unit.startswith("m") else (timedelta(hours=n) if unit.startswith("h") else timedelta(days=n))
        return now + delta, m.group(3).strip()
    # tomorrow HH:MM / today HH:MM
    m = re.match(r"^(today|tomorrow|اليوم|بكرة|غدا|غداً)\s+(\d{1,2}):(\d{2})\s*(.*)$", s, re.I)
    if m:
        day = m.group(1).lower()
        base = now.date() + (timedelta(days=1) if day in ("tomorrow", "بكرة", "غدا", "غداً") else timedelta(0))
        dt = datetime(base.year, base.month, base.day, int(m.group(2)), int(m.group(3)), tzinfo=CAIRO)
        if dt <= now:
            dt += timedelta(days=1)
        return dt, m.group(4).strip()
    # HH:MM (today or next day)
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(.*)$", s)
    if m:
        dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return dt, m.group(3).strip()
    # 9am / 9 pm
    m = re.match(r"^(\d{1,2})\s*(am|pm)\s*(.*)$", s, re.I)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(2).lower() == "pm" else 0)
        dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return dt, m.group(3).strip()
    # ISO
    try:
        iso = s.split(" ", 1)[0]
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CAIRO)
        rest = s[len(iso):].strip()
        return dt.astimezone(CAIRO), rest
    except Exception:
        pass
    # "30m text" as shorthand
    m = re.match(r"^(\d+)\s*(m|h|d)\b\s*(.*)$", s, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(minutes=n) if unit == "m" else (timedelta(hours=n) if unit == "h" else timedelta(days=n))
        return now + delta, m.group(3).strip()
    raise ValueError("مش فاهم الوقت. جرّب: +30m أو 18:00 أو tomorrow 09:00")


async def cmd_remind(args: str, body: dict) -> str:
    try:
        due, text = _parse_reminder_when(args)
    except ValueError as e:
        return f"❌ {e}"
    if not text:
        return "اكتب نص التذكير بعد الوقت. مثال: /remind +30m أذاكر"
    chat_id = str(body.get("chat_id") or CFG.get("telegram_chat_id") or "")
    await sb_table_upsert("reminders", [{
        "text": text[:1000],
        "remind_at": due.astimezone(timezone.utc).isoformat(),
        "status": "pending",
        "chat_id": chat_id,
        "updated_at": _now_iso(),
    }])
    return (
        f"⏰ *التذكير اتضبط*\n\n"
        f"• {text}\n"
        f"• {due.strftime('%Y-%m-%d %H:%M')} (القاهرة) — {_human_until(due)}"
    )


async def cmd_reminders(args: str, body: dict) -> str:
    rows = await sb_table_get(
        "reminders",
        "status=eq.pending&select=*&order=remind_at.asc&limit=30",
    )
    if not rows:
        return "⏰ مفيش تذكيرات قادمة."
    lines = [f"⏰ *التذكيرات القادمة* — {len(rows)}", ""]
    for i, r in enumerate(rows, 1):
        when = _iso_local(r.get("remind_at"))
        lines.append(f"{i}. {r.get('text')}  \n   🕒 {when}")
    lines.append("\nإلغاء: /remind_del <رقم>")
    return "\n".join(lines)


async def cmd_remind_del(args: str, body: dict) -> str:
    rows = await sb_table_get("reminders", "status=eq.pending&select=*&order=remind_at.asc&limit=100")
    row = _index_row(rows, args)
    if not row:
        return "❓ رقم أو ID غير موجود. استخدم /reminders."
    await sb_table_patch("reminders", "id=eq." + quote(str(row.get("id")), safe=""), {
        "status": "cancelled",
        "updated_at": _now_iso(),
    })
    return f"🗑️ اتلغى التذكير: *{row.get('text')}*"


async def cmd_expense(args: str, body: dict) -> str:
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(.*)$", args.strip())
    if not m:
        return "استخدم: /expense <المبلغ> [التصنيف] [وصف]\nمثال: /expense 120 أكل غداء"
    amount = float(m.group(1))
    rest = m.group(2).strip()
    parts = rest.split(maxsplit=1)
    category = parts[0] if parts else "متفرقات"
    description = parts[1] if len(parts) > 1 else ""
    await sb_table_upsert("expenses", [{
        "amount": amount,
        "currency": "EGP",
        "category": category[:80] or "متفرقات",
        "description": description[:500],
    }])
    return f"💸 اتسجل مصروف *{_money(amount)} EGP* — {category}"


async def cmd_expenses(args: str, body: dict) -> str:
    now = datetime.now(CAIRO)
    month = args.strip() or now.strftime("%Y-%m")
    if not re.match(r"^\d{4}-\d{2}$", month):
        return "استخدم: /expenses [YYYY-MM]\nمثال: /expenses 2026-10"
    y, mo = int(month[:4]), int(month[5:7])
    start = f"{y:04d}-{mo:02d}-01"
    end = f"{y + 1:04d}-01-01" if mo == 12 else f"{y:04d}-{mo + 1:02d}-01"
    rows = await sb_table_get(
        "expenses",
        f"spent_on=gte.{start}&spent_on=lt.{end}&select=amount,category,description,spent_on&order=spent_on.desc",
    )
    if not rows:
        return f"💸 مفيش مصروفات مسجلة في {month}."
    total = sum(float(r.get("amount") or 0) for r in rows)
    cats: dict[str, float] = {}
    for r in rows:
        cat = str(r.get("category") or "متفرقات")
        cats[cat] = cats.get(cat, 0) + float(r.get("amount") or 0)
    lines = [f"💸 *مصروفات {month}* — {_money(total)} EGP ({len(rows)} عملية)", ""]
    for cat, val in sorted(cats.items(), key=lambda kv: kv[1], reverse=True)[:10]:
        lines.append(f"• {cat}: {_money(val)} EGP")
    lines.append("\nآخر العمليات:")
    for r in rows[:5]:
        lines.append(f"• {r.get('spent_on')} — {_money(r.get('amount'))} EGP — {r.get('description') or r.get('category')}")
    return "\n".join(lines)


async def cmd_streaks(args: str, body: dict) -> str:
    rows = await sb_table_get(
        "streaks",
        "is_active=eq.true&select=name,current_streak,longest_streak,last_completed,reminder_time&order=name",
    )
    if not rows:
        return "📊 مفيش ستريكات نشطة حاليًا."
    lines = ["📊 *الستريكات النشطة*", ""]
    for r in rows:
        lines.append(
            f"• *{r.get('name')}*: {r.get('current_streak', 0)} يوم "
            f"(أطول: {r.get('longest_streak', 0)}) — آخر إنجاز: {r.get('last_completed') or '—'}"
        )
        if r.get("reminder_time"):
            lines.append(f"  ⏰ التذكير: {r.get('reminder_time')}")
    lines.append("\nتسجيل إنجاز: /streak_done <الاسم>")
    return "\n".join(lines)


async def cmd_streak_done(args: str, body: dict) -> str:
    name = args.strip()
    if not name:
        return "استخدم: /streak_done <اسم الستريك>"
    rows = await sb_table_get("streaks", "is_active=eq.true&select=*&order=name")
    row, matches = _match_row(rows, "name", name)
    if not row:
        return "❓ مش لاقي ستريك واحد. المتاح:\n" + "\n".join(f"• {r.get('name')}" for r in (matches or rows))
    today = datetime.now(CAIRO).date().isoformat()
    if str(row.get("last_completed") or "") == today:
        return f"✅ *{row.get('name')}* متسجل النهاردة بالفعل ({row.get('current_streak')} يوم)."
    current = int(row.get("current_streak") or 0) + 1
    longest = max(current, int(row.get("longest_streak") or 0))
    await sb_table_patch("streaks", "id=eq." + quote(str(row.get("id")), safe=""), {
        "current_streak": current,
        "longest_streak": longest,
        "last_completed": today,
        "updated_at": _now_iso(),
    })
    return f"🔥 *{row.get('name')}* — {current} يوم متتالي!"


async def cmd_modes(args: str, body: dict) -> str:
    rows = await sb_table_get("modes", "is_active=eq.true&select=name,description&order=name")
    if not rows:
        return "🎛️ مفيش أوضاع نشطة."
    lines = ["🎛️ *الأوضاع المتاحة*", ""]
    for r in rows:
        lines.append(f"• *{r.get('name')}* — {r.get('description') or ''}")
    lines.append("\nللتفعيل: /mode <الاسم>")
    return "\n".join(lines)


async def cmd_mode(args: str, body: dict) -> str:
    name = args.strip()
    if not name:
        return await cmd_modes("", body)
    res = await op_workflows({"operation": "modes.activate", "name": name})
    if not res.get("ok"):
        return f"❌ تعذر تفعيل الوضع: {res.get('error') or ''}"
    mode = res.get("mode") or {}
    return f"🎛️ اتفعّل *{mode.get('name') or name}* — {res.get('steps_run', 0)} خطوة."


async def cmd_memory(args: str, body: dict) -> str:
    stats = await op_memory({"operation": "stats"})
    if not stats.get("ok"):
        return f"⚠️ تعذر قراءة الذاكرة: {stats.get('error') or ''}"
    counts_map = await _safe_counts(
        "counts:memory",
        [("knowledge_base", "select count(*) from public.knowledge_base"),
         ("conversation_summaries", "select count(*) from public.conversation_summaries")],
        ttl=60.0,
    )
    by_ns = {str(r.get("namespace")): r for r in stats.get("by_namespace") or []
             if r.get("status") == "active"}
    active = sum(int(r.get("n") or 0) for r in by_ns.values())
    personal = by_ns.get("personal") or {}
    system = by_ns.get("system") or {}
    return (
        "🧠 *ذاكرة OTHVM — موحدة*\n\n"
        f"• النشط: {active} صف (إجمالي {stats.get('total', 0)})\n"
        f"• شخصي: {personal.get('n', 0)} | نظام: {system.get('n', 0)}\n"
        f"• بدون embedding: {stats.get('missing_embeddings', 0)}\n"
        f"• أحداث الذاكرة: {stats.get('events', 0)}\n"
        f"• knowledge_base: {counts_map.get('knowledge_base', '?')} | "
        f"ملخصات المحادثات: {counts_map.get('conversation_summaries', '?')}"
    )


async def cmd_remember(args: str, body: dict) -> str:
    text = args.strip()
    if not text:
        return "استخدم: /remember <معلومة تحب أحفظها>"
    res = await op_memory({
        "operation": "remember",
        "namespace": "personal",
        "category": "personal_context",
        "value": text[:4000],
        "source": "telegram-command",
        "reason": "/remember",
    })
    if not res.get("ok"):
        return f"⚠️ تعذر الحفظ: {res.get('error') or ''}"
    if res.get("changed") is False:
        return "🧠 المعلومة موجودة بالفعل في الذاكرة."
    note = "" if res.get("embedded") else "\n_(اتحفظت بدون embedding — أعد الفهرسة لاحقاً)_"
    return "🧠 حفظت المعلومة في الذاكرة الموحدة." + note


async def cmd_recall(args: str, body: dict) -> str:
    q = args.strip()
    if not q:
        return "استخدم: /recall <كلمة للبحث>"
    res = await op_memory({"operation": "recall", "query": q, "limit": 12, "min_similarity": 0.2})
    if not res.get("ok"):
        return f"⚠️ تعذر البحث: {res.get('error') or ''}"
    rows = res.get("results") or []
    if not rows:
        return f"🔎 مفيش نتائج في الذاكرة عن: {q}"
    lines = [f"🔎 *نتائج الذاكرة* — {q}", ""]
    for r in rows[:12]:
        ns = "شخصي" if r.get("namespace") == "personal" else "نظام"
        lines.append(
            f"• *{r.get('key')}* ({ns}/{r.get('category')}) — "
            f"{str(r.get('value') or '')[:300]}"
        )
    if res.get("fallback"):
        lines.append("\n_(بحث نصي — embedding غير متاح مؤقتاً)_")
    return "\n".join(lines)


async def cmd_knowledge(args: str, body: dict) -> str:
    q = args.strip()
    if not q:
        return "استخدم: /knowledge <كلمة للبحث>"
    pattern = quote("*" + q + "*", safe="*")
    rows = await sb_table_get(
        "knowledge_base",
        "or=(title.ilike." + pattern + ",content.ilike." + pattern + ",source.ilike." + pattern + ")&select=title,source,content,created_at&order=created_at.desc&limit=6",
    )
    if not rows:
        return f"🔎 مفيش نتائج في قاعدة المعرفة عن: {q}"
    lines = [f"📚 *قاعدة المعرفة* — {q}", ""]
    for r in rows:
        lines.append(f"• *{r.get('title') or r.get('source') or 'بدون عنوان'}*")
        lines.append(f"  {str(r.get('content') or '')[:400]}")
    return "\n".join(lines)


WEATHER_CODES = {
    0: "صحو", 1: "صحو غالبًا", 2: "غيوم متفرقة", 3: "غائم",
    45: "شبورة", 48: "شبورة متجمدة", 51: "رشاش خفيف", 53: "رشاش", 55: "رشاش كثيف",
    61: "مطر خفيف", 63: "مطر", 65: "مطر غزير", 66: "مطر متجمد", 67: "مطر متجمد غزير",
    71: "ثلج خفيف", 73: "ثلج", 75: "ثلج غزير", 80: "زخات مطر", 81: "زخات متوسطة",
    82: "زخات قوية", 95: "عاصفة رعدية", 96: "رعد وبرق", 99: "رعد وبرق شديد",
}


async def cmd_weather(args: str, body: dict) -> str:
    city = args.strip() or "Cairo"
    lat, lon, label = 30.0444, 31.2357, "القاهرة"
    if city.lower() not in ("cairo", "القاهرة", "القاهره"):
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": city, "count": 1, "language": "ar", "format": "json"},
                )
            results = (r.json() or {}).get("results") or []
            if not results:
                return f"❓ مش لاقي مدينة اسمها {city}."
            g = results[0]
            lat, lon = float(g["latitude"]), float(g["longitude"])
            label = str(g.get("name") or city)
        except Exception as e:
            return f"❌ تعذر البحث عن المدينة: {str(e)[:120]}"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "Africa/Cairo", "forecast_days": 3,
                },
            )
        data = r.json()
    except Exception as e:
        return f"❌ تعذر جلب الطقس: {str(e)[:120]}"
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    code = int(cur.get("weather_code") or 0)
    lines = [
        f"🌤️ *الطقس — {label}*",
        "",
        f"• الآن: {cur.get('temperature_2m')}° — {WEATHER_CODES.get(code, 'حالة ' + str(code))}",
        f"• الإحساس: {cur.get('apparent_temperature')}° | الرطوبة: {cur.get('relative_humidity_2m')}%",
        f"• الرياح: {cur.get('wind_speed_10m')} كم/س",
    ]
    dates = daily.get("time") or []
    for i, d in enumerate(dates[:3]):
        hi = (daily.get("temperature_2m_max") or [None] * 3)[i]
        lo = (daily.get("temperature_2m_min") or [None] * 3)[i]
        rain = (daily.get("precipitation_probability_max") or [None] * 3)[i]
        lines.append(f"• {d}: {lo}° إلى {hi}° — مطر {rain}%")
    return "\n".join(lines)


async def cmd_prayer(args: str, body: dict) -> str:
    now = datetime.now(CAIRO)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(
                "https://api.aladhan.com/v1/timings/" + now.strftime("%d-%m-%Y"),
                params={"latitude": 30.0444, "longitude": 31.2357, "method": 5},
            )
        data = (r.json() or {}).get("data") or {}
        timings = data.get("timings") or {}
    except Exception as e:
        return f"❌ تعذر جلب مواقيت الصلاة: {str(e)[:120]}"
    names = [("Fajr", "الفجر"), ("Sunrise", "الشروق"), ("Dhuhr", "الظهر"),
             ("Asr", "العصر"), ("Maghrib", "المغرب"), ("Isha", "العشاء")]
    parsed = []
    for key, label in names:
        raw = str(timings.get(key) or "")[:5]
        try:
            dt = datetime.strptime(f"{now.date().isoformat()} {raw}", "%Y-%m-%d %H:%M").replace(tzinfo=CAIRO)
            parsed.append((label, dt, raw))
        except Exception:
            continue
    if not parsed:
        return "❌ مواقيت الصلاة غير متاحة حاليًا."
    upcoming = next(((label, dt) for label, dt, _ in parsed if dt > now), None)
    lines = ["🕌 *مواقيت الصلاة — القاهرة*", ""]
    for label, dt, raw in parsed:
        mark = " ←" if upcoming and upcoming[0] == label else ""
        lines.append(f"• {label}: {raw}{mark}")
    if upcoming:
        lines.append(f"\n⏳ الصلاة القادمة: *{upcoming[0]}* — {_human_until(upcoming[1])}")
    else:
        lines.append("\n⏳ انتهت صلوات اليوم. الفجر القادم غدًا.")
    return "\n".join(lines)


def _parse_alarm_time(raw: str) -> tuple[datetime, str]:
    s = raw.strip()
    if not s:
        raise ValueError("الوقت مطلوب")
    now = datetime.now(CAIRO)
    m = re.match(r"^\+(\d+)\s*(m|min|minutes|h|hour|hours)?\b\s*(.*)$", s, re.I)
    if m:
        n = int(m.group(1))
        unit = (m.group(2) or "m").lower()
        delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
        return now + delta, m.group(3).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(.*)$", s)
    if m:
        dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return dt, m.group(3).strip()
    raise ValueError("استخدم HH:MM أو +دقائق. مثال: /alarm 06:30 صحيان")


async def cmd_alarm(args: str, body: dict) -> str:
    try:
        dt, label = _parse_alarm_time(args)
    except ValueError as e:
        return f"❌ {e}"
    label = label or "OTHVM"
    res = await _android_call("/device/alarm/create", {
        "hour": dt.hour, "minute": dt.minute, "label": label[:80], "skip_ui": True,
    })
    if not res.get("ok"):
        status = int(res.get("status") or 0)
        detail = str(res.get("detail") or res.get("error") or res.get("output") or "").strip()
        if status == 503 or "No Android device" in detail:
            return (
                "📱 *تعذر ضبط المنبه* — الموبايل مش متصل بـADB.\n\n"
                f"• المطلوب: {dt.strftime('%H:%M')} — {label}\n"
                "• فعّل Wireless debugging على الموبايل، وبعدين ابعت:\n"
                "`/phone_pair <IP:pair_port> <code>` ثم `/phone_connect <IP:port>`\n"
                "• أو وصّل USB وفعّل USB debugging."
            )
        return (
            "📱 *تعذر ضبط المنبه* — الموبايل متصل لكن تطبيق الساعة رفض الأمر.\n\n"
            f"• المطلوب: {dt.strftime('%H:%M')} — {label}\n"
            f"• التفاصيل: `{detail[:300] or 'بدون تفاصيل'}`\n"
            "• جرّب `/alarms` للتأكد، أو افتح تطبيق الساعة يدويًا."
        )
    return f"⏰ *المنبه اتضبط* — {dt.strftime('%H:%M')} (القاهرة)\nالوصف: {label}"


async def cmd_alarms(args: str, body: dict) -> str:
    res = await _android_call("/device/alarms/list", {})
    if not res.get("ok"):
        return "📱 الموبايل مش متصل، مش قادر أقرأ المنبهات."
    alarms = res.get("alarms") or []
    if not alarms:
        return "⏰ مفيش منبهات ظاهرة على الموبايل."
    lines = ["⏰ *منبهات الموبايل*", ""]
    for a in alarms[:12]:
        when = _iso_local(a.get("when"))
        tag = a.get("tag") or a.get("package") or ""
        lines.append(f"• {when} — {tag}")
    return "\n".join(lines)


async def cmd_phone(args: str, body: dict) -> str:
    h = await _android_health()
    peer = h.get("peer") or {}
    lines = [
        "📱 *الموبايل*",
        "",
        f"• ADB: {'متصل ✅' if h.get('connected') else 'غير متصل ❌'}",
        f"• الوكيل: {'شغال' if h.get('ok') else 'مش شغال'} (منفذ {h.get('port')})",
    ]
    if peer:
        lines.append(f"• Tailscale: {peer.get('host')} — {'online' if peer.get('online') else 'offline'}")
        if peer.get("ips"):
            lines.append(f"• IP: {peer['ips'][0]}")
    if not h.get("connected"):
        lines.append("\nللاتصال: `/phone_pair <IP:pair_port> <code>` ثم `/phone_connect <IP:port>`")
    return "\n".join(lines)


def _split_host_port(raw: str) -> tuple[str, int]:
    s = raw.strip()
    if ":" not in s:
        raise ValueError("اكتب host:port")
    host, port = s.rsplit(":", 1)
    if not host or not port.isdigit():
        raise ValueError("host:port غير صحيح")
    return host, int(port)


async def cmd_phone_connect(args: str, body: dict) -> str:
    try:
        host, port = _split_host_port(args)
    except ValueError as e:
        return f"❌ {e}"
    res = await _android_post("/device/connect", {"host": host, "port": port})
    if not res.get("ok") and res.get("status") not in (200, 201):
        return f"❌ فشل الاتصال بـ{host}:{port} — {res.get('error') or res.get('detail') or res.get('status')}"
    h = await _android_health()
    return f"📱 تم الاتصال بـ{host}:{port}.\nADB: {'متصل ✅' if h.get('connected') else 'لسه غير متصل — جرّب /phone'}"


async def cmd_phone_pair(args: str, body: dict) -> str:
    parts = args.split()
    if len(parts) < 2:
        return "استخدم: /phone_pair <IP:pair_port> <code> [connect_port]\nمثال: /phone_pair 192.0.2.10:37251 123456 5555"
    try:
        host, pair_port = _split_host_port(parts[0])
    except ValueError as e:
        return f"❌ {e}"
    code = parts[1]
    connect_port = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 5555
    pair = await _android_post("/device/pair", {"host": host, "pair_port": pair_port, "code": code})
    if not pair.get("ok"):
        return f"❌ فشل الإقران — {pair.get('error') or pair.get('detail') or pair.get('status')}\nتأكد إن الكود والمنفذ من شاشة Wireless debugging."
    conn = await _android_post("/device/connect", {"host": host, "port": connect_port})
    h = await _android_health()
    return (
        f"🔗 تم الإقران مع {host}:{pair_port}.\n"
        f"• الاتصال {host}:{connect_port}: {'✅' if conn.get('ok') or conn.get('status') in (200, 201) else '❌'}\n"
        f"• ADB الآن: {'متصل ✅' if h.get('connected') else 'غير متصل'}"
    )


async def cmd_phone_info(args: str, body: dict) -> str:
    res = await _android_call("/device/info", {})
    if not res.get("ok"):
        return "📱 الموبايل مش متصل."
    battery = str(res.get("battery") or "")
    level = re.search(r"level:\s*(\d+)", battery)
    return (
        "📱 *بيانات الموبايل*\n\n"
        f"• {res.get('brand') or ''} {res.get('model') or ''}\n"
        f"• Android {res.get('android') or '—'} (SDK {res.get('sdk') or '—'})\n"
        f"• Serial: `{res.get('serial') or '—'}`\n"
        f"• البطارية: {level.group(1) + '%' if level else '—'}"
    )


async def cmd_phone_battery(args: str, body: dict) -> str:
    res = await _android_call("/device/battery", {})
    if not res.get("ok"):
        return "📱 الموبايل مش متصل."
    b = res.get("battery") or {}
    return (
        "🔋 *بطارية الموبايل*\n\n"
        f"• المستوى: {b.get('level', '—')}%\n"
        f"• الحالة: {b.get('status', '—')}\n"
        f"• الحرارة: {b.get('temperature', '—')}\n"
        f"• الشحن: {b.get('powered', '—')}"
    )


async def cmd_phone_notifications(args: str, body: dict) -> str:
    res = await _android_call("/device/notifications", {})
    if not res.get("ok"):
        return "📱 الموبايل مش متصل."
    items = res.get("notifications") or []
    if not items:
        return "🔔 مفيش إشعارات ظاهرة."
    lines = ["🔔 *آخر الإشعارات*", ""]
    for n in items[:12]:
        title = str(n.get("title") or "").strip()
        text = str(n.get("text") or "").strip()
        pkg = str(n.get("package") or "").split(".")[-1]
        lines.append(f"• *{title or pkg}* — {text[:140] or pkg}")
    return "\n".join(lines)


async def cmd_social(args: str, body: dict) -> str:
    rows = await sb_table_get(
        "social_posts",
        "select=platform,content,status,url,created_at&order=created_at.desc&limit=8",
    )
    if not rows:
        return "📣 مفيش منشورات مسجلة."
    lines = ["📣 *آخر المنشورات*", ""]
    for r in rows[:6]:
        lines.append(f"• {r.get('platform')} — {r.get('status')} — {_iso_local(r.get('created_at'))}")
        lines.append(f"  {str(r.get('content') or '')[:160]}")
    return "\n".join(lines)


async def cmd_sleep(args: str, body: dict) -> str:
    res = await op_sleep_monitor({"action": "status"})
    session = res.get("session") or {}
    if not session:
        return res.get("reply") or "😴 مفيش جلسة نوم نشطة."
    return (
        "😴 *جلسة النوم*\n\n"
        f"• الحالة: {session.get('status')}\n"
        f"• الاستيقاظ: {_iso_local(session.get('wake_at'))}\n"
        f"• التذكيرات: {session.get('reminders', 0)}/3\n"
        f"• التأجيلات: {session.get('snoozes', 0)}"
    )


async def cmd_sleep_cancel(args: str, body: dict) -> str:
    chat_id = str(body.get("chat_id") or CFG.get("telegram_chat_id") or "")
    rows = await sb_table_get(
        "sleep_sessions",
        f"chat_id=eq.{quote(chat_id)}&status=in.(pending,notified,snoozed)&select=id&order=wake_at.asc&limit=1",
    )
    if not rows:
        return "😴 مفيش جلسة نوم نشطة تلغيها."
    await sb_rpc("sleep_action", {"p_session_id": rows[0]["id"], "p_action": "cancel", "p_minutes": 10})
    return "❌ تم إلغاء جلسة النوم."


async def _safe_count(table: str, query: str = "", ttl: float = 30.0):
    try:
        return await sb_count_cached(table, query, ttl=ttl)
    except Exception:
        return "?"


async def _safe_rows(table: str, query: str = "", ttl: float = 0.0):
    try:
        if ttl:
            return await _cached(f"rows:{table}:{query}", ttl, sb_table_get, table, query)
        return await sb_table_get(table, query)
    except Exception:
        return []


_GOOGLE_HEALTH_CACHE: dict[str, Any] = {"at": 0.0, "data": None}


async def _google_health(ttl: float = 120.0) -> dict:
    """Live Google credential health from the n8n Google Health webhook.

    n8n's built-in credential test only validates the OAuth userinfo endpoint, so a
    credential can say "Connection Successful" while every Gmail/Calendar/Drive call
    still fails with "needs to be reconnected". This webhook probes each Google API
    with a real read-only request instead.
    """
    now = time.monotonic()
    cached = _GOOGLE_HEALTH_CACHE.get("data")
    if cached is not None and now - float(_GOOGLE_HEALTH_CACHE.get("at") or 0.0) < ttl:
        return cached
    data: dict
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post("http://127.0.0.1:5678/webhook/othvm-google-health", json={})
        if r.status_code != 200:
            data = {"ok": False, "error": f"HTTP {r.status_code}"}
        else:
            body = r.json()
            data = body if isinstance(body, dict) else {"ok": False, "error": "unreadable response"}
    except Exception as e:
        data = {"ok": False, "error": f"{type(e).__name__}: {e}"[:220]}
    _GOOGLE_HEALTH_CACHE["at"] = now
    _GOOGLE_HEALTH_CACHE["data"] = data
    return data


def _google_status_line(health: dict) -> str:
    services = health.get("services") or {}
    if not services:
        err = str(health.get("error") or "تعذر الفحص")
        return f"• جوجل: ⚠️ {err[:160]}"
    labels = {
        "gmail": "Gmail",
        "calendar": "Calendar",
        "drive": "Drive",
        "tasks": "Tasks",
        "contacts": "Contacts",
    }
    parts = []
    for key, label in labels.items():
        item = services.get(key) or {}
        parts.append(f"{label} {'✅' if item.get('ok') else '❌'}")
    line = "• جوجل: " + " | ".join(parts)
    failed = health.get("failed") or [k for k, v in services.items() if not (v or {}).get("ok")]
    if failed:
        names = "، ".join(labels.get(k, k) for k in failed)
        line += f"\n  ↳ محتاج Reconnect في n8n: {names}"
    return line


async def command_status() -> str:
    status_specs = [
        ("othvm_memory", "select count(*) from public.othvm_memory"),
        ("othman_memory", "select count(*) from public.othman_memory"),
        ("knowledge_base", "select count(*) from public.knowledge_base"),
        ("briefs_registry", "select count(*) from public.briefs_registry"),
        ("active_streaks", "select count(*) from public.streaks where is_active = true"),
        ("active_modes", "select count(*) from public.modes where is_active = true"),
        ("open_todos", "select count(*) from public.todos where status = 'pending'"),
        ("pending_reminders", "select count(*) from public.reminders where status = 'pending'"),
    ]
    flag_rows, android, counts_map, asleep, google = await asyncio.gather(
        _cached("rows:system_flags", 15.0, sb_table_get, "system_flags", "select=key,value"),
        _cached("android:health", 8.0, _android_health),
        _safe_counts("counts:status", status_specs, ttl=30.0),
        _safe_rows("sleep_sessions",
                   "status=in.(pending,notified,snoozed)&select=id,wake_at,status&order=wake_at.asc&limit=1",
                   ttl=15.0),
        _google_health(),
    )
    flags = {r["key"]: r.get("value") for r in flag_rows}
    stop_all = bool(flags.get("stop_all"))
    emergency = bool(flags.get("emergency_stop")) or EMERGENCY_FLAG.exists()
    briefs = bool(flags.get("briefs_enabled", True))
    counts = {k: counts_map.get(k, "?") for k in
              ("othvm_memory", "othman_memory", "knowledge_base", "briefs_registry")}
    active_streaks = counts_map.get("active_streaks", "?")
    active_modes = counts_map.get("active_modes", "?")
    open_todos = counts_map.get("open_todos", "?")
    pending_reminders = counts_map.get("pending_reminders", "?")
    sleep_line = "لا توجد جلسة نوم معلقة"
    if asleep:
        s = asleep[0]
        sleep_line = f"جلسة {s.get('status')} — الاستيقاظ {_iso_local(s.get('wake_at'))}"
    be = "موقوفة" if not briefs else "مفعلة"
    state = "🛑 إيقاف طارئ" if emergency else ("⏸️ إيقاف مؤقت" if stop_all else "🟢 يعمل")
    peer = android.get("peer") or {}
    phone = "متصل" if android.get("connected") else "غير متصل"
    if peer.get("host"):
        phone += f" ({peer.get('host')})"
    return (
        f"📊 *OTHVM Status*\n\n"
        f"• الحالة: {state}\n"
        f"• البريفات: {be}\n"
        f"• اللابتوب: {read_mode()}\n"
        f"• الموبايل: {phone}\n"
        f"• الستريكات النشطة: {active_streaks}\n"
        f"• الأوضاع النشطة: {active_modes}\n"
        f"• المهام المفتوحة: {open_todos}\n"
        f"• التذكيرات القادمة: {pending_reminders}\n"
        f"• النوم: {sleep_line}\n"
        f"{_google_status_line(google)}\n\n"
        f"🧠 الذاكرة: othvm {counts.get('othvm_memory')} | personal {counts.get('othman_memory')} | Knowledge {counts.get('knowledge_base')} | Briefs {counts.get('briefs_registry')}\n"
        f"🕒 آخر إيقاف: {_iso_local(flags.get('last_stop_at'))}\n"
        f"🕒 آخر استئناف: {_iso_local(flags.get('last_resume_at'))}"
    )


async def command_permission(args: str) -> str:
    parts = [p for p in args.split() if p]
    if not parts or parts[0].lower() in ("status", "حالة"):
        pending = len(_prune_pending(_load_pending()))
        return (
            f"🔐 *صلاحية اللابتوب*: `{read_mode()}`\n"
            f"• الدور: {ROLE}\n"
            f"• طلبات معلقة: {pending}\n\n"
            f"للتغيير: `/permission full` ثم ابعت الكود: `/permission full 123456`\n"
            f"المستويات: full | write | read | none"
        )
    mode = parts[0].lower()
    token = parts[1] if len(parts) > 1 else ""
    res = await op_permission({"action": "set_mode", "mode": mode, "token": token})
    if not res.get("ok"):
        return "❌ " + str(res.get("error") or "فشل تغيير الصلاحية")
    if res.get("changed"):
        return f"✅ تم تغيير صلاحية اللابتوب إلى `{mode}`."
    if res.get("needs_confirmation"):
        return (
            f"🔐 بعت كود تأكيد على تيليجرام لتغيير الصلاحية إلى `{mode}`.\n"
            f"الكود صالح 5 دقائق. ابعت: `/permission {mode} <code>`"
        )
    return str(res.get("message") or "تم.")


async def command_stop(reason: str = "/stop") -> str:
    now = datetime.now(timezone.utc).isoformat()
    await set_flag("stop_all", True, by="telegram-command")
    await set_flag("briefs_enabled", False, by="telegram-command")
    await set_flag("last_stop_at", now, by="telegram-command")
    await _android_post("/emergency_stop", {"active": True, "reason": reason})
    return (
        "🛑 *تم تفعيل وضع الإيقاف*\n\n"
        "• البريفات والمهام التلقائية: موقوفة\n"
        "• التذكيرات: موقوفة\n"
        "• أوامر الموبايل: موقوفة\n"
        "• اللابتوب: لسه شغال (استخدم /emergency_stop لإيقافه كمان)\n\n"
        "للاستئناف: /resume"
    )


async def command_emergency_stop() -> str:
    now = datetime.now(timezone.utc).isoformat()
    laptop = op_emergency_stop({"reason": "Telegram /emergency_stop"})
    android = await _android_post("/emergency_stop", {"active": True, "reason": "Telegram /emergency_stop"})
    await set_flag("emergency_stop", True, by="telegram-command")
    await set_flag("stop_all", True, by="telegram-command")
    await set_flag("briefs_enabled", False, by="telegram-command")
    await set_flag("last_stop_at", now, by="telegram-command")
    return (
        "🚨 *إيقاف طارئ مفعّل*\n\n"
        f"• اللابتوب MCP: {'✅' if laptop.get('ok') else '❌'} {laptop.get('error') or 'flag set'}\n"
        f"• الأندرويد: {'✅' if android.get('ok') else '❌'} {android.get('error') or ''}\n"
        "• البريفات والتذكيرات: موقوفة\n\n"
        "للاستئناف: /resume"
    )


async def command_resume() -> str:
    now = datetime.now(timezone.utc).isoformat()
    laptop = op_stop_clear({})
    android = await _android_post("/stop/clear", {})
    await set_flag("emergency_stop", False, by="telegram-command")
    await set_flag("stop_all", False, by="telegram-command")
    await set_flag("briefs_enabled", True, by="telegram-command")
    await set_flag("last_resume_at", now, by="telegram-command")
    return (
        "🟢 *تم الاستئناف*\n\n"
        f"• اللابتوب MCP: {'✅' if laptop.get('ok') else '❌'}\n"
        f"• الأندرويد: {'✅' if android.get('ok') else '❌'}\n"
        "• البريفات والتذكيرات: مفعلة"
    )


async def _android_call(path: str, payload: dict) -> dict:
    """Call the Android agent and retry once after a forced reconnect."""
    res = await _android_post(path, payload)
    if res.get("ok") or 200 <= int(res.get("status") or 0) < 300:
        return res
    await _android_post("/device/reconnect", {"force": True})
    return await _android_post(path, payload)


async def cmd_loops(args: str, body: dict) -> str:
    want_all = args.strip().lower() in ("all", "الكل", "كل")
    res = await op_loops({"action": "list", "all": want_all})
    if not res.get("ok"):
        return f"⚠️ {res.get('error')}"
    rows = res.get("loops") or []
    if not rows:
        return "🔁 مفيش لوبات نشطة.\n\nتقدر تعمل لوب من الشات: «فكرني كل 30 دقيقة أشرب مية»."
    lines = [f"🔁 *{'كل اللوبات' if want_all else 'اللوبات النشطة'}* — {len(rows)}", ""]
    icons = {"wake": "⏰", "alarm": "⏰", "commitment": "🎯", "reminder": "📌"}
    for i, r in enumerate(rows, 1):
        icon = icons.get(str(r.get("kind") or ""), "🔁")
        nxt = _iso_local(r.get("next_run_at"))
        lines.append(
            f"{i}. {icon} *{r.get('title')}*  \n"
            f"   {r.get('status')} — {r.get('runs')}/{r.get('max_runs')} — كل {r.get('cadence_minutes')} دقيقة  \n"
            f"   🕒 {nxt}"
        )
    lines.append("\nتأكيد منبه: /wake — إيقاف لوب: من الشات.")
    return "\n".join(lines)


async def cmd_wake(args: str, body: dict) -> str:
    payload = {"action": "ack", "kind": "wake"}
    lid = args.strip()
    if re.fullmatch(r"[0-9a-fA-F-]{36}", lid):
        payload["loop_id"] = lid
    res = await op_loops(payload)
    if not res.get("ok"):
        return f"⚠️ {res.get('error')}"
    loop = res.get("loop") or {}
    return f"☀️ صباح الخير. اتأكد الاستيقاظ واتقفل «{loop.get('title')}»."


async def cmd_goals(args: str, body: dict) -> str:
    res = await op_loops({"action": "goal_list"})
    if not res.get("ok"):
        return f"⚠️ {res.get('error')}"
    rows = res.get("goals") or []
    if not rows:
        return "🎯 مفيش أهداف مسجلة.\n\nقول: «هدفي أخلص المشروع قبل 30 نوفمبر»."
    lines = [f"🎯 *الأهداف* — {len(rows)}", ""]
    for i, g in enumerate(rows, 1):
        lines.append(
            f"{i}. *{g.get('goal_description')}*  \n"
            f"   {g.get('status') or 'active'}"
            + (f" — deadline: {g.get('deadline')}" if g.get("deadline") else "")
            + (f"  \n   آخر تقدم: {g.get('last_progress')}" if g.get("last_progress") else "")
            + (f"  \n   الخطوة الجاية: {g.get('next_step')}" if g.get("next_step") else "")
        )
    return "\n".join(lines)


COMMAND_HANDLERS = {
    "help": cmd_help,
    "status": lambda a, b: command_status(),
    "health": cmd_health,
    "id": cmd_id,
    "time": cmd_time,
    "briefs": cmd_briefs,
    "brief": cmd_brief,
    "briefs_on": lambda a, b: _briefs_toggle(True),
    "briefs_off": lambda a, b: _briefs_toggle(False),
    "workflows": cmd_workflows,
    "run": cmd_run,
    "wf_on": cmd_wf_on,
    "wf_off": cmd_wf_off,
    "todo": cmd_todo,
    "todos": cmd_todos,
    "done": cmd_done,
    "todo_del": cmd_todo_del,
    "note": cmd_note,
    "notes": cmd_notes,
    "note_del": cmd_note_del,
    "remind": cmd_remind,
    "reminders": cmd_reminders,
    "remind_del": cmd_remind_del,
    "expense": cmd_expense,
    "expenses": cmd_expenses,
    "streaks": cmd_streaks,
    "streak_done": cmd_streak_done,
    "modes": cmd_modes,
    "mode": cmd_mode,
    "loops": cmd_loops,
    "wake": cmd_wake,
    "goals": cmd_goals,
    "memory": cmd_memory,
    "remember": cmd_remember,
    "recall": cmd_recall,
    "knowledge": cmd_knowledge,
    "weather": cmd_weather,
    "prayer": cmd_prayer,
    "alarm": cmd_alarm,
    "alarms": cmd_alarms,
    "phone": cmd_phone,
    "phone_connect": cmd_phone_connect,
    "phone_pair": cmd_phone_pair,
    "phone_info": cmd_phone_info,
    "phone_battery": cmd_phone_battery,
    "phone_notifications": cmd_phone_notifications,
    "sleep": cmd_sleep,
    "sleep_cancel": cmd_sleep_cancel,
    "social": cmd_social,
    "stop": lambda a, b: command_stop("/stop"),
    "resume": lambda a, b: command_resume(),
    "emergency_stop": lambda a, b: command_emergency_stop(),
    "permission": lambda a, b: command_permission(a),
}


async def _briefs_toggle(enabled: bool) -> str:
    await set_flag("briefs_enabled", enabled, by="telegram-command")
    return "📬 تم تشغيل البريفات." if enabled else "📴 تم إيقاف البريفات. للتفعيل: /briefs_on"


async def op_commands(body: dict) -> dict:
    raw = str(body.get("command") or "").strip()
    command = raw.lower().split()[0] if raw else ""
    if command.startswith("/"):
        command = command[1:]
    command = command.split("@")[0]
    command = COMMAND_ALIASES.get(command, command)
    args = str(body.get("args") or "").strip()
    if not command or command in ("start", "menu"):
        command = "help"
    try:
        handler = COMMAND_HANDLERS.get(command)
        if not handler:
            close = difflib.get_close_matches(command, list(COMMAND_HANDLERS), n=3, cutoff=0.55)
            hint = ("\nقصدت: " + " / ".join("/" + c for c in close)) if close else ""
            reply = f"❓ أمر غير معروف: `{raw}`{hint}\n\n{_cmd_help_text()}"
        else:
            reply = await handler(args, body)
        reply = str(reply or "")
        if len(reply) > 3900:
            reply = reply[:3890] + "\n… (تم الاختصار)"
        return _ok(action="commands", command="/" + command, args=args, reply=reply, handled=True)
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}", command="/" + command)


async def op_reminders(body: dict) -> dict:
    action = str(body.get("action") or "due").lower().strip()
    try:
        if action in ("due", "tick"):
            if await get_flag("stop_all", False) or await get_flag("emergency_stop", False):
                return _ok(action="reminders.due", due=0, sent=0, skipped="stop_all")
            now = datetime.now(timezone.utc).isoformat()
            rows = await sb_table_get(
                "reminders",
                "status=eq.pending&remind_at=lte." + quote(now) + "&select=*&order=remind_at.asc&limit=10",
            )
            sent = 0
            for r in rows:
                text = (
                    "⏰ *تذكير OTHVM*\n\n"
                    f"{r.get('text')}\n\n"
                    f"🕒 {_iso_local(r.get('remind_at'))}"
                )
                ok = await telegram_send_payload({"text": text}, str(r.get("chat_id") or "") or None)
                if ok:
                    sent += 1
                    await sb_table_patch("reminders", "id=eq." + quote(str(r.get("id")), safe=""), {
                        "status": "sent",
                        "sent_at": now,
                        "updated_at": now,
                    })
            return _ok(action="reminders.due", due=len(rows), sent=sent, handled=True)
        if action in ("list", "pending"):
            rows = await sb_table_get(
                "reminders",
                "status=eq.pending&select=*&order=remind_at.asc&limit=50",
            )
            return _ok(action="reminders.list", count=len(rows), reminders=rows, handled=True)
        return _fail(f"unknown reminders action '{action}'", action=action)
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}", action=action)


# --------------------------------------------------------------------------- sleep monitor
def _parse_sleep_time(body: dict) -> datetime:
    raw = body.get("wake_at")
    if raw:
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except Exception as e:
            raise ValueError(f"invalid wake_at: {e}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CAIRO)
        dt = dt.astimezone(CAIRO)
        if dt <= datetime.now(CAIRO):
            raise ValueError("wake_at is in the past")
        return dt
    time_s = str(body.get("time") or "").strip()
    hour = body.get("hour")
    minute = body.get("minute")
    if time_s:
        parts = time_s.split(":")
        if len(parts) < 2:
            raise ValueError("time must be HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
    elif hour is not None:
        hour, minute = int(hour), int(minute or 0)
    else:
        raise ValueError("wake_at or time (HH:MM) is required")
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise ValueError("hour/minute out of range")
    now = datetime.now(CAIRO)
    dt = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if dt <= now:
        dt += timedelta(days=1)
    return dt


async def _sleep_start(body: dict) -> dict:
    dt = _parse_sleep_time(body)
    label = str(body.get("label") or "Sleep")[:80]
    chat_id = str(body.get("chat_id") or CFG.get("telegram_chat_id") or "")
    wake_iso = dt.isoformat()
    rows = await sb_table_upsert("sleep_sessions", [{
        "chat_id": chat_id,
        "label": label,
        "start_at": datetime.now(timezone.utc).isoformat(),
        "wake_at": wake_iso,
        "wake_check_at": wake_iso,
        "status": "pending",
    }])
    session = rows[0] if rows else {}
    alarm = await _android_post("/device/alarm/create", {
        "hour": dt.hour,
        "minute": dt.minute,
        "label": label,
        "skip_ui": True,
    })
    alarm_ok = bool(alarm.get("ok"))
    reply = (
        f"😴 *تم تسجيل النوم*\n\n"
        f"• الوقت: {dt.strftime('%Y-%m-%d %H:%M')} (القاهرة)\n"
        f"• المنبه على الموبايل: {'✅' if alarm_ok else '⚠️ مش متاح'}\n"
        f"• سأرسل تذكير استيقاظ في الوقت المحدد.\n\n"
        f"لو الموبايل مش متصل، المنبه مش هيتضبط، لكن التذكير على تيليجرام شغال."
    )
    return _ok(action="sleep.start", session=session, wake_at=wake_iso,
               cairo_time=dt.strftime("%Y-%m-%d %H:%M"), alarm=alarm,
               alarm_ok=alarm_ok, reply=reply, handled=True)


async def _sleep_status(body: dict) -> dict:
    chat_id = str(body.get("chat_id") or CFG.get("telegram_chat_id") or "")
    rows = await sb_table_get(
        "sleep_sessions",
        f"chat_id=eq.{quote(chat_id)}&status=in.(pending,notified,snoozed)&select=*&order=wake_at.asc&limit=1",
    )
    if not rows:
        reply = "😴 لا توجد جلسة نوم نشطة حاليًا.\nللبدء: قول «نيمني الساعة 11»."
        return _ok(action="sleep.status", session=None, reply=reply, handled=True)
    s = rows[0]
    reply = (
        f"😴 *جلسة نوم*\n\n"
        f"• الحالة: {s.get('status')}\n"
        f"• الاستيقاظ: {s.get('wake_at')}\n"
        f"• التذكيرات: {s.get('reminders', 0)}/3\n"
        f"• التأجيلات: {s.get('snoozes', 0)}\n\n"
        f"لو صحيت: /sleep_woke أو من رسالة التذكير."
    )
    return _ok(action="sleep.status", session=s, reply=reply, handled=True)


async def _sleep_list(body: dict) -> dict:
    rows = await sb_table_get(
        "sleep_sessions",
        "select=id,label,wake_at,status,reminders,snoozes&order=created_at.desc&limit=10",
    )
    return _ok(action="sleep.list", sessions=rows, count=len(rows),
               reply="\n".join(
                   f"• {r.get('wake_at')} — {r.get('status')} — {r.get('label')}"
                   for r in rows
               ) or "لا توجد جلسات.")


async def _sleep_action(body: dict) -> dict:
    sid = str(body.get("session_id") or body.get("id") or "").strip()
    action = str(body.get("action") or "").lower().strip()
    if not sid:
        return _fail("session_id is required")
    if action not in ("woke", "still", "snooze", "cancel"):
        return _fail("action must be woke | still | snooze | cancel")
    minutes = max(1, min(120, int(body.get("minutes") or 10)))
    row = await sb_rpc("sleep_action", {
        "p_session_id": sid,
        "p_action": action,
        "p_minutes": minutes,
    })
    if isinstance(row, list):
        row = row[0] if row else None
    if not row or not row.get("id"):
        return _fail("sleep session not found")
    if action == "woke":
        reply = "☀️ *صباح الخير!* تم تسجيل إنك صحيت. يوم سعيد."
    elif action == "still":
        reply = "😴 تمام، هسألك تاني بعد 5 دقائق."
    elif action == "snooze":
        reply = f"⏰ تمام، هصحيك بعد {minutes} دقيقة."
    else:
        reply = "❌ تم إلغاء جلسة النوم."
    await telegram_send(reply)
    return _ok(action=f"sleep.{action}", session=row, reply=reply, handled=True)


async def _sleep_due(body: dict) -> dict:
    if await get_flag("stop_all", False) or await get_flag("emergency_stop", False):
        return _ok(action="sleep.due", due=0, sent=0, skipped="stop_all")
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    next_iso = (now_dt + timedelta(minutes=10)).isoformat()
    rows = await sb_table_get(
        "sleep_sessions",
        f"status=in.(pending,notified,snoozed)&wake_check_at=lte.{quote(now_iso)}"
        f"&reminders=lt.3&select=*&order=wake_check_at.asc&limit=5",
    )
    sent = 0
    for s in rows:
        sid = str(s.get("id"))
        label = str(s.get("label") or "Sleep")
        text = (
            f"⏰ *وقت الاستيقاظ* — {label}\n\n"
            f"إيه الوضع؟ اختار من الأزرار تحت:\n"
            f"• لو صحيت: ✅\n"
            f"• لو لسه نايم: 😴 هسألك بعد 5 دقائق\n"
            f"• لو عايز تأجل: ⏰ 10 دقائق\n"
            f"• لو عايز تلغي: ❌"
        )
        base = PUBLIC_BASE_URL + "/webhook/sleep-monitor"
        sid_q = quote(sid)
        keyboard = {
            "inline_keyboard": [
                [{"text": "✅ صحيت", "url": f"{base}?action=woke&session_id={sid_q}"}],
                [
                    {"text": "😴 لسه نايم 5 د", "url": f"{base}?action=still&session_id={sid_q}"},
                    {"text": "⏰ تأجيل 10 د", "url": f"{base}?action=snooze&session_id={sid_q}&minutes=10"},
                ],
                [{"text": "❌ إلغاء", "url": f"{base}?action=cancel&session_id={sid_q}"}],
            ]
        }
        ok = await telegram_send_payload({"text": text, "reply_markup": keyboard},
                                          chat_id=str(s.get("chat_id") or "") or None)
        if ok:
            sent += 1
            await sb_table_patch("sleep_sessions", f"id=eq.{sid_q}", {
                "status": "notified",
                "reminders": int(s.get("reminders") or 0) + 1,
                "last_ping_at": now_iso,
                "wake_check_at": next_iso,
                "updated_at": now_iso,
            })
    return _ok(action="sleep.due", due=len(rows), sent=sent, handled=True)


async def _sleep_alarm_test(body: dict) -> dict:
    dt = datetime.now(CAIRO) + timedelta(minutes=1)
    alarm = await _android_post("/device/alarm/create", {
        "hour": dt.hour,
        "minute": dt.minute,
        "label": str(body.get("label") or "OTHVM test")[:80],
        "skip_ui": True,
    })
    return _ok(action="sleep.alarm_test", at=dt.strftime("%H:%M"),
               alarm=alarm, alarm_ok=bool(alarm.get("ok")), handled=True)


async def op_sleep_monitor(body: dict) -> dict:
    action = str(body.get("action") or "status").lower().strip()
    try:
        if action in ("start", "schedule"):
            return await _sleep_start(body)
        if action == "status":
            return await _sleep_status(body)
        if action == "list":
            return await _sleep_list(body)
        if action in ("woke", "still", "snooze", "cancel"):
            return await _sleep_action(body)
        if action in ("due", "tick"):
            return await _sleep_due(body)
        if action in ("alarm_test", "test_alarm"):
            return await _sleep_alarm_test(body)
        return _fail(f"unknown sleep action '{action}'")
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}", action=action)


# --------------------------------------------------------------------------- goals + loops engine
def _loop_when(body: dict) -> datetime:
    raw = body.get("first_run_at") or body.get("next_run_at") or body.get("wake_at")
    if raw:
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CAIRO)
        return dt.astimezone(CAIRO)
    time_s = str(body.get("time") or "").strip()
    if time_s:
        parts = time_s.split(":")
        if len(parts) < 2:
            raise ValueError("time must be HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("hour/minute out of range")
        now = datetime.now(CAIRO)
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return dt if dt > now else dt + timedelta(days=1)
    minutes = int(body.get("start_in_minutes") or 0)
    return datetime.now(CAIRO) + timedelta(minutes=max(0, minutes))


def _loop_id(body: dict) -> str:
    lid = str(body.get("loop_id") or body.get("id") or "").strip()
    return lid if re.fullmatch(r"[0-9a-fA-F-]{36}", lid) else ""


def _loop_parse_dt(raw: Any) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CAIRO)
        return dt.astimezone(CAIRO)
    except Exception:
        return None


async def _loop_find_near(first: datetime, kind: str, chat_id: str,
                          window_minutes: int = 30) -> dict | None:
    """Return an active wake/alarm/reminder loop within the dedupe window."""
    if kind not in ("wake", "alarm", "reminder"):
        return None
    rows = await sb_table_get(
        "loops",
        "status=eq.active&kind=in.(wake,alarm,reminder)&select=*&order=next_run_at.asc&limit=50",
    )
    window = max(1, min(int(window_minutes or 30), 120)) * 60
    for row in rows:
        row_chat = str(row.get("chat_id") or "")
        if chat_id and row_chat and row_chat != chat_id:
            continue
        dt = _loop_parse_dt(row.get("next_run_at"))
        if dt:
            delta = abs((
                dt.replace(second=0, microsecond=0)
                - first.replace(second=0, microsecond=0)
            ).total_seconds())
            if delta <= window:
                return row
    return None


def _loop_when_human(row: dict) -> str:
    dt = _loop_parse_dt(row.get("next_run_at"))
    return dt.strftime("%Y-%m-%d %H:%M") if dt else str(row.get("next_run_at") or "?")


def _ar_count(n: int, one: str, two: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if n == 2:
        return two
    if 3 <= n <= 10:
        return f"{n} {few}"
    return f"{n} {many}"


def _loop_relative_ar(dt: datetime) -> str:
    minutes = int(round((dt - datetime.now(CAIRO)).total_seconds() / 60))
    if minutes <= 0:
        return "دلوقتي"
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(_ar_count(hours, "ساعة واحدة", "ساعتين", "ساعات", "ساعة"))
    if mins:
        parts.append(_ar_count(mins, "دقيقة واحدة", "دقيقتين", "دقائق", "دقيقة"))
    return "بعد " + " و".join(parts)


def _loop_reply(row: dict, created: bool) -> str:
    dt = _loop_parse_dt(row.get("next_run_at"))
    if not dt:
        return "✅ تم ضبط المنبه."
    when = dt.strftime("%Y-%m-%d %H:%M")
    rel = _loop_relative_ar(dt)
    if created:
        return f"✅ المنبه مضبوط على {when} (القاهرة) — {rel}."
    return f"✅ في منبه شغال بالفعل على {when} (القاهرة) — {rel}، مفيش تكرار."


def _loop_meta(row: dict) -> dict:
    dt = _loop_parse_dt(row.get("next_run_at"))
    if not dt:
        return {}
    return {
        "when_human": dt.strftime("%Y-%m-%d %H:%M"),
        "relative_ar": _loop_relative_ar(dt),
        "minutes_from_now": int(round((dt - datetime.now(CAIRO)).total_seconds() / 60)),
    }


def _loop_reschedule_reply(row: dict) -> str:
    dt = _loop_parse_dt(row.get("next_run_at"))
    if not dt:
        return "✅ تم تحديث المنبه."
    return (f"✅ تم تحديث المنبه إلى {dt.strftime('%Y-%m-%d %H:%M')} "
            f"(القاهرة) — {_loop_relative_ar(dt)}.")


async def _loop_active_alarms(chat_id: str) -> list[dict]:
    rows = await sb_table_get(
        "loops",
        "status=eq.active&kind=in.(wake,alarm)&select=*&order=next_run_at.asc&limit=50",
    )
    return [
        row for row in rows
        if not chat_id or not row.get("chat_id") or str(row.get("chat_id")) == chat_id
    ]


async def _loop_cancel_others(rows: list[dict], keep_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        if str(row.get("id")) == str(keep_id):
            continue
        try:
            await sb_table_patch(
                "loops", "id=eq." + quote(str(row["id"])),
                {"status": "cancelled", "updated_at": now},
            )
        except Exception:
            pass


async def _loop_create(body: dict) -> dict:
    title = str(body.get("title") or body.get("name") or "").strip()
    if not title:
        return _fail("title is required")
    prompt = str(body.get("prompt") or body.get("question") or body.get("message") or title).strip()
    kind = str(body.get("kind") or body.get("type") or "checkin").lower().strip()
    if kind not in ("checkin", "wake", "alarm", "commitment", "reminder", "custom"):
        kind = "checkin"
    cadence = max(1, min(int(body.get("cadence_minutes") or body.get("every_minutes") or 30), 10080))
    max_runs = max(1, min(int(body.get("max_runs") or body.get("repeats") or 10), 1000))
    ack_required = _truthy(body.get("ack_required", True))
    first = _loop_when(body)
    chat_id = str(body.get("chat_id") or CFG.get("telegram_chat_id") or "")
    if _truthy(body.get("dedupe", True)):
        existing = await _loop_find_near(
            first, kind, chat_id, int(body.get("dedupe_window_minutes") or 30))
        if existing:
            return _ok(
                action="loops.create",
                loop=existing,
                created=False,
                deduped=True,
                alarm=None,
                alarm_ok=None,
                reply=_loop_reply(existing, False),
                **_loop_meta(existing),
            )
    goal_id = str(body.get("goal_id") or "").strip()
    if goal_id and not re.fullmatch(r"[0-9a-fA-F-]{36}", goal_id):
        return _fail("goal_id must be a UUID")
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    row = {
        "title": title[:160],
        "kind": kind,
        "prompt": prompt[:4000],
        "cadence_minutes": cadence,
        "next_run_at": first.isoformat(),
        "status": "active",
        "runs": 0,
        "max_runs": max_runs,
        "ack_required": ack_required,
        "chat_id": chat_id,
        "stop_condition": str(body.get("stop_condition") or "")[:1000] or None,
        "metadata": metadata,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if goal_id:
        row["goal_id"] = goal_id
    rows = await sb_table_upsert("loops", [row])
    loop = rows[0] if rows else row
    alarm = None
    if kind in ("wake", "alarm") and _truthy(body.get("alarm", True)):
        alarm = await _android_alarm_create({
            "hour": first.hour,
            "minute": first.minute,
            "label": title[:80],
            "skip_ui": True,
        })
    alarm_ok = bool(alarm.get("ok")) if alarm else None
    if kind in ("wake", "alarm", "reminder"):
        reply = _loop_reply(loop, True)
        if kind in ("wake", "alarm") and alarm_ok is False:
            reply += " ⚠️ منبه الموبايل مش متاح."
    else:
        reply = (
            f"🔁 *تم إنشاء لوب*\n\n"
            f"• {title}\n"
            f"• النوع: {kind}\n"
            f"• أول تنبيه: {first.strftime('%Y-%m-%d %H:%M')} (القاهرة)\n"
            f"• التكرار: كل {cadence} دقيقة، بحد أقصى {max_runs} مرة\n"
        )
    return _ok(action="loops.create", loop=loop, alarm=alarm, alarm_ok=alarm_ok,
               reply=reply, **_loop_meta(loop))


async def _ensure_alarm(body: dict) -> dict:
    """Keep one active wake/alarm per chat, rescheduling when the time moves."""
    first = _loop_when(body)
    kind = str(body.get("kind") or "alarm").lower().strip()
    if kind not in ("wake", "alarm", "reminder"):
        kind = "alarm"
    title = str(body.get("title") or body.get("name") or "منبه").strip() or "منبه"
    prompt = str(body.get("prompt") or body.get("question") or body.get("message") or title).strip()
    chat_id = str(body.get("chat_id") or CFG.get("telegram_chat_id") or "")
    window = max(1, min(int(body.get("dedupe_window_minutes") or 30), 120)) * 60

    if kind in ("wake", "alarm"):
        rows = await _loop_active_alarms(chat_id)
        near = None
        for row in rows:
            dt = _loop_parse_dt(row.get("next_run_at"))
            if not dt:
                continue
            delta = abs((
                dt.replace(second=0, microsecond=0)
                - first.replace(second=0, microsecond=0)
            ).total_seconds())
            if delta <= window:
                near = row
                break
        if near:
            await _loop_cancel_others(rows, str(near.get("id")))
            return _ok(
                action="loops.ensure_alarm",
                loop=near,
                created=False,
                deduped=True,
                rescheduled=False,
                alarm=None,
                alarm_ok=None,
                reply=_loop_reply(near, False),
                **_loop_meta(near),
            )
        if rows:
            def _distance(row: dict) -> float:
                dt = _loop_parse_dt(row.get("next_run_at"))
                return abs((dt - first).total_seconds()) if dt else float("inf")

            target = min(rows, key=_distance)
            now = datetime.now(timezone.utc).isoformat()
            patch = {
                "next_run_at": first.isoformat(),
                "title": title[:160],
                "prompt": prompt[:4000],
                "updated_at": now,
            }
            updated = await sb_table_patch(
                "loops", "id=eq." + quote(str(target["id"])), patch)
            row = updated[0] if updated else {**target, **patch}
            await _loop_cancel_others(rows, str(row.get("id")))
            return _ok(
                action="loops.ensure_alarm",
                loop=row,
                created=False,
                deduped=False,
                rescheduled=True,
                alarm=None,
                alarm_ok=None,
                reply=_loop_reschedule_reply(row),
                **_loop_meta(row),
            )
        res = await _loop_create({**body, "title": title, "kind": kind, "dedupe": False})
        if not res.get("ok"):
            return res
        return _ok(
            action="loops.ensure_alarm",
            loop=res.get("loop"),
            created=True,
            deduped=False,
            rescheduled=False,
            alarm=res.get("alarm"),
            alarm_ok=res.get("alarm_ok"),
            reply=res.get("reply", ""),
            when_human=res.get("when_human"),
            relative_ar=res.get("relative_ar"),
            minutes_from_now=res.get("minutes_from_now"),
        )

    existing = await _loop_find_near(
        first, kind, chat_id, int(body.get("dedupe_window_minutes") or 30))
    if existing:
        return _ok(
            action="loops.ensure_alarm",
            loop=existing,
            created=False,
            deduped=True,
            rescheduled=False,
            alarm=None,
            alarm_ok=None,
            reply=_loop_reply(existing, False),
            **_loop_meta(existing),
        )
    res = await _loop_create({**body, "title": title, "kind": kind, "dedupe": False})
    if not res.get("ok"):
        return res
    return _ok(
        action="loops.ensure_alarm",
        loop=res.get("loop"),
        created=True,
        deduped=False,
        rescheduled=False,
        alarm=res.get("alarm"),
        alarm_ok=res.get("alarm_ok"),
        reply=res.get("reply", ""),
        when_human=res.get("when_human"),
        relative_ar=res.get("relative_ar"),
        minutes_from_now=res.get("minutes_from_now"),
    )


async def _loop_list(body: dict) -> dict:
    status = str(body.get("status") or "").strip()
    if _truthy(body.get("all")):
        q = "select=*&order=created_at.desc&limit=50"
    elif status:
        q = f"status=eq.{quote(status)}&select=*&order=next_run_at.asc&limit=50"
    else:
        q = "status=in.(active,waiting,paused)&select=*&order=next_run_at.asc&limit=50"
    rows = await sb_table_get("loops", q)
    return _ok(action="loops.list", count=len(rows), loops=rows,
               reply="\n".join(
                   f"• {r.get('title')} — {r.get('status')} — {r.get('runs')}/{r.get('max_runs')} — {r.get('next_run_at')}"
                   for r in rows) or "مفيش لوبات نشطة.")


async def _loop_ack(body: dict) -> dict:
    lid = _loop_id(body)
    kind = str(body.get("kind") or "").strip().lower()
    kind_q = "kind=in.(wake,alarm)&" if kind in ("wake", "alarm") else ""
    if lid:
        rows = await sb_table_get("loops", f"id=eq.{quote(lid)}&select=*&limit=1")
    else:
        rows = await sb_table_get(
            "loops",
            f"{kind_q}status=in.(active,waiting)&ack_required=eq.true&select=*&order=next_run_at.asc&limit=1",
        )
        if not rows and kind_q:
            rows = await sb_table_get(
                "loops",
                "status=in.(active,waiting)&ack_required=eq.true&select=*&order=next_run_at.asc&limit=1",
            )
    if not rows:
        return _fail("مفيش لوب نشط محتاج تأكيد")
    row = rows[0]
    now = datetime.now(timezone.utc).isoformat()
    await sb_table_patch("loops", "id=eq." + quote(str(row["id"])), {
        "status": "done", "acked_at": now, "updated_at": now,
    })
    row.update({"status": "done", "acked_at": now, "updated_at": now})
    return _ok(action="loops.ack", loop=row,
               reply=f"✅ تم تأكيد «{row.get('title')}» وإيقاف اللوب.")


async def _loop_snooze(body: dict) -> dict:
    lid = _loop_id(body)
    if not lid:
        return _fail("loop_id is required")
    minutes = max(1, min(int(body.get("minutes") or 10), 1440))
    now = datetime.now(timezone.utc)
    next_iso = (now + timedelta(minutes=minutes)).isoformat()
    rows = await sb_table_get("loops", f"id=eq.{quote(lid)}&select=*&limit=1")
    if not rows:
        return _fail("loop not found")
    row = rows[0]
    await sb_table_patch("loops", "id=eq." + quote(lid), {
        "status": "active",
        "next_run_at": next_iso,
        "snoozes": int(row.get("snoozes") or 0) + 1,
        "updated_at": now.isoformat(),
    })
    return _ok(action="loops.snooze", loop_id=lid, minutes=minutes, next_run_at=next_iso,
               reply=f"⏰ تمام، هفكرك تاني بعد {minutes} دقيقة.")


async def _loop_set_status(body: dict, status: str) -> dict:
    lid = _loop_id(body)
    if not lid:
        return _fail("loop_id is required")
    now = datetime.now(timezone.utc).isoformat()
    patch = {"status": status, "updated_at": now}
    if status == "active":
        patch["next_run_at"] = datetime.now(timezone.utc).isoformat()
    await sb_table_patch("loops", "id=eq." + quote(lid), patch)
    return _ok(action=f"loops.{status}", loop_id=lid, status=status,
               reply=f"✅ اللوب بقى {status}.")


async def _loop_update(body: dict) -> dict:
    lid = _loop_id(body)
    if not lid:
        return _fail("loop_id is required")
    allowed = ("title", "prompt", "cadence_minutes", "max_runs", "ack_required",
               "next_run_at", "stop_condition", "chat_id")
    patch = {}
    for key in allowed:
        if body.get(key) is not None:
            patch[key] = body[key]
    if not patch:
        return _fail("nothing to update")
    patch["updated_at"] = datetime.now(timezone.utc).isoformat()
    await sb_table_patch("loops", "id=eq." + quote(lid), patch)
    rows = await sb_table_get("loops", f"id=eq.{quote(lid)}&select=*&limit=1")
    return _ok(action="loops.update", loop=rows[0] if rows else None)


async def _loop_delete(body: dict) -> dict:
    lid = _loop_id(body)
    if not lid:
        return _fail("loop_id is required")
    if not _truthy(body.get("confirm") or body.get("confirmed")):
        return _fail("loop delete needs confirm=true; use cancel to keep history")
    await sb_table_delete("loops", "id=eq." + quote(lid))
    return _ok(action="loops.delete", loop_id=lid)


async def _loops_due(body: dict) -> dict:
    if await get_flag("stop_all", False) or await get_flag("emergency_stop", False):
        return _ok(action="loops.due", due=0, sent=0, skipped="stop_all")
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    rows = await sb_table_get(
        "loops",
        f"status=eq.active&next_run_at=lte.{quote(now_iso)}&select=*&order=next_run_at.asc&limit=5",
    )
    sent = 0
    errors = []
    for r in rows:
        lid = str(r.get("id"))
        title = str(r.get("title") or "Loop")
        prompt = str(r.get("prompt") or title)
        kind = str(r.get("kind") or "checkin")
        runs = int(r.get("runs") or 0) + 1
        max_runs = int(r.get("max_runs") or 10)
        icon = "⏰" if kind in ("wake", "alarm") else ("🎯" if kind == "commitment" else "🔁")
        head = "وقت الاستيقاظ" if kind in ("wake", "alarm") else title
        text = f"{icon} *{head}*\n\n{prompt}\n\n({runs}/{max_runs})"
        base = PUBLIC_BASE_URL + "/webhook/loop-ack"
        lid_q = quote(lid)
        keyboard = {"inline_keyboard": [
            [{"text": "✅ تم", "url": f"{base}?action=ack&loop_id={lid_q}"}],
            [{"text": "⏰ بعد 10 دقائق", "url": f"{base}?action=snooze&loop_id={lid_q}&minutes=10"},
             {"text": "❌ إيقاف", "url": f"{base}?action=stop&loop_id={lid_q}"}],
        ]}
        ok = await telegram_send_payload({"text": text, "reply_markup": keyboard},
                                          chat_id=str(r.get("chat_id") or "") or None)
        if not ok:
            errors.append({"loop_id": lid, "error": "telegram send failed"})
            continue
        sent += 1
        status = "active"
        if runs >= max_runs:
            status = "expired" if _truthy(r.get("ack_required", True)) else "completed"
        await sb_table_patch("loops", "id=eq." + quote(lid), {
            "runs": runs,
            "last_run_at": now_iso,
            "next_run_at": (now + timedelta(minutes=int(r.get("cadence_minutes") or 30))).isoformat(),
            "status": status,
            "updated_at": now_iso,
        })
    return _ok(action="loops.due", due=len(rows), sent=sent, errors=errors)


async def _goal_create(body: dict) -> dict:
    desc = str(body.get("goal_description") or body.get("title") or body.get("goal") or "").strip()
    if not desc:
        return _fail("goal_description is required")
    row = {
        "goal_description": desc[:4000],
        "deadline": str(body.get("deadline") or "") or None,
        "last_progress": str(body.get("last_progress") or "") or None,
        "next_step": str(body.get("next_step") or "") or None,
        "status": str(body.get("status") or "active"),
    }
    rows = await sb_table_upsert("goals_memory", [row])
    return _ok(action="loops.goal_create", goal=rows[0] if rows else row,
               reply=f"🎯 تم تسجيل الهدف: {desc[:120]}")


async def _goal_list(body: dict) -> dict:
    status = str(body.get("status") or "").strip()
    q = (f"status=eq.{quote(status)}&" if status else "") + "select=*&order=created_at.desc&limit=50"
    rows = await sb_table_get("goals_memory", q)
    return _ok(action="loops.goal_list", count=len(rows), goals=rows)


async def _goal_update(body: dict) -> dict:
    gid = str(body.get("goal_id") or body.get("id") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", gid):
        return _fail("goal_id is required")
    allowed = ("goal_description", "deadline", "last_progress", "next_step", "status")
    patch = {k: body[k] for k in allowed if body.get(k) is not None}
    if not patch:
        return _fail("nothing to update")
    await sb_table_patch("goals_memory", "id=eq." + quote(gid), patch)
    rows = await sb_table_get("goals_memory", f"id=eq.{quote(gid)}&select=*&limit=1")
    return _ok(action="loops.goal_update", goal=rows[0] if rows else None)


async def _commitment_create(body: dict) -> dict:
    goal_res = await _goal_create(body)
    if not goal_res.get("ok"):
        return goal_res
    goal = goal_res.get("goal") or {}
    loop_body = dict(body)
    loop_body["goal_id"] = goal.get("id")
    loop_body["kind"] = "commitment"
    loop_body.setdefault("title", str(body.get("goal_description") or body.get("title") or "Commitment"))
    loop_body.setdefault("prompt", str(body.get("prompt") or body.get("goal_description") or "إيه آخر تقدم؟"))
    loop_res = await _loop_create(loop_body)
    if not loop_res.get("ok"):
        return loop_res
    return _ok(action="loops.commitment_create", goal=goal, loop=loop_res.get("loop"),
               reply=goal_res.get("reply", "") + "\n" + loop_res.get("reply", ""))


async def op_loops(body: dict) -> dict:
    action = str(body.get("action") or body.get("operation") or "list").lower().strip()
    try:
        if action in ("create", "start", "add"):
            return await _loop_create(body)
        if action in ("ensure_alarm", "ensure_wake", "ensure_reminder"):
            return await _ensure_alarm(body)
        if action in ("list", "status", "active"):
            return await _loop_list(body)
        if action in ("get", "show"):
            lid = _loop_id(body)
            if not lid:
                return _fail("loop_id is required")
            rows = await sb_table_get("loops", f"id=eq.{quote(lid)}&select=*&limit=1")
            return _ok(action="loops.get", loop=rows[0] if rows else None)
        if action in ("due", "tick"):
            return await _loops_due(body)
        if action in ("ack", "done", "woke", "complete"):
            return await _loop_ack(body)
        if action == "snooze":
            return await _loop_snooze(body)
        if action == "pause":
            return await _loop_set_status(body, "paused")
        if action == "resume":
            return await _loop_set_status(body, "active")
        if action in ("cancel", "stop"):
            return await _loop_set_status(body, "cancelled")
        if action == "update":
            return await _loop_update(body)
        if action == "delete":
            return await _loop_delete(body)
        if action in ("goal_create", "goal_add"):
            return await _goal_create(body)
        if action in ("goal_list", "goals"):
            return await _goal_list(body)
        if action == "goal_update":
            return await _goal_update(body)
        if action == "goal_done":
            body = {**body, "status": "done"}
            return await _goal_update(body)
        if action in ("commitment_create", "commitment"):
            return await _commitment_create(body)
        if action == "stats":
            statuses = ("active", "paused", "done", "expired", "cancelled")
            counts = {}
            for s in statuses:
                counts[s] = await sb_table_count("loops", f"status=eq.{s}")
            return _ok(action="loops.stats", counts=counts)
        if action in ("guard", "wake_guard"):
            now = datetime.now(timezone.utc)
            overdue = await sb_table_get(
                "loops",
                "status=eq.active&ack_required=eq.true&select=*&order=next_run_at.asc&limit=20",
            )
            stale = [r for r in overdue
                     if str(r.get("next_run_at") or "") < (now - timedelta(minutes=5)).isoformat()]
            return _ok(action="loops.guard", stale=len(stale), loops=stale)
        return _fail(f"unknown loops action '{action}'")
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}", action=action)


LOOPS_WORKER_TASK: asyncio.Task | None = None
HEARTBEAT_TASK: asyncio.Task | None = None
STARTED_AT = time.time()
HEARTBEAT_FILE = BASE_DIR / "heartbeat.json"
LAST_LOOP_TICK: str | None = None
LAST_LOOP_ERROR: str | None = None
LOOP_TICKS = 0


async def _loops_worker() -> None:
    """Background tick: send due loops and mark them as run."""
    global LAST_LOOP_TICK, LAST_LOOP_ERROR, LOOP_TICKS
    await asyncio.sleep(20)
    while True:
        try:
            await op_loops({"action": "due"})
            LAST_LOOP_TICK = datetime.now(timezone.utc).isoformat()
            LAST_LOOP_ERROR = None
            LOOP_TICKS += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LAST_LOOP_ERROR = f"{type(e).__name__}: {e}"
            log_action("loops", "worker", {}, "error", LAST_LOOP_ERROR, 0)
        await asyncio.sleep(60)


async def _heartbeat_worker() -> None:
    """Write a heartbeat file so an external watchdog can detect a hung server."""
    while True:
        payload = {
            "pid": os.getpid(),
            "role": ROLE,
            "time": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": int(time.time() - STARTED_AT),
            "loops_worker": bool(LOOPS_WORKER_TASK and not LOOPS_WORKER_TASK.done()),
            "last_loop_tick": LAST_LOOP_TICK,
            "loop_ticks": LOOP_TICKS,
            "last_loop_error": LAST_LOOP_ERROR,
            "mode": read_mode(),
        }
        try:
            save_json(HEARTBEAT_FILE, payload)
        except Exception:
            pass
        await asyncio.sleep(30)


# --------------------------------------------------------------------------- Android controls
def _android_out(action: str, result: dict) -> dict:
    out = dict(result) if isinstance(result, dict) else {"result": result}
    out["action"] = f"android.{action}"
    out["handled"] = True
    if "ok" not in out:
        try:
            out["ok"] = 200 <= int(out.get("status") or 0) < 300
        except Exception:
            out["ok"] = False
    return out


async def op_android(body: dict) -> dict:
    action = str(body.get("action") or "status").lower().strip()
    try:
        if action in ("status", "health"):
            h = await _android_health()
            peer = h.get("peer") or {}
            reply = (
                f"📱 *الأندرويد*\n\n"
                f"• ADB: {'متصل' if h.get('connected') else 'غير متصل'}\n"
                f"• الجهاز: {peer.get('host') or '—'}\n"
                f"• IP: {(peer.get('ips') or ['—'])[0]}\n"
                f"• المنفذ: {h.get('port')}\n"
                f"• الإيقاف: {'نشط' if h.get('stop_flag') else 'غير نشط'}"
            )
            return _android_out("status", {**h, "reply": reply})

        if action in ("reconnect", "connect_auto"):
            r = await _android_post("/device/reconnect", {"force": bool(body.get("force", False))})
            reply = "📱 " + ("تم الاتصال بالموبايل." if r.get("ok") else "تعذر الاتصال بالموبايل: " + str(r.get("error") or r.get("detail") or "غير معروف"))
            return _android_out("reconnect", {**r, "reply": reply})

        if action == "pair":
            host = str(body.get("host") or "").strip()
            code = str(body.get("code") or "").strip()
            if not host or not code:
                return _fail("host and code are required for pairing")
            r = await _android_post("/device/pair", {"host": host, "code": code,
                                                       "pair_port": int(body.get("pair_port") or 0)})
            return _android_out("pair", r)

        if action == "connect":
            host = str(body.get("host") or "").strip()
            if not host:
                return _fail("host is required (for example 100.64.0.1)")
            r = await _android_post("/device/connect", {"host": host,
                                                        "port": int(body.get("port") or 5555)})
            return _android_out("connect", r)

        if action == "tcpip":
            r = await _android_post("/device/tcpip", {"port": int(body.get("port") or 5555)})
            return _android_out("tcpip", r)

        if action in ("info", "device_info"):
            r = await _android_post("/device/info", {})
            return _android_out("info", r)

        if action in ("alarms", "alarms_list"):
            limit = max(1, min(200, int(body.get("limit") or 50)))
            r = await _android_post(f"/device/alarms/list?limit={limit}", {})
            return _android_out("alarms", r)

        if action in ("alarm_create", "create_alarm"):
            hour = int(body.get("hour"))
            minute = int(body.get("minute"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return _fail("hour/minute out of range")
            label = str(body.get("label") or "OTHVM")[:80]
            r = await _android_post("/device/alarm/create", {
                "hour": hour, "minute": minute, "label": label,
                "skip_ui": bool(body.get("skip_ui", True)),
            })
            return _android_out("alarm_create", r)

        if action in ("alarms_show", "show_alarms"):
            r = await _android_post("/device/alarms/show", {})
            return _android_out("alarms_show", r)

        if action in ("notifications", "notification_list"):
            limit = max(1, min(100, int(body.get("limit") or 20)))
            r = await _android_post(f"/device/notifications?limit={limit}", {})
            return _android_out("notifications", r)

        if action == "volume":
            payload = {"stream": int(body.get("stream") or 3)}
            if body.get("level") is not None and body.get("level") != "":
                payload["level"] = int(body.get("level"))
            r = await _android_post("/device/volume", payload)
            return _android_out("volume", r)

        if action in ("keyevent", "key"):
            raw_key = body.get("key", body.get("keycode"))
            repeat = max(1, min(10, int(body.get("repeat") or 1)))
            if isinstance(raw_key, str) and not raw_key.strip().isdigit():
                r = await _android_post("/device/key", {"key": raw_key, "repeat": repeat})
            else:
                keycode = int(raw_key)
                r = await _android_post("/device/keyevent", {"keycode": keycode, "repeat": repeat})
            return _android_out("keyevent", r)

        if action in ("screenshot", "screen"):
            r = await _android_post("/device/screenshot",
                                    {"include_base64": bool(body.get("include_base64", False))})
            if r.get("ok"):
                r["reply"] = f"📸 صورة الموبايل محفوظة: {r.get('path')}"
            return _android_out("screenshot", r)

        if action == "tap":
            r = await _android_post("/device/tap", {
                "x": int(body.get("x")), "y": int(body.get("y")),
                "duration_ms": int(body.get("duration_ms") or 0),
            })
            return _android_out("tap", r)

        if action == "swipe":
            r = await _android_post("/device/swipe", {
                "x1": int(body.get("x1")), "y1": int(body.get("y1")),
                "x2": int(body.get("x2")), "y2": int(body.get("y2")),
                "duration_ms": int(body.get("duration_ms") or 300),
            })
            return _android_out("swipe", r)

        if action in ("text", "type", "input_text"):
            value = str(body.get("text") or "")
            if not value:
                return _fail("text is required")
            r = await _android_post("/device/text", {
                "text": value, "submit": bool(body.get("submit", False)),
            })
            return _android_out("text", r)

        if action in ("open_app", "launch", "app_open"):
            app = str(body.get("app") or body.get("package") or "").strip()
            if not app:
                return _fail("app or package is required")
            r = await _android_post("/device/open_app", {
                "app": app, "package": str(body.get("package") or ""),
                "wait_ms": int(body.get("wait_ms") or 800),
            })
            if r.get("ok"):
                r["reply"] = f"📱 فتحت {r.get('package')}"
            return _android_out("open_app", r)

        if action in ("packages", "package_list"):
            params = []
            if body.get("query"):
                params.append("query=" + quote(str(body.get("query"))))
            params.append("include_system=" + ("true" if body.get("include_system") else "false"))
            params.append("limit=" + str(max(1, min(1000, int(body.get("limit") or 300)))))
            r = await _android_post("/device/packages?" + "&".join(params), {})
            return _android_out("packages", r)

        if action in ("ui", "ui_dump", "screen_nodes"):
            r = await _android_post(f"/device/ui?limit={max(1, min(500, int(body.get('limit') or 200)))}", {})
            return _android_out("ui", r)

        if action in ("tap_text", "tap_label"):
            text = str(body.get("text") or "").strip()
            if not text:
                return _fail("text is required")
            r = await _android_post("/device/tap_text", {
                "text": text, "exact": bool(body.get("exact", False)),
                "index": int(body.get("index") or 0),
            })
            return _android_out("tap_text", r)

        if action in ("current_app", "foreground_app"):
            r = await _android_post("/device/current_app", {})
            return _android_out("current_app", r)

        if action in ("sms_send", "send_sms", "sms"):
            number = str(body.get("number") or body.get("to") or "").strip()
            text = str(body.get("text") or body.get("message") or "").strip()
            if not number or not text:
                return _fail("number and text are required")
            r = await _android_post("/device/sms/send", {
                "number": number, "text": text,
                "exit_on_sent": bool(body.get("exit_on_sent", True)),
            })
            if r.get("opened"):
                r["reply"] = f"✉️ فتحت رسالة لـ {number} جاهزة للإرسال."
            return _android_out("sms_send", r)

        if action in ("sms_list", "messages", "inbox"):
            limit = max(1, min(100, int(body.get("limit") or 20)))
            r = await _android_post(f"/device/sms/list?limit={limit}", {})
            return _android_out("sms_list", r)

        if action in ("calls", "call_log"):
            limit = max(1, min(100, int(body.get("limit") or 20)))
            r = await _android_post(f"/device/calls?limit={limit}", {})
            return _android_out("calls", r)

        if action == "media":
            payload = {"action": str(body.get("media_action") or body.get("mode") or body.get("media") or "status")}
            if body.get("level") is not None:
                payload["level"] = int(body.get("level"))
            r = await _android_post("/device/media", payload)
            return _android_out("media", r)

        if action in ("dnd", "zen"):
            r = await _android_post("/device/dnd", {"mode": str(body.get("mode") or body.get("dnd") or "status")})
            return _android_out("dnd", r)

        if action == "brightness":
            payload = {}
            if body.get("percent") is not None:
                payload["percent"] = int(body.get("percent"))
            r = await _android_post("/device/brightness", payload)
            return _android_out("brightness", r)

        if action in ("network", "wifi_toggle", "data_toggle", "airplane"):
            target = str(body.get("target") or ("wifi" if action == "wifi_toggle" else "data" if action == "data_toggle" else "airplane"))
            r = await _android_post("/device/network", {
                "target": target, "enabled": bool(body.get("enabled", True)),
            })
            return _android_out("network", r)

        if action in ("screen_state", "wake_screen", "unlock"):
            state = str(body.get("state") or ("unlock" if action == "unlock" else "status"))
            r = await _android_post("/device/screen", {"state": state})
            return _android_out("screen", r)

        if action in ("open_url", "url"):
            url = str(body.get("url") or "").strip()
            if not url:
                return _fail("url is required")
            r = await _android_post("/device/open_url", {"url": url})
            return _android_out("open_url", r)

        if action in ("apps", "app_list"):
            inc = "true" if body.get("include_system") else "false"
            r = await _android_post(f"/device/apps?include_system={inc}", {})
            return _android_out("apps", r)

        if action == "battery":
            r = await _android_post("/device/battery", {})
            return _android_out("battery", r)

        if action == "storage":
            r = await _android_post("/device/storage", {})
            return _android_out("storage", r)

        if action == "wifi":
            r = await _android_post("/device/wifi", {})
            return _android_out("wifi", r)

        if action in ("log", "connection_log"):
            lines = max(1, min(500, int(body.get("lines") or 100)))
            r = await _android_post(f"/device/log?lines={lines}", {})
            return _android_out("log", r)

        if action in ("shell", "adb_shell"):
            command = str(body.get("command") or "").strip()
            if not command:
                return _fail("command is required")
            r = await _android_post("/device/shell", {
                "command": command,
                "timeout": max(1, min(60, int(body.get("timeout") or 20))),
            })
            return _android_out("shell", r)

        if action in ("emergency_stop", "stop"):
            r = await _android_post("/emergency_stop", {
                "active": bool(body.get("active", True)),
                "reason": str(body.get("reason") or "MCP android stop"),
            })
            return _android_out("emergency_stop", r)

        if action in ("stop_clear", "clear"):
            r = await _android_post("/stop/clear", {})
            return _android_out("stop_clear", r)

        return _fail(f"unknown android action '{action}'")
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}", action=action)


def _as_vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


async def op_knowledge(body: dict) -> dict:
    op = str(body.get("operation") or body.get("action") or "").strip()
    op = op.replace("knowledge.", "")
    if op in ("search", "stats", "listSources", "get"):
        require("read", "knowledge." + op)
    elif op in ("add", "ingest"):
        require("write", "knowledge." + op)
    elif op == "delete":
        require("full", "knowledge.delete")
    try:
        if op == "search":
            query = str(body.get("query", "")).strip()
            if not query:
                return _fail("query is required")
            limit = max(1, min(int(body.get("limit", 5)), 50))
            vec = (await gemini_embed([query], task_type="RETRIEVAL_QUERY"))[0]
            payload = {
                "query_embedding": _as_vector_literal(vec),
                "match_count": limit,
                "filter_source": str(body["filter_source"]) if body.get("filter_source") else None,
            }
            rows = await sb_rpc("match_knowledge", payload) or []
            min_sim = float(body.get("min_similarity", 0) or 0)
            rows = [r for r in rows if float(r.get("similarity") or 0) >= min_sim]
            for r in rows:
                if r.get("similarity") is not None:
                    r["similarity"] = round(float(r["similarity"]), 4)
            return _ok(operation="knowledge.search", query=query, count=len(rows), results=rows,
                       note="no results" if not rows else "ok")

        if op in ("add", "ingest"):
            source = str(body.get("source", "")).strip()
            if not source:
                return _fail("source is required")
            items = body.get("items")
            if op == "add":
                items = [{
                    "source_id": body.get("source_id"),
                    "title": body.get("title"),
                    "content": body.get("content"),
                    "metadata": body.get("metadata"),
                }]
            if not isinstance(items, list) or not items:
                return _fail("items[] (or content) is required")
            do_chunk = body.get("chunk", op == "ingest") is not False
            replace = body.get("replace", True) is not False
            prepared, skipped = [], 0
            for it in items:
                if not isinstance(it, dict):
                    skipped += 1
                    continue
                content = str(it.get("content") or "").strip()
                if not content:
                    skipped += 1
                    continue
                sid = it.get("source_id")
                title = it.get("title") or (content[:80].replace("\n", " "))
                meta = it.get("metadata") if isinstance(it.get("metadata"), dict) else {}
                pieces = chunk_text(content) if do_chunk else [content]
                for idx, piece in enumerate(pieces):
                    prepared.append({
                        "source": source,
                        "source_id": f"{sid}#{idx}" if (sid is not None and len(pieces) > 1) else sid,
                        "title": title,
                        "content": piece,
                        "metadata": {**meta, "chunk_index": idx, "chunks": len(pieces)},
                    })
            if not prepared:
                return _fail("no valid content to ingest", skipped=skipped)
            if replace:
                if op == "add" and body.get("source_id"):
                    await sb_delete(f"source=eq.{_quote(source)}&source_id=eq.{_quote(str(body['source_id']))}")
                elif op == "ingest" and body.get("replace_all"):
                    await sb_delete(f"source=eq.{_quote(source)}")
            vectors = await gemini_embed([p["content"] for p in prepared])
            for p, v in zip(prepared, vectors):
                p["embedding"] = _as_vector_literal(v)
            inserted = 0
            for i in range(0, len(prepared), 50):
                batch = prepared[i:i + 50]
                await sb_insert(batch)
                inserted += len(batch)
            return _ok(operation="knowledge." + op, source=source, chunks=inserted,
                       documents=len(items), skipped=skipped)

        if op == "stats":
            total = await sb_count()
            rows = await sb_select("select=source&limit=100000")
            counts: dict[str, int] = {}
            for r in rows:
                counts[r.get("source", "?")] = counts.get(r.get("source", "?"), 0) + 1
            return _ok(operation="knowledge.stats", total_chunks=total, by_source=counts)

        if op == "listSources":
            rows = await sb_select("select=source&limit=100000")
            counts: dict[str, int] = {}
            for r in rows:
                counts[r.get("source", "?")] = counts.get(r.get("source", "?"), 0) + 1
            return _ok(operation="knowledge.listSources", count=len(counts),
                       sources=[{"source": k, "chunks": v} for k, v in sorted(counts.items())])

        if op == "get":
            kid = str(body.get("id", "")).strip()
            if not kid:
                return _fail("id is required")
            rows = await sb_select(f"select=*&id=eq.{_quote(kid)}&limit=1")
            return _ok(operation="knowledge.get", found=bool(rows), item=rows[0] if rows else None)

        if op == "delete":
            if not body.get("confirm"):
                return {"ok": False, "needs_approval": True,
                        "error": "knowledge.delete is irreversible — ask the founder, then call again with confirm=true."}
            kid = str(body.get("id", "")).strip()
            src = str(body.get("source", "")).strip()
            sid = body.get("source_id")
            if kid:
                await sb_delete(f"id=eq.{_quote(kid)}")
                return _ok(operation="knowledge.delete", deleted_by="id", id=kid)
            if src:
                q = f"source=eq.{_quote(src)}"
                if sid is not None:
                    q += f"&source_id=eq.{_quote(str(sid))}"
                await sb_delete(q)
                return _ok(operation="knowledge.delete", deleted_by="source", source=src, source_id=sid)
            return _fail("id or source is required")

        return _fail(f'unknown knowledge operation "{op}" (search | add | ingest | stats | listSources | get | delete)')
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")


def _quote(value: str) -> str:
    return str(value).replace('"', "")


# --------------------------------------------------------------------------- unified memory v1
MEMORY_NAMESPACES = {"system", "personal"}
MEMORY_LEGACY_CATEGORIES = {
    "personal": {"personal_info", "preferences", "daily_patterns", "personal_context"},
    "system": {"capabilities", "workflows", "connected_tools", "operational_rules"},
}
MEMORY_LEGACY_FALLBACK = {"personal": "personal_context", "system": "operational_rules"}


def _mem_namespace(value: Any) -> str:
    s = str(value or "").strip().lower()
    aliases = {"othvm": "system", "user": "personal", "core": "personal"}
    s = aliases.get(s, s)
    return s if s in MEMORY_NAMESPACES else "personal"


def _mem_slug(value: Any, default: str = "") -> str:
    s = str(value or "").strip()
    if not s:
        return default
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\u0600-\u06FF.\-]+", "", s, flags=re.UNICODE)
    return s[:160] or default


def _mem_embed_text(namespace: str, category: str, key: str, value: str) -> str:
    return f"{namespace} | {category} | {key}\n{value}"[:9000]


def _mem_compact(row: dict, max_value: int = 800) -> dict:
    if not isinstance(row, dict):
        return {}
    value = str(row.get("value") or "")
    out = {
        "id": row.get("id"),
        "namespace": row.get("namespace"),
        "category": row.get("category"),
        "key": row.get("key"),
        "value": value[:max_value],
        "importance": row.get("importance"),
        "version": row.get("version"),
        "status": row.get("status") or "active",
        "updated_at": row.get("updated_at"),
    }
    if len(value) > max_value:
        out["truncated"] = True
    if row.get("similarity") is not None:
        out["similarity"] = round(float(row["similarity"]), 4)
    if row.get("score") is not None:
        out["score"] = round(float(row["score"]), 4)
    if row.get("fallback"):
        out["fallback"] = row.get("fallback")
    return out


def _mem_dt(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _mem_merge_values(a: Any, b: Any) -> str:
    a = str(a or "").strip()
    b = str(b or "").strip()
    if not a:
        return b
    if not b:
        return a
    if b in a:
        return a
    if a in b:
        return b
    if len(a) <= 800 and len(b) <= 800 and len(a) + len(b) <= 1600:
        return a + "\n---\n" + b
    return a if len(a) >= len(b) else b


async def _mem_rest(table: str, method: str, query: str = "", payload: Any = None,
                    prefer: str | None = None, timeout: int = 45) -> Any:
    url = _sb_table_url(table)
    if query:
        url += "?" + query
    headers = _sb_headers()
    if prefer:
        headers["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, url, headers=headers,
                            json=payload if payload is not None else None)
    if r.status_code not in (200, 201, 204, 206):
        raise RuntimeError(f"memory {method} {table} failed ({r.status_code}): {r.text[:400]}")
    if r.status_code == 204 or not r.text:
        return None
    try:
        return r.json()
    except Exception:
        return None


async def _mem_get(namespace: str, category: str, key: str) -> dict | None:
    q = (f"namespace=eq.{quote(namespace, safe='')}"
         f"&category=eq.{quote(category, safe='')}"
         f"&key=eq.{quote(key, safe='')}"
         "&select=id,namespace,category,key,value,importance,status,version,metadata,access_count,created_at,updated_at"
         "&limit=1")
    rows = await sb_table_get("memory_items", q)
    return rows[0] if rows else None


async def _mem_get_by_key(namespace: str, key: str) -> dict | None:
    q = (f"namespace=eq.{quote(namespace, safe='')}"
         f"&key=eq.{quote(key, safe='')}"
         "&select=id,namespace,category,key,value,importance,status,version,metadata,access_count,created_at,updated_at"
         "&limit=1")
    rows = await sb_table_get("memory_items", q)
    return rows[0] if rows else None


async def _mem_find_by_hash(namespace: str, category: str, value: str) -> dict | None:
    h = hashlib.md5(value.encode("utf-8")).hexdigest()
    q = (f"namespace=eq.{quote(namespace, safe='')}"
         f"&category=eq.{quote(category, safe='')}"
         f"&value_hash=eq.{h}&limit=1")
    rows = await sb_table_get("memory_items", q)
    return rows[0] if rows else None


async def _mem_locate(body: dict) -> dict | None:
    if body.get("id"):
        q = "id=eq." + quote(str(body["id"]), safe="") + "&limit=1"
        rows = await sb_table_get("memory_items", q)
        return rows[0] if rows else None
    namespace = _mem_namespace(body.get("namespace") or body.get("scope"))
    category = _mem_slug(body.get("category")) if body.get("category") else None
    key = _mem_slug(body.get("key") or body.get("name"))
    if not key:
        return None
    if category:
        return await _mem_get(namespace, category, key)
    return await _mem_get_by_key(namespace, key)


async def _mem_event(action: str, *, memory_id: Any = None, namespace: str = "personal",
                     category: str = "", key: str = "", old_value: Any = None,
                     new_value: Any = None, reason: str = "", actor: str = "agent",
                     metadata: dict | None = None) -> None:
    try:
        await _mem_rest("memory_events", "POST", "", {
            "memory_id": memory_id,
            "namespace": namespace or "personal",
            "category": category or "",
            "key": key or "",
            "action": action,
            "old_value": old_value,
            "new_value": new_value,
            "reason": (reason or "")[:500] or None,
            "actor": (actor or "agent")[:80],
            "metadata": metadata or {},
        }, prefer="return=minimal")
    except Exception:
        pass


def _mem_legacy_target(namespace: str, category: str, key: str) -> tuple[str, str, str] | None:
    table = {"system": "othvm_memory", "personal": "othman_memory"}.get(namespace)
    if not table:
        return None
    allowed = MEMORY_LEGACY_CATEGORIES.get(namespace, set())
    if category in allowed:
        return table, category, key
    fallback = MEMORY_LEGACY_FALLBACK.get(namespace, "general")
    return table, fallback, f"{category}__{key}"[:160]


async def _mem_legacy_upsert(namespace: str, category: str, key: str, value: str,
                             version: int) -> bool:
    target = _mem_legacy_target(namespace, category, key)
    if not target:
        return False
    table, legacy_category, legacy_key = target
    try:
        await _mem_rest(table, "POST", "on_conflict=category,key", {
            "category": legacy_category,
            "key": legacy_key,
            "value": value,
            "version": int(version or 1),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }, prefer="resolution=merge-duplicates,return=minimal")
        return True
    except Exception:
        return False


async def _mem_legacy_delete(namespace: str, category: str, key: str) -> bool:
    target = _mem_legacy_target(namespace, category, key)
    if not target:
        return False
    table, legacy_category, legacy_key = target
    try:
        await _mem_rest(table, "DELETE",
                        f"category=eq.{quote(legacy_category, safe='')}&key=eq.{quote(legacy_key, safe='')}")
        return True
    except Exception:
        return False


async def _memory_sql(query: str) -> list[dict]:
    res = await sb_rpc("exec_sql", {"query": query})
    if not isinstance(res, dict) or not res.get("ok"):
        raise RuntimeError(str(res)[:400])
    rows = res.get("rows")
    return rows if isinstance(rows, list) else []


async def _memory_exec(query: str) -> int:
    res = await sb_rpc("exec_sql", {"query": query})
    if not isinstance(res, dict) or not res.get("ok"):
        raise RuntimeError(str(res)[:400])
    return int(res.get("row_count") or 0)


async def op_memory(body: dict) -> dict:
    raw = str(body.get("operation") or body.get("action") or "").strip().lower()
    op = raw.split(".", 1)[1] if raw.startswith("memory.") else raw
    if op in ("remember", "save", "upsert", "add", "forget", "delete", "remove",
              "restore", "undo", "embed", "reindex", "backfill"):
        require("write", "memory." + op)
    elif op in ("consolidate", "dedupe", "merge"):
        require("read" if body.get("dry_run", True) is not False else "write", "memory." + op)
    else:
        require("read", "memory." + (op or "recall"))
    try:
        if op in ("remember", "save", "upsert", "add"):
            namespace = _mem_namespace(body.get("namespace") or body.get("scope"))
            category = _mem_slug(body.get("category") or body.get("type") or "general", "general")
            value = str(body.get("value") or body.get("content") or body.get("text") or "").strip()
            if not value:
                return _fail("value is required")
            key = _mem_slug(body.get("key") or body.get("name"))
            existing = None
            if key:
                existing = await _mem_get(namespace, category, key)
            else:
                existing = await _mem_find_by_hash(namespace, category, value)
                key = _mem_slug((existing or {}).get("key") or "")
            if not key:
                key = "note_" + hashlib.md5(value.encode("utf-8")).hexdigest()[:12]
            importance = max(1, min(5, int(body.get("importance") or 3)))
            source = str(body.get("source") or "agent").strip()[:80] or "agent"
            meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            if existing and str(existing.get("value") or "") == value and existing.get("status") == "active":
                try:
                    await sb_rpc("touch_memory", {"p_ids": [existing["id"]]})
                except Exception:
                    pass
                return _ok(operation="memory.remember", changed=False, created=False,
                           item=_mem_compact(existing), note="unchanged")
            version = int(existing.get("version") or 0) + 1 if existing else 1
            merged_meta = {**(existing.get("metadata") or {}), **meta} if existing else dict(meta)
            embedding = None
            embed_error = ""
            if body.get("embed", True) is not False:
                try:
                    embedding = (await gemini_embed(
                        [_mem_embed_text(namespace, category, key, value)]
                    ))[0]
                except Exception as e:
                    embed_error = f"{type(e).__name__}: {e}"[:240]
            payload = {
                "namespace": namespace,
                "category": category,
                "key": key,
                "value": value,
                "importance": importance,
                "status": "active",
                "version": version,
                "source": source,
                "metadata": merged_meta,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if embedding is not None:
                payload["embedding"] = _as_vector_literal(embedding)
            elif existing and str(existing.get("value") or "") != value:
                payload["embedding"] = None
            if existing:
                rows = await _mem_rest(
                    "memory_items", "PATCH",
                    "id=eq." + quote(str(existing["id"]), safe=""),
                    payload, prefer="return=representation")
                action = "update"
            else:
                rows = await _mem_rest(
                    "memory_items", "POST", "", payload,
                    prefer="return=representation")
                action = "create"
            row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else {})
            await _mem_event(action, memory_id=row.get("id") or (existing or {}).get("id"),
                             namespace=namespace, category=category, key=key,
                             old_value=(existing or {}).get("value"), new_value=value,
                             reason=str(body.get("reason") or ""), actor=source, metadata=meta)
            legacy = await _mem_legacy_upsert(namespace, category, key, value, version)
            out = _ok(operation="memory.remember", changed=True, created=(action == "create"),
                      item=_mem_compact(row), legacy_synced=legacy,
                      embedded=embedding is not None)
            if embed_error:
                out["embed_error"] = embed_error
            return out

        if op in ("recall", "search", "find"):
            query = str(body.get("query") or body.get("q") or "").strip()
            namespace = _mem_namespace(body["namespace"]) if body.get("namespace") else None
            category = _mem_slug(body["category"]) if body.get("category") else None
            key = _mem_slug(body.get("key") or body.get("name")) if (body.get("key") or body.get("name")) else None
            limit = max(1, min(int(body.get("limit") or 6), 50))
            min_sim = float(body.get("min_similarity", 0.35) or 0)
            if key and not query:
                row = await _mem_locate({**body, "namespace": namespace or "personal", "category": category, "key": key})
                if not row:
                    return _ok(operation="memory.recall", count=0, results=[], note="no results")
                if row.get("status") != "active":
                    return _ok(operation="memory.recall", count=0, results=[],
                               note="item is " + str(row.get("status")))
                try:
                    await sb_rpc("touch_memory", {"p_ids": [row["id"]]})
                except Exception:
                    pass
                return _ok(operation="memory.recall", count=1, results=[_mem_compact(row)])
            if not query:
                return _fail("query or key is required")
            rows = []
            embed_error = ""
            try:
                vec = (await gemini_embed([query], task_type="RETRIEVAL_QUERY"))[0]
                rows = await sb_rpc("match_memory", {
                    "query_embedding": _as_vector_literal(vec),
                    "query_text": query,
                    "match_count": limit,
                    "filter_namespace": namespace,
                    "filter_category": category,
                    "min_similarity": min_sim,
                }) or []
            except Exception as e:
                embed_error = str(e)[:240]
            if not rows:
                rows = await sb_rpc("search_memory_text", {
                    "p_query": query,
                    "p_namespace": namespace,
                    "p_category": category,
                    "p_limit": limit,
                }) or []
                if embed_error:
                    for r in rows:
                        r["fallback"] = "keyword"
            if rows and rows[0].get("similarity") is not None:
                rows = [r for r in rows if float(r.get("similarity") or 0) >= min_sim]
            ids = [r.get("id") for r in rows if r.get("id")]
            if ids:
                try:
                    await sb_rpc("touch_memory", {"p_ids": ids})
                except Exception:
                    pass
            out = _ok(operation="memory.recall", query=query, count=len(rows),
                      results=[_mem_compact(r) for r in rows])
            if embed_error:
                out["embed_error"] = embed_error
                out["fallback"] = "keyword"
            if not rows:
                out["note"] = "no results"
            return out

        if op in ("forget", "delete", "remove"):
            if body.get("confirm") is not True and body.get("confirmed") is not True:
                return {"ok": False, "needs_approval": True,
                        "error": "memory.forget is destructive — ask the founder, then call again with confirm=true."}
            row = await _mem_locate(body)
            if not row:
                return _fail("memory item not found")
            if row.get("status") == "forgotten":
                return _ok(operation="memory.forget", forgotten=True, id=row.get("id"),
                           key=row.get("key"), note="already forgotten")
            await _mem_rest("memory_items", "PATCH",
                            "id=eq." + quote(str(row["id"]), safe=""),
                            {"status": "forgotten", "embedding": None,
                             "updated_at": datetime.now(timezone.utc).isoformat()},
                            prefer="return=minimal")
            await _mem_event("forget", memory_id=row.get("id"),
                             namespace=row.get("namespace") or "personal",
                             category=row.get("category") or "", key=row.get("key") or "",
                             old_value=row.get("value"), reason=str(body.get("reason") or ""),
                             actor=str(body.get("source") or "agent"))
            await _mem_legacy_delete(row.get("namespace") or "personal",
                                     row.get("category") or "", row.get("key") or "")
            return _ok(operation="memory.forget", forgotten=True, id=row.get("id"),
                       key=row.get("key"), restorable=True)

        if op in ("restore", "undo"):
            row = await _mem_locate(body)
            if not row:
                return _fail("memory item not found")
            if row.get("status") == "active":
                return _ok(operation="memory.restore", restored=False,
                           item=_mem_compact(row), note="already active")
            embedding = None
            try:
                embedding = (await gemini_embed([
                    _mem_embed_text(row.get("namespace") or "personal",
                                    row.get("category") or "", row.get("key") or "",
                                    row.get("value") or "")
                ]))[0]
            except Exception:
                embedding = None
            payload = {"status": "active",
                       "updated_at": datetime.now(timezone.utc).isoformat()}
            payload["embedding"] = _as_vector_literal(embedding) if embedding is not None else None
            await _mem_rest("memory_items", "PATCH",
                            "id=eq." + quote(str(row["id"]), safe=""),
                            payload, prefer="return=minimal")
            await _mem_event("restore", memory_id=row.get("id"),
                             namespace=row.get("namespace") or "personal",
                             category=row.get("category") or "", key=row.get("key") or "",
                             new_value=row.get("value"), reason=str(body.get("reason") or ""),
                             actor=str(body.get("source") or "agent"))
            await _mem_legacy_upsert(row.get("namespace") or "personal",
                                     row.get("category") or "", row.get("key") or "",
                                     row.get("value") or "", int(row.get("version") or 1))
            return _ok(operation="memory.restore", restored=True, id=row.get("id"),
                       key=row.get("key"))

        if op in ("consolidate", "dedupe", "merge"):
            dry = body.get("dry_run", True) is not False
            threshold = max(0.5, min(float(body.get("similarity_threshold") or 0.95), 0.999))
            namespace = _mem_namespace(body["namespace"]) if body.get("namespace") else None
            category = _mem_slug(body["category"]) if body.get("category") else None
            ns_clause = (" AND a.namespace = " + _sql_lit(namespace)) if namespace else ""
            cat_clause = (" AND a.category = " + _sql_lit(category)) if category else ""
            pairs = await _memory_sql(f"""
                SELECT a.id AS keep_id, b.id AS drop_id, a.namespace, a.category,
                       a.key AS keep_key, b.key AS drop_key,
                       a.value AS keep_value, b.value AS drop_value,
                       a.version AS keep_version, b.version AS drop_version,
                       a.importance AS keep_importance, b.importance AS drop_importance,
                       a.updated_at AS keep_updated, b.updated_at AS drop_updated,
                       a.metadata AS keep_metadata, b.metadata AS drop_metadata,
                       1 - (a.embedding_half <=> b.embedding_half) AS similarity
                FROM public.memory_items a
                JOIN public.memory_items b
                  ON a.namespace = b.namespace AND a.category = b.category
                 AND a.id < b.id
                 AND a.status = 'active' AND b.status = 'active'
                 AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                WHERE 1 - (a.embedding_half <=> b.embedding_half) >= {threshold}
                  {ns_clause} {cat_clause}
                ORDER BY similarity DESC
                LIMIT 200
            """)
            used: set[str] = set()
            merged: list[dict] = []
            for p in pairs:
                keep_id = str(p.get("keep_id") or "")
                drop_id = str(p.get("drop_id") or "")
                if not keep_id or not drop_id or keep_id in used or drop_id in used:
                    continue
                used.add(keep_id)
                used.add(drop_id)
                keep_side = "a"
                keep_imp = int(p.get("keep_importance") or 0)
                drop_imp = int(p.get("drop_importance") or 0)
                if drop_imp > keep_imp:
                    keep_side = "b"
                elif drop_imp == keep_imp:
                    keep_dt = _mem_dt(p.get("keep_updated"))
                    drop_dt = _mem_dt(p.get("drop_updated"))
                    if drop_dt > keep_dt:
                        keep_side = "b"
                    elif drop_dt == keep_dt and len(str(p.get("drop_value") or "")) > len(str(p.get("keep_value") or "")):
                        keep_side = "b"
                if keep_side == "a":
                    keeper_id, loser_id = keep_id, drop_id
                    keeper_key, loser_key = p.get("keep_key"), p.get("drop_key")
                    keeper_value, loser_value = p.get("keep_value"), p.get("drop_value")
                    keeper_version = int(p.get("keep_version") or 1)
                    keeper_meta = p.get("keep_metadata") or {}
                else:
                    keeper_id, loser_id = drop_id, keep_id
                    keeper_key, loser_key = p.get("drop_key"), p.get("keep_key")
                    keeper_value, loser_value = p.get("drop_value"), p.get("keep_value")
                    keeper_version = int(p.get("drop_version") or 1)
                    keeper_meta = p.get("drop_metadata") or {}
                merged_value = _mem_merge_values(keeper_value, loser_value)
                similarity = round(float(p.get("similarity") or 0), 4)
                summary = {
                    "keeper_id": keeper_id,
                    "keeper_key": keeper_key,
                    "dropped_id": loser_id,
                    "dropped_key": loser_key,
                    "similarity": similarity,
                }
                if dry:
                    merged.append(summary)
                    continue
                new_version = keeper_version + 1
                new_meta = dict(keeper_meta) if isinstance(keeper_meta, dict) else {}
                history = new_meta.get("consolidated_from")
                if not isinstance(history, list):
                    history = []
                history.append({"id": loser_id, "key": loser_key, "similarity": similarity})
                new_meta["consolidated_from"] = history[-20:]
                payload = {
                    "value": merged_value,
                    "version": new_version,
                    "metadata": new_meta,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                if merged_value != str(keeper_value or "").strip():
                    embedding = None
                    try:
                        embedding = (await gemini_embed([
                            _mem_embed_text(p.get("namespace") or "personal",
                                            p.get("category") or "", keeper_key or "",
                                            merged_value)
                        ]))[0]
                    except Exception:
                        embedding = None
                    payload["embedding"] = _as_vector_literal(embedding) if embedding is not None else None
                await _mem_rest("memory_items", "PATCH",
                                "id=eq." + quote(keeper_id, safe=""),
                                payload, prefer="return=minimal")
                await _mem_rest("memory_items", "PATCH",
                                "id=eq." + quote(loser_id, safe=""),
                                {"status": "superseded", "superseded_by": keeper_id,
                                 "updated_at": datetime.now(timezone.utc).isoformat()},
                                prefer="return=minimal")
                await _mem_legacy_delete(p.get("namespace") or "personal",
                                         p.get("category") or "", loser_key or "")
                await _mem_legacy_upsert(p.get("namespace") or "personal",
                                         p.get("category") or "", keeper_key or "",
                                         merged_value, new_version)
                await _mem_event("consolidate", memory_id=keeper_id,
                                 namespace=p.get("namespace") or "personal",
                                 category=p.get("category") or "", key=keeper_key or "",
                                 old_value=keeper_value, new_value=merged_value,
                                 reason="merged " + str(loser_key),
                                 metadata={"merged_id": loser_id, "similarity": similarity})
                await _mem_event("consolidate", memory_id=loser_id,
                                 namespace=p.get("namespace") or "personal",
                                 category=p.get("category") or "", key=loser_key or "",
                                 old_value=loser_value,
                                 reason="superseded by " + str(keeper_key),
                                 metadata={"keeper_id": keeper_id, "similarity": similarity})
                merged.append(summary)
            archived = 0
            if body.get("prune") and not dry:
                days = max(30, min(int(body.get("prune_days") or 365), 3650))
                archived = await _memory_exec(
                    "UPDATE public.memory_items SET status='archived', updated_at=now() "
                    "WHERE status='active' AND importance <= 2 AND access_count = 0 "
                    f"AND updated_at < now() - interval '{days} days' RETURNING id")
                if archived:
                    await _mem_event("archive", namespace=namespace or "all",
                                     reason=f"pruned {archived} stale low-importance items older than {days} days",
                                     metadata={"days": days})
            return _ok(operation="memory.consolidate", dry_run=dry, threshold=threshold,
                       candidate_pairs=len(pairs), merged=len(merged), archived=archived,
                       pairs=merged[:20])

        if op in ("embed", "reindex", "backfill"):
            limit = max(1, min(int(body.get("limit") or 25), 50))
            rows = await sb_table_get(
                "memory_items",
                f"embedding=is.null&status=eq.active&select=id,namespace,category,key,value"
                f"&order=updated_at.asc&limit={limit}")
            if not rows:
                return _ok(operation="memory.embed", embedded=0, remaining=0)
            texts = [_mem_embed_text(r.get("namespace") or "personal", r.get("category") or "",
                                     r.get("key") or "", r.get("value") or "") for r in rows]
            try:
                vectors = await gemini_embed(texts)
            except Exception as e:
                return _fail(f"embedding failed: {type(e).__name__}: {e}",
                             operation="memory.embed", pending=len(rows))
            items = [{"id": r["id"], "embedding": _as_vector_literal(v)}
                     for r, v in zip(rows, vectors)]
            n = await sb_rpc("memory_set_embeddings", {"p_items": items})
            remaining = await sb_table_get(
                "memory_items", "embedding=is.null&status=eq.active&select=id&limit=1000")
            return _ok(operation="memory.embed", embedded=int(n or len(items)),
                       remaining=len(remaining))

        if op in ("stats", "status"):
            rows = await _memory_sql(
                "select namespace, status, count(*)::int as n, count(embedding)::int as embedded "
                "from public.memory_items group by namespace, status order by namespace, status")
            total = sum(int(r.get("n") or 0) for r in rows)
            active = sum(int(r.get("n") or 0) for r in rows if r.get("status") == "active")
            missing = sum(int(r.get("n") or 0) - int(r.get("embedded") or 0)
                          for r in rows if r.get("status") == "active")
            events = await _memory_sql("select count(*)::int as n from public.memory_events")
            return _ok(operation="memory.stats", total=total, active=active,
                       missing_embeddings=missing,
                       events=int((events[0] or {}).get("n") or 0) if events else 0,
                       by_namespace=rows)

        if op in ("history", "events"):
            row = await _mem_locate(body)
            if not row:
                return _fail("memory item not found")
            events = await sb_table_get(
                "memory_events",
                "memory_id=eq." + quote(str(row["id"]), safe="") +
                "&order=created_at.desc&limit=20")
            return _ok(operation="memory.history", item=_mem_compact(row),
                       count=len(events), events=events)

        if op in ("list", "items"):
            parts = ["select=id,namespace,category,key,value,importance,status,version,access_count,updated_at",
                     "order=updated_at.desc",
                     f"limit={max(1, min(int(body.get('limit') or 50), 200))}"]
            if body.get("namespace"):
                parts.append("namespace=eq." + quote(_mem_namespace(body["namespace"]), safe=""))
            if body.get("category"):
                parts.append("category=eq." + quote(_mem_slug(body["category"]), safe=""))
            if body.get("status"):
                parts.append("status=eq." + quote(str(body["status"]), safe=""))
            rows = await sb_table_get("memory_items", "&".join(parts))
            return _ok(operation="memory.list", count=len(rows),
                       items=[_mem_compact(r) for r in rows])

        return _fail(f'unknown memory operation "{op}" '
                     "(remember | recall | forget | restore | consolidate | embed | stats | history | list)")
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}", operation="memory." + (op or "?"))


# --------------------------------------------------------------------------- context (conversation summaries)
def _sqlite_ro() -> sqlite3.Connection:
    path = str(CFG.get("n8n_db_path") or "").replace("\\", "/")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _parse_message_text(raw: Any) -> str:
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return str(raw or "").strip()
    if isinstance(obj, str):
        return obj.strip()
    if not isinstance(obj, dict):
        return ""
    content = obj.get("content")
    if isinstance(content, str):
        return content.strip()
    out = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            out.append(str(part["text"]))
    return "\n".join(out).strip()


def _platform_of(resource_id: str) -> str:
    res = str(resource_id or "")
    if res.startswith("integration:telegram"):
        return "telegram"
    if res.startswith("draft-chat"):
        return "preview"
    return "workflow"


def _thread_rows(limit: int = 50) -> list[dict]:
    con = _sqlite_ro()
    try:
        rows = con.execute(
            """
            SELECT m.threadId AS thread_id,
                   COUNT(*) AS messages,
                   MIN(m.createdAt) AS first_at,
                   MAX(m.createdAt) AS last_at,
                   t.title AS title,
                   t.resourceId AS resource_id
            FROM agents_messages m
            LEFT JOIN agents_threads t ON t.id = m.threadId
            GROUP BY m.threadId
            ORDER BY last_at DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        out.append({
            "thread_id": r["thread_id"],
            "messages": int(r["messages"]),
            "first_at": r["first_at"],
            "last_at": r["last_at"],
            "title": r["title"],
            "platform": _platform_of(r["resource_id"]),
        })
    return out


def _load_messages(thread_id: str, since_sqlite: str | None, limit: int = 300) -> tuple[list[dict], dict]:
    con = _sqlite_ro()
    try:
        q = "SELECT id, role, content, createdAt FROM agents_messages WHERE threadId = ?"
        args: list[Any] = [thread_id]
        if since_sqlite:
            q += " AND createdAt > ?"
            args.append(since_sqlite)
        q += " ORDER BY createdAt ASC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        rows = con.execute(q, args).fetchall()
        meta_row = con.execute("SELECT title, resourceId FROM agents_threads WHERE id = ?", (thread_id,)).fetchone()
    finally:
        con.close()
    msgs = []
    for r in rows:
        text = _parse_message_text(r["content"])
        if not text:
            continue
        msgs.append({"id": r["id"], "role": r["role"], "text": text, "at": r["createdAt"]})
    meta = {
        "title": (meta_row["title"] if meta_row else None),
        "platform": _platform_of(meta_row["resourceId"] if meta_row else ""),
    }
    return msgs, meta


def _sqlite_to_iso(value: str) -> str:
    try:
        dt = datetime.strptime(str(value)[:23], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        pass
    try:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return str(value)


def _iso_to_sqlite(value: str) -> str:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except Exception:
        return str(value).replace("T", " ")[:23]


async def sb_get(table: str, query: str) -> list[dict]:
    url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/{table}?{query}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(url, headers=_sb_headers())
    if r.status_code not in (200, 206):
        raise RuntimeError(f"Supabase select {table} failed ({r.status_code}): {r.text[:500]}")
    data = r.json()
    return data if isinstance(data, list) else []


async def sb_post(table: str, rows: list[dict]) -> None:
    url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/{table}"
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(url, headers={**_sb_headers(), "Prefer": "return=minimal"}, json=rows)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase insert {table} failed ({r.status_code}): {r.text[:600]}")


SUMMARY_SYSTEM = (
    "أنت تلخص محادثة بين المستخدم (المؤسس) ومساعده الشخصي OTHVM. "
    "الملخص بالعربي المصري، دقيق، بدون مبالغة، ويركز على ما يهم المستخدم لاحقاً. "
    "أخرج JSON فقط."
)

SUMMARY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "topics": {"type": "ARRAY", "items": {"type": "STRING"}},
        "open_items": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["summary"],
}


async def gemini_summarize(transcript: str, previous_summary: str | None) -> dict:
    key = str(CFG.get("gemini_api_key") or "").strip()
    if not key:
        raise RuntimeError("gemini_api_key is not configured")
    model = str(CFG.get("summary_model") or "gemini-3.8-flash")
    prompt_parts = [
        "لخص المحادثة التالية في 5 إلى 12 سطراً. غطِّ: الموضوعات الأساسية، القرارات، "
        "المهام المنجزة، أي وعود أو مواعيد، وأي تفضيلات أو معلومات شخصية مهمة عن المستخدم.",
        "اكتب summary كملخص مترابط، وtopics كوسوم قصيرة، وopen_items كالمهام أو الأسئلة المعلقة (لو مفيش، سيبها فاضية).",
    ]
    if previous_summary:
        prompt_parts.append("الملخص السابق (للسياق، حدّثه بالجديد):\n" + previous_summary[:4000])
    prompt_parts.append("الرسائل الجديدة:\n" + transcript[:30000])
    body = {
        "systemInstruction": {"parts": [{"text": SUMMARY_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": "\n\n".join(prompt_parts)}]}],
        "generationConfig": {
            "temperature": 0.25,
            "maxOutputTokens": 2500,
            "responseMimeType": "application/json",
            "responseSchema": SUMMARY_SCHEMA,
        },
    }
    async with httpx.AsyncClient(timeout=180) as c:
        r = await _gemini_post(
            c,
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key}, json_body=body,
        )
    if r.status_code != 200:
        raise RuntimeError(f"Gemini summarize failed ({r.status_code}): {r.text[:600]}")
    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(str(p.get("text", "")) for p in parts).strip()
    except Exception:
        raise RuntimeError("Gemini returned no candidates")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return {"summary": text[:4000], "topics": [], "open_items": []}
    if not isinstance(parsed, dict) or not str(parsed.get("summary") or "").strip():
        return {"summary": text[:4000], "topics": [], "open_items": []}
    return parsed


async def summarize_thread(thread_id: str, force: bool = False, min_new: int = 10, max_messages: int = 40) -> dict:
    latest = await sb_get(
        "conversation_summaries",
        f"select=*&thread_id=eq.{_quote(thread_id)}&order=created_at.desc&limit=1",
    )
    previous = latest[0] if latest else None
    since = _iso_to_sqlite(previous["last_message_at"]) if previous and previous.get("last_message_at") else None
    messages, meta = _load_messages(thread_id, since, limit=500)
    if not previous and messages:
        # first summary: cover the most recent messages, not ancient history
        messages = messages[-max(1, min(int(max_messages), 200)):]
    if not messages:
        return {"thread_id": thread_id, "skipped": True, "reason": "no new messages", "new_messages": 0}
    if len(messages) < int(min_new) and not force:
        return {"thread_id": thread_id, "skipped": True, "reason": f"only {len(messages)} new messages (min {min_new})",
                "new_messages": len(messages)}
    batch = messages[: max(1, min(int(max_messages), 200))]
    lines = []
    for m in batch:
        role = "المستخدم" if m["role"] == "user" else "OTHVM"
        lines.append(f"[{m['at'][11:19]}] {role}: {m['text'][:1800]}")
    transcript = "\n\n".join(lines)
    result = await gemini_summarize(transcript, previous.get("summary") if previous else None)
    row = {
        "thread_id": thread_id,
        "agent_id": CFG.get("agent_id"),
        "platform": meta.get("platform"),
        "title": meta.get("title"),
        "message_count": len(batch),
        "first_message_at": _sqlite_to_iso(batch[0]["at"]),
        "last_message_at": _sqlite_to_iso(batch[-1]["at"]),
        "last_message_id": batch[-1]["id"],
        "summary": str(result.get("summary") or "")[:8000],
        "topics": result.get("topics") or [],
        "open_items": result.get("open_items") or [],
    }
    await sb_post("conversation_summaries", [row])
    return {
        "thread_id": thread_id,
        "skipped": False,
        "new_messages": len(messages),
        "summarized_messages": len(batch),
        "summary": row["summary"],
        "topics": row["topics"],
        "open_items": row["open_items"],
        "last_message_at": row["last_message_at"],
    }


async def op_context(body: dict) -> dict:
    op = str(body.get("operation") or body.get("action") or "").strip().lower()
    op = op.replace("context.", "")
    try:
        if op == "sync":
            result = await _context_sync(limit=int(body.get("limit") or 400), full=_truthy(body.get("full")))
            return _ok(operation="context.sync", **result)

        if op in ("maintain", "summarize_log"):
            synced = await _context_sync(limit=int(body.get("limit") or 400))
            result = await _context_summarize_log(force=_truthy(body.get("force")),
                                                  min_new=int(body.get("min_new") or 10),
                                                  max_messages=int(body.get("max_messages") or 40))
            return _ok(operation="context.maintain", sync=synced, **result)

        if op in ("threads", "listthreads", "list"):
            threads = _thread_rows(int(body.get("limit", 30)))
            return _ok(operation="context.threads", count=len(threads), threads=threads)

        if op in ("latest", "get"):
            tid = str(body.get("thread_id") or "").strip() or str(CFG.get("main_telegram_thread_id"))
            rows = await sb_get(
                "conversation_summaries",
                f"select=*&thread_id=eq.{_quote(tid)}&order=created_at.desc&limit=1",
            )
            if not rows and not body.get("thread_id"):
                rows = await sb_get("conversation_summaries", "select=*&order=created_at.desc&limit=1")
            if not rows:
                return _ok(operation="context.latest", found=False, thread_id=tid,
                           note="لا يوجد ملخص بعد")
            s = rows[0]
            return _ok(operation="context.latest", found=True, thread_id=s.get("thread_id"),
                       title=s.get("title"), platform=s.get("platform"),
                       summary=s.get("summary"), topics=s.get("topics") or [],
                       open_items=s.get("open_items") or [],
                       message_count=s.get("message_count"),
                       last_message_at=s.get("last_message_at"),
                       created_at=s.get("created_at"))

        if op == "history":
            tid = str(body.get("thread_id") or "").strip()
            q = "select=id,thread_id,title,message_count,summary,topics,open_items,last_message_at,created_at"
            if tid:
                q += f"&thread_id=eq.{_quote(tid)}"
            q += f"&order=created_at.desc&limit={max(1, min(int(body.get('limit', 5)), 30))}"
            rows = await sb_get("conversation_summaries", q)
            return _ok(operation="context.history", count=len(rows), summaries=rows)

        if op == "summarize":
            force = bool(body.get("force"))
            min_new = max(1, min(int(body.get("min_new", 10)), 100))
            max_messages = max(1, min(int(body.get("max_messages", 40)), 200))
            targets: list[str] = []
            tid = str(body.get("thread_id") or "").strip()
            if tid:
                targets = [tid]
            elif body.get("all"):
                targets = [t["thread_id"] for t in _thread_rows(100) if t["platform"] in ("telegram", "preview")]
            else:
                targets = [str(CFG.get("main_telegram_thread_id"))]
            results = []
            for t in targets:
                try:
                    results.append(await summarize_thread(t, force=force, min_new=min_new, max_messages=max_messages))
                except Exception as e:
                    results.append({"thread_id": t, "skipped": False, "error": f"{type(e).__name__}: {e}"})
            done = [r for r in results if not r.get("skipped") and not r.get("error")]
            return _ok(operation="context.summarize", threads=len(targets), summarized=len(done), results=results)

        return _fail(f'unknown context operation "{op}" (threads | latest | history | summarize)')
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- code tools
CODE_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".next", ".nuxt", "dist", "build", "out", "target", "coverage", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "browser_profile", "temp",
    "logs", ".cache", "vendor", "bin", "obj",
}
CODE_LANG = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript", ".json": "json",
    ".md": "markdown", ".markdown": "markdown", ".yaml": "yaml", ".yml": "yaml",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css", ".sass": "css",
    ".less": "css", ".sql": "sql", ".sh": "shell", ".ps1": "powershell", ".java": "java",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".c": "c", ".cpp": "cpp",
    ".h": "c", ".hpp": "cpp", ".cs": "csharp", ".swift": "swift", ".kt": "kotlin",
}
CODE_EXTS = set(CODE_LANG)


def _code_dir(body: dict) -> Path:
    raw = str(body.get("path") or ".").strip() or "."
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (BASE_DIR / p).resolve()
    return p


def _code_file(body: dict) -> Path:
    raw = str(body.get("path") or body.get("file") or "").strip()
    if not raw:
        raise ValueError("path is required")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (BASE_DIR / p).resolve()
    return p


def _should_skip(name: str, include_ignored: bool = False) -> bool:
    if include_ignored:
        return False
    return name in CODE_IGNORE_DIRS or name.startswith(".venv") or name.endswith(".pyc")


def _iter_code_files(root: Path, max_depth: int = 10, include_ignored: bool = False):
    root = root.resolve()
    depth_cap = max(1, min(int(max_depth or 10), 30))
    for current, dirs, files in os.walk(root):
        cur = Path(current)
        try:
            rel_depth = len(cur.relative_to(root).parts)
        except Exception:
            rel_depth = 0
        dirs[:] = sorted(d for d in dirs if not _should_skip(d, include_ignored))
        if rel_depth >= depth_cap:
            dirs[:] = []
        for name in sorted(files):
            if _should_skip(name, include_ignored):
                continue
            p = cur / name
            if p.is_symlink() or not p.is_file():
                continue
            yield p


def _rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root)).replace("\\", "/")
    except Exception:
        return str(p)


def _lang_for(path: Path) -> str:
    return CODE_LANG.get(path.suffix.lower(), "text")


def _project_python(start: Path) -> str:
    """Prefer a project virtualenv (up to two parent levels) over the MCP service interpreter."""
    cur = start if start.is_dir() else start.parent
    for _ in range(3):
        for venv in (".venv", "venv", "env"):
            candidate = cur / venv / "Scripts" / "python.exe"
            if candidate.exists():
                return str(candidate)
        if cur.parent == cur:
            break
        cur = cur.parent
    return sys.executable


def _complexity_of_source(text: str) -> int:
    # Cheap, language-agnostic proxy: decision points + 1.
    tokens = re.findall(r"\b(if|else|elif|for|while|case|catch|except|with|and|or|switch|match|when|select)\b|&&|\|\||\?\.", text)
    return 1 + len(tokens)


def _py_symbols(tree: ast.AST, source: str = "") -> tuple[list[dict], list[dict]]:
    funcs: list[dict] = []
    classes: list[dict] = []

    def walk(node: ast.AST, parent: str | None = None):
        if isinstance(node, ast.ClassDef):
            methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", None),
                "methods_count": len(methods),
                "bases": [getattr(b, "id", getattr(b, "attr", "?")) for b in node.bases],
                "docstring": (ast.get_docstring(node) or "")[:200] or None,
            })
            for child in node.body:
                walk(child, node.name)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            body_src = ast.get_source_segment(source, node) or "" if source else ""
            funcs.append({
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", None),
                "args": args,
                "async": isinstance(node, ast.AsyncFunctionDef),
                "class": parent,
                "complexity": _complexity_of_source(body_src) if body_src else 1,
                "docstring": (ast.get_docstring(node) or "")[:200] or None,
            })
            for child in node.body:
                walk(child, parent)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, parent)

    walk(tree)
    return funcs, classes


def _py_imports(tree: ast.AST) -> list[dict]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append({"module": a.name, "alias": a.asname, "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            out.append({"module": ("." * (node.level or 0)) + (node.module or ""),
                        "names": [a.name for a in node.names], "line": node.lineno})
    return out


def _analyze_python(path: Path, text: str) -> dict:
    info = {"language": "python", "functions": [], "classes": [], "imports": [],
            "complexity": _complexity_of_source(text)}
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        info["parse_error"] = f"SyntaxError: {e.msg} (line {e.lineno})"
        # Fallback: line-level regex so the tool still returns something useful.
        for i, line in enumerate(text.splitlines(), 1):
            m = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", line)
            if m:
                info["functions"].append({"name": m.group(1), "line": i, "args": [a.strip() for a in m.group(2).split(",") if a.strip()], "class": None})
            m = re.match(r"\s*class\s+([A-Za-z_]\w*)", line)
            if m:
                info["classes"].append({"name": m.group(1), "line": i})
            m = re.match(r"\s*(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))", line)
            if m:
                info["imports"].append({"module": (m.group(1) or m.group(2) or "").strip(), "line": i})
        return info
    funcs, classes = _py_symbols(tree, text)
    info.update({"functions": funcs, "classes": classes, "imports": _py_imports(tree)})
    if funcs:
        info["max_function_complexity"] = max(f.get("complexity", 1) for f in funcs)
    info["docstring"] = (ast.get_docstring(tree) or "")[:300] or None
    return info


def _analyze_js(path: Path, text: str) -> dict:
    lang = "typescript" if path.suffix.lower() in (".ts", ".tsx") else "javascript"
    funcs, classes, imports = [], [], []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        for pat in (
            r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)",
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>",
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?([A-Za-z_$][\w$]*)\s*=>",
        ):
            m = re.search(pat, line)
            if m:
                funcs.append({"name": m.group(1), "line": i, "args": [a.strip() for a in (m.group(2) or "").split(",") if a.strip()], "class": None})
                break
        m = re.search(r"\bclass\s+([A-Za-z_$][\w$]*)", line)
        if m:
            classes.append({"name": m.group(1), "line": i})
        m = re.search(r"\bimport\s+(?:[\w*{}\s,]+\s+from\s+)?['\"]([^'\"]+)['\"]", line)
        if m:
            imports.append({"module": m.group(1), "line": i})
        else:
            m = re.search(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)", line)
            if m:
                imports.append({"module": m.group(1), "line": i})
    return {"language": lang, "functions": funcs, "classes": classes, "imports": imports,
            "complexity": _complexity_of_source(text), "lines": len(lines)}


def _analyze_markdown(path: Path, text: str) -> dict:
    headings, links = [], []
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            headings.append({"level": len(m.group(1)), "title": m.group(2).strip(), "line": i})
        for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", line):
            links.append({"text": m.group(1)[:120], "url": m.group(2)[:300], "line": i})
    return {"language": "markdown", "headings": headings, "links": links,
            "links_count": len(links), "headings_count": len(headings),
            "words": len(re.findall(r"\S+", text)), "complexity": 1}


def _analyze_json(path: Path, text: str) -> dict:
    try:
        obj = json.loads(text)
    except Exception as e:
        return {"language": "json", "parse_error": f"JSONDecodeError: {e}", "complexity": 1}
    top = []
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:100]:
            top.append({"key": k, "type": type(v).__name__,
                        "items": len(v) if isinstance(v, (list, dict)) else None})
    return {"language": "json", "root_type": type(obj).__name__, "keys": top,
            "keys_count": len(obj) if isinstance(obj, dict) else None,
            "complexity": 1 + len(re.findall(r"[{}\[\]]", text)) // 10}


def _analyze_yaml(path: Path, text: str) -> dict:
    try:
        import yaml  # type: ignore
        obj = yaml.safe_load(text)
        if isinstance(obj, dict):
            keys = [{"key": str(k), "type": type(v).__name__} for k, v in list(obj.items())[:100]]
        else:
            keys = []
        return {"language": "yaml", "root_type": type(obj).__name__, "keys": keys, "complexity": _complexity_of_source(text)}
    except Exception as e:
        keys = []
        for i, line in enumerate(text.splitlines(), 1):
            m = re.match(r"^([A-Za-z0-9_.-]+)\s*:", line)
            if m:
                keys.append({"key": m.group(1), "line": i})
        return {"language": "yaml", "keys": keys[:100], "parse_error": f"{type(e).__name__}: {e}",
                "complexity": _complexity_of_source(text)}


class _HTMLCollector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, int] = {}
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self.links: list[dict] = []
        self.title = ""
        self._in_title = False
        self._in_script = False
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        self.tags[tag] = self.tags.get(tag, 0) + 1
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "script":
            self._in_script = True
            if d.get("src"):
                self.scripts.append(d["src"])
        if tag == "style":
            self._in_style = True
        if tag == "link" and d.get("href"):
            self.links.append({"href": d["href"], "rel": d.get("rel")})

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_script = False
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_script:
            self.scripts.append(data.strip()[:500])
        if self._in_style:
            self.styles.append(data.strip()[:500])


def _analyze_html(path: Path, text: str) -> dict:
    p = _HTMLCollector()
    try:
        p.feed(text)
    except Exception:
        pass
    return {"language": "html", "title": p.title.strip() or None,
            "tags": [{"tag": k, "count": v} for k, v in sorted(p.tags.items(), key=lambda x: -x[1])[:50]],
            "scripts": p.scripts[:50], "styles_count": len(p.styles), "links": p.links[:50],
            "complexity": _complexity_of_source(text)}


def _analyze_css(path: Path, text: str) -> dict:
    selectors = []
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s and not s.startswith(("@", "/*", "*", "}")) and "{" in s:
            selectors.append({"selector": s.split("{")[0].strip()[:200], "line": i})
    imports = [m.group(1) for m in re.finditer(r"@import\s+(?:url\()?['\"]([^'\"]+)", text)]
    return {"language": "css", "selectors": selectors[:100], "selectors_count": len(selectors),
            "imports": imports, "complexity": _complexity_of_source(text)}


def analyze_code_file(path: Path) -> dict:
    if not path.exists() or not path.is_file():
        return _fail(f"file not found: {path}")
    if path.stat().st_size > 2_000_000:
        return _fail(f"file is too large to analyze ({path.stat().st_size} bytes, max 2 MB)")
    text = read_text_file(path, max_bytes=2_000_000)
    ext = path.suffix.lower()
    base = {"ok": True, "path": str(path), "language": _lang_for(path),
            "size_bytes": path.stat().st_size, "lines": len(text.splitlines()),
            "complexity_estimate": _complexity_of_source(text)}
    try:
        if ext == ".py":
            detail = _analyze_python(path, text)
        elif ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"):
            detail = _analyze_js(path, text)
        elif ext == ".json":
            detail = _analyze_json(path, text)
        elif ext in (".md", ".markdown"):
            detail = _analyze_markdown(path, text)
        elif ext in (".yaml", ".yml"):
            detail = _analyze_yaml(path, text)
        elif ext in (".html", ".htm"):
            detail = _analyze_html(path, text)
        elif ext in (".css", ".scss", ".sass", ".less"):
            detail = _analyze_css(path, text)
        else:
            detail = {"language": _lang_for(path), "note": "generic analysis only",
                      "imports": [], "functions": [], "classes": []}
    except Exception as e:
        detail = {"language": _lang_for(path), "parse_error": f"{type(e).__name__}: {e}",
                  "functions": [], "classes": [], "imports": []}
    base.update(detail)
    base["language"] = detail.get("language", base["language"])
    base["functions"] = detail.get("functions", [])
    base["classes"] = detail.get("classes", [])
    base["imports"] = detail.get("imports", [])
    base["complexity"] = detail.get("complexity", base["complexity_estimate"])
    base["complexity_estimate"] = base["complexity"]
    return base


def op_code_read_project(body: dict) -> dict:
    require("read", "code.read_project")
    root = _code_dir(body)
    if not root.exists() or not root.is_dir():
        return _fail(f"directory not found: {root}")
    max_depth = max(1, min(int(body.get("max_depth", 3) or 3), 15))
    include_ignored = bool(body.get("include_ignored", False))
    max_files = max(1, min(int(body.get("max_files", 5000) or 5000), 20000))
    summary: dict[str, dict] = {}
    total_files = 0
    total_bytes = 0
    largest: list[dict] = []
    tree: dict = {"name": root.name or str(root), "type": "folder", "children": []}
    dir_index: dict[str, dict] = {"": tree}

    def branch(rel_dir: str) -> dict:
        if rel_dir in dir_index:
            return dir_index[rel_dir]
        parent_rel = str(Path(rel_dir).parent).replace("\\", "/")
        if parent_rel == ".":
            parent_rel = ""
        parent = branch(parent_rel)
        node = {"name": Path(rel_dir).name, "type": "folder", "children": []}
        parent["children"].append(node)
        dir_index[rel_dir] = node
        return node

    for p in _iter_code_files(root, max_depth=max_depth, include_ignored=include_ignored):
        if total_files >= max_files:
            break
        try:
            st = p.stat()
        except Exception:
            continue
        total_files += 1
        total_bytes += st.st_size
        ext = p.suffix.lower() or "(none)"
        lang = _lang_for(p)
        s = summary.setdefault(ext, {"extension": ext, "language": lang, "count": 0, "bytes": 0})
        s["count"] += 1
        s["bytes"] += st.st_size
        largest.append({"path": _rel(root, p), "bytes": st.st_size, "extension": ext})
        rel = _rel(root, p)
        node = branch(str(Path(rel).parent).replace("\\", "/") if Path(rel).parent != Path(".") else "")
        node["children"].append({"name": p.name, "path": rel, "type": "file",
                                 "extension": ext, "language": lang, "size_bytes": st.st_size,
                                 "lines": None})
    # shallow line counts for small source files (best effort, capped)
    for i, entry in enumerate(largest[:200]):
        pass
    largest.sort(key=lambda x: -x["bytes"])
    by_type = sorted(summary.values(), key=lambda x: (-x["count"], x["extension"]))
    lang_totals: dict[str, dict] = {}
    for s in by_type:
        agg = lang_totals.setdefault(s["language"], {"language": s["language"], "count": 0, "bytes": 0})
        agg["count"] += s["count"]
        agg["bytes"] += s["bytes"]
    language_summary = sorted(lang_totals.values(), key=lambda x: -x["count"])
    return _ok(action="code.read_project", path=str(root), max_depth=max_depth,
               total_files=total_files, total_bytes=total_bytes,
               total_mb=round(total_bytes / 1024 / 1024, 2),
               types=by_type, language_summary=language_summary,
               largest_files=largest[:20], tree=tree,
               ignored_dirs=sorted(CODE_IGNORE_DIRS) if not include_ignored else [],
               truncated=total_files >= max_files)


def op_code_analyze_file(body: dict) -> dict:
    require("read", "code.analyze_file")
    try:
        path = _code_file(body)
    except ValueError as e:
        return _fail(str(e))
    return analyze_code_file(path)


def op_code_find_in_project(body: dict) -> dict:
    require("read", "code.find_in_project")
    pattern = str(body.get("pattern") or body.get("query") or "").strip()
    if not pattern:
        return _fail("pattern is required")
    root = _code_dir(body)
    if not root.exists() or not root.is_dir():
        return _fail(f"directory not found: {root}")
    flags = 0 if body.get("case_sensitive") else re.IGNORECASE
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return _fail(f"invalid regular expression: {e}")
    raw_ext = body.get("file_extensions") or body.get("extensions") or ""
    if isinstance(raw_ext, str):
        exts = {x.strip().lower() for x in raw_ext.replace(";", ",").split(",") if x.strip()}
    elif isinstance(raw_ext, list):
        exts = {str(x).strip().lower() for x in raw_ext if str(x).strip()}
    else:
        exts = set()
    exts = {("." + e.lstrip(".")) if e and not e.startswith(".") else e for e in exts}
    max_results = max(1, min(int(body.get("max_results", body.get("limit", 100)) or 100), 1000))
    max_depth = max(1, min(int(body.get("max_depth", 12) or 12), 30))
    context_lines = max(0, min(int(body.get("context_lines", 2) or 2), 5))
    max_file_bytes = max(1024, min(int(body.get("max_file_size", 2_000_000) or 2_000_000), 10_000_000))
    results = []
    files_scanned = 0
    for p in _iter_code_files(root, max_depth=max_depth, include_ignored=bool(body.get("include_ignored"))):
        ext = p.suffix.lower()
        if exts and ext not in exts:
            continue
        if not exts and ext not in (CODE_EXTS | TEXT_EXTS):
            continue
        try:
            if p.stat().st_size > max_file_bytes:
                continue
            text = read_text_file(p, max_bytes=max_file_bytes)
        except Exception:
            continue
        files_scanned += 1
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = rx.search(line)
            if not m:
                continue
            lo = max(0, i - context_lines)
            hi = min(len(lines), i + context_lines + 1)
            results.append({
                "file": str(p), "relative": _rel(root, p), "line": i + 1,
                "column": m.start() + 1, "match": m.group(0)[:300],
                "context": "\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi))[:3000],
            })
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break
    return _ok(action="code.find_in_project", path=str(root), pattern=pattern,
               file_extensions=sorted(exts) if exts else "all-code", files_scanned=files_scanned,
               count=len(results), results=results, truncated=len(results) >= max_results)


def _git(path: Path, args: list[str], timeout: int = 60) -> dict:
    git = shutil.which("git")
    if not git:
        return {"ok": False, "error": "git executable not found on PATH"}
    # The MCP service runs as SYSTEM; git otherwise refuses repositories owned by the
    # interactive user ("dubious ownership"). Scope the exception to this process only.
    base_args = [git, "-c", "safe.directory=*", "-c", "core.quotepath=false", "-C", str(path)]
    try:
        proc = subprocess.run(base_args + args, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"git timed out after {timeout}s", "args": args}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "args": args}
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[-30000:], "stderr": (proc.stderr or "")[-8000:],
            "args": args}


def _git_repo(body: dict) -> tuple[Path | None, dict | None]:
    path = _code_dir(body)
    if not path.exists() or not path.is_dir():
        return None, _fail(f"directory not found: {path}")
    probe = _git(path, ["rev-parse", "--is-inside-work-tree"], timeout=15)
    if not probe.get("ok"):
        return None, _fail(f"not a git repository: {path}", detail=(probe.get("stderr") or probe.get("error") or "")[-500:])
    return path, None


def op_code_git_status(body: dict) -> dict:
    require("read", "code.git_status")
    path, err = _git_repo(body)
    if err:
        return err
    r = _git(path, ["status", "--porcelain=v1", "-b"], timeout=60)
    if not r.get("ok"):
        return _fail(r.get("error") or r.get("stderr") or "git status failed")
    modified, staged, untracked, deleted, renamed, branch = [], [], [], [], [], None
    ahead = behind = 0
    for line in (r.get("stdout") or "").splitlines():
        if line.startswith("##"):
            rest = line[2:].strip()
            branch = rest.split("...")[0].strip() if rest else None
            m = re.search(r"\[ahead (\d+)(?:, behind (\d+))?\]", rest)
            if m:
                ahead = int(m.group(1) or 0)
                behind = int(m.group(2) or 0)
            continue
        if len(line) < 3:
            continue
        x, y, name = line[0], line[1], line[3:].strip()
        if x == "?" and y == "?":
            untracked.append(name)
            continue
        if x == "R" or y == "R":
            renamed.append(name)
        if x not in (" ", "?"):
            staged.append({"path": name, "status": x})
        if y not in (" ", "?"):
            if y == "D":
                deleted.append(name)
            else:
                modified.append({"path": name, "status": y})
    return _ok(action="code.git_status", path=str(path), branch=branch,
               ahead=ahead, behind=behind, clean=not (modified or staged or untracked or deleted or renamed),
               modified=modified, staged=staged, untracked=untracked,
               deleted=deleted, renamed=renamed, raw=(r.get("stdout") or "")[:10000])


def op_code_git_diff(body: dict) -> dict:
    require("read", "code.git_diff")
    path, err = _git_repo(body)
    if err:
        return err
    file = str(body.get("file") or "").strip()
    staged = bool(body.get("staged", False))
    args = ["diff", "--stat"]
    if staged:
        args.append("--cached")
    if file:
        args += ["--", file]
    stat = _git(path, args, timeout=60)
    args2 = ["diff"]
    if staged:
        args2.append("--cached")
    if file:
        args2 += ["--", file]
    diff = _git(path, args2, timeout=90)
    if not diff.get("ok"):
        return _fail(diff.get("error") or diff.get("stderr") or "git diff failed")
    text = diff.get("stdout") or ""
    limit = max(1000, min(int(body.get("max_chars", 120000) or 120000), 500000))
    return _ok(action="code.git_diff", path=str(path), file=file or None, staged=staged,
               stat=(stat.get("stdout") or "")[:10000], diff=text[:limit],
               diff_chars=len(text), truncated=len(text) > limit)


def op_code_git_log(body: dict) -> dict:
    require("read", "code.git_log")
    path, err = _git_repo(body)
    if err:
        return err
    count = max(1, min(int(body.get("count", 10) or 10), 100))
    file = str(body.get("file") or "").strip()
    args = ["log", f"-{count}", "--date=iso-strict", "--pretty=format:%H%x1f%an%x1f%ae%x1f%ad%x1f%s"]
    if file:
        args += ["--", file]
    r = _git(path, args, timeout=60)
    if not r.get("ok"):
        return _fail(r.get("error") or r.get("stderr") or "git log failed")
    commits = []
    for line in (r.get("stdout") or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5:
            commits.append({"hash": parts[0], "short": parts[0][:8], "author": parts[1],
                            "email": parts[2], "date": parts[3], "message": parts[4]})
    return _ok(action="code.git_log", path=str(path), count=len(commits), commits=commits)


def op_code_git_commit(body: dict) -> dict:
    require("write", "code.git_commit")
    path, err = _git_repo(body)
    if err:
        return err
    message = str(body.get("message") or "").strip()
    if not message:
        return _fail("commit message is required")
    files = body.get("files") or []
    if isinstance(files, str):
        files = [files]
    files = [str(f) for f in files][:200]
    add_args = ["add", "--"] + files if files else ["add", "-A"]
    add = _git(path, add_args, timeout=120)
    if not add.get("ok"):
        return _fail(f"git add failed: {add.get('stderr') or add.get('error')}")
    commit = _git(path, ["commit", "-m", message], timeout=120)
    if not commit.get("ok"):
        out = (commit.get("stdout") or "") + (commit.get("stderr") or "")
        if re.search(r"nothing to commit|no changes added", out, re.I):
            return _ok(action="code.git_commit", path=str(path), nothing_to_commit=True,
                       message=message, detail=out[-1000:])
        return _fail(f"git commit failed: {out[-2000:]}")
    head = _git(path, ["rev-parse", "HEAD"], timeout=15)
    return _ok(action="code.git_commit", path=str(path), message=message,
               commit=(head.get("stdout") or "").strip(), output=(commit.get("stdout") or "")[-2000:])


def op_code_git_push(body: dict) -> dict:
    require("full", "code.git_push")
    path, err = _git_repo(body)
    if err:
        return err
    branch = str(body.get("branch") or "").strip()
    if not branch:
        cur = _git(path, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
        branch = (cur.get("stdout") or "").strip() or "HEAD"
    remote = str(body.get("remote") or "origin").strip() or "origin"
    r = _git(path, ["push", remote, branch], timeout=180)
    if not r.get("ok"):
        return _fail(f"git push failed: {(r.get('stderr') or r.get('stdout') or r.get('error') or '')[-2000:]}")
    return _ok(action="code.git_push", path=str(path), remote=remote, branch=branch,
               output=((r.get("stdout") or "") + (r.get("stderr") or ""))[-4000:])


def op_code_run_script(body: dict) -> dict:
    require("full", "code.run_script")
    try:
        path = _code_file(body)
    except ValueError as e:
        return _fail(str(e))
    if not path.exists() or not path.is_file():
        return _fail(f"script not found: {path}")
    args = body.get("args") or []
    if isinstance(args, str):
        args = [a for a in re.split(r"\s+", args.strip()) if a]
    args = [str(a) for a in args][:50]
    timeout = max(1, min(int(body.get("timeout", 30) or 30), 30))
    cwd = str(body.get("cwd") or path.parent)
    suffix = path.suffix.lower()
    if suffix == ".py":
        cmd = [str(body.get("python") or _project_python(Path(cwd))), str(path)] + args
    elif suffix in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")
        if not node:
            return _fail("node executable not found on PATH")
        cmd = [node, str(path)] + args
    elif suffix == ".ps1":
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)] + args
    elif suffix in (".bat", ".cmd"):
        cmd = ["cmd", "/c", str(path)] + args
    else:
        return _fail(f"unsupported script type: {suffix} (use .py, .js, .mjs, .ps1, .bat, .cmd)")
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                              encoding="utf-8", errors="replace")
        return _ok(action="code.run_script", path=str(path), args=args, command=cmd,
                   exit_code=proc.returncode, stdout=(proc.stdout or "")[-20000:],
                   stderr=(proc.stderr or "")[-10000:], timed_out=False,
                   duration_ms=int((time.time() - started) * 1000))
    except subprocess.TimeoutExpired as e:
        return _fail(f"script timed out after {timeout}s", path=str(path),
                     stdout=(e.stdout or "")[-10000:] if isinstance(e.stdout, str) else "",
                     stderr=(e.stderr or "")[-5000:] if isinstance(e.stderr, str) else "",
                     timed_out=True, exit_code=None)
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}", path=str(path))


def op_code_install_packages(body: dict) -> dict:
    require("full", "code.install_packages")
    path = _code_dir(body)
    if not path.exists() or not path.is_dir():
        return _fail(f"directory not found: {path}")
    manager = str(body.get("manager") or body.get("package_manager") or "pip").lower().strip()
    packages = body.get("packages") or []
    if isinstance(packages, str):
        packages = [p.strip() for p in re.split(r"[\s,]+", packages) if p.strip()]
    packages = [str(p) for p in packages][:100]
    bad = [p for p in packages if p.startswith("-")]
    if bad:
        return _fail(f"package names must not start with '-' (got: {', '.join(bad[:5])})")
    timeout = max(10, min(int(body.get("timeout", 300) or 300), 600))
    python = str(body.get("python") or _project_python(path))
    if manager in ("pip", "python"):
        cmd = [python, "-m", "pip", "install"]
        if packages:
            cmd += packages
        elif (path / "requirements.txt").exists():
            cmd += ["-r", str(path / "requirements.txt")]
        else:
            return _fail("packages[] is required (or provide requirements.txt in path)")
    elif manager in ("npm", "node"):
        npm = shutil.which("npm")
        if not npm:
            return _fail("npm executable not found on PATH")
        cmd = [npm, "install"] + packages
    else:
        return _fail(f'unknown manager "{manager}" (use pip or npm)')
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(path),
                              encoding="utf-8", errors="replace")
        return _ok(action="code.install_packages", path=str(path), manager=manager,
                   packages=packages, command=cmd, exit_code=proc.returncode,
                   stdout=(proc.stdout or "")[-20000:], stderr=(proc.stderr or "")[-10000:],
                   duration_ms=int((time.time() - started) * 1000))
    except subprocess.TimeoutExpired:
        return _fail(f"package install timed out after {timeout}s", path=str(path), manager=manager)


PROJECT_TEMPLATES = {
    "python": {
        "src/__init__.py": "",
        "src/main.py": "def main():\n    print(\"Hello from {name}\")\n\n\nif __name__ == \"__main__\":\n    main()\n",
        "tests/test_main.py": "from src.main import main\n\n\ndef test_main(capsys):\n    main()\n    assert \"{name}\" in capsys.readouterr().out\n",
        "requirements.txt": "pytest\n",
        "pyproject.toml": "[project]\nname = \"{slug}\"\nversion = \"0.1.0\"\nrequires-python = \">=3.10\"\n\n[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n",
        "README.md": "# {name}\n\nScaffolded by OTHVM.\n\n## Run\n\n```bash\npython src/main.py\n```\n\n## Test\n\n```bash\npytest\n```\n",
        ".gitignore": "__pycache__/\n.venv/\n*.pyc\n.env\n",
    },
    "node": {
        "src/index.js": "function main() {\n  console.log('Hello from {name}');\n}\n\nmain();\n",
        "package.json": "{\n  \"name\": \"{slug}\",\n  \"version\": \"1.0.0\",\n  \"private\": true,\n  \"type\": \"module\",\n  \"scripts\": { \"start\": \"node src/index.js\", \"test\": \"node --test\" }\n}\n",
        "README.md": "# {name}\n\nScaffolded by OTHVM.\n\n```bash\nnpm start\n```\n",
        ".gitignore": "node_modules/\n.env\n",
    },
    "react": {
        "index.html": "<!doctype html>\n<html lang=\"en\">\n  <head><meta charset=\"UTF-8\" /><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" /><title>{name}</title></head>\n  <body><div id=\"root\"></div><script type=\"module\" src=\"/src/main.jsx\"></script></body>\n</html>\n",
        "src/main.jsx": "import React from 'react';\nimport { createRoot } from 'react-dom/client';\nimport App from './App.jsx';\nimport './styles.css';\n\ncreateRoot(document.getElementById('root')).render(<App />);\n",
        "src/App.jsx": "export default function App() {\n  return <main><h1>{name}</h1><p>Scaffolded by OTHVM.</p></main>;\n}\n",
        "src/styles.css": "body { font-family: system-ui, sans-serif; margin: 2rem; }\n",
        "package.json": "{\n  \"name\": \"{slug}\",\n  \"version\": \"1.0.0\",\n  \"private\": true,\n  \"type\": \"module\",\n  \"scripts\": { \"dev\": \"vite\", \"build\": \"vite build\", \"preview\": \"vite preview\" },\n  \"dependencies\": { \"react\": \"^19.0.0\", \"react-dom\": \"^19.0.0\" },\n  \"devDependencies\": { \"vite\": \"^6.0.0\", \"@vitejs/plugin-react\": \"^4.0.0\" }\n}\n",
        "vite.config.js": "import { defineConfig } from 'vite';\nimport react from '@vitejs/plugin-react';\n\nexport default defineConfig({ plugins: [react()] });\n",
        "README.md": "# {name}\n\nScaffolded by OTHVM.\n\n```bash\nnpm install\nnpm run dev\n```\n",
        ".gitignore": "node_modules/\ndist/\n.env\n",
    },
    "next": {
        "app/layout.js": "export const metadata = { title: '{name}' };\n\nexport default function RootLayout({ children }) {\n  return <html lang=\"en\"><body>{children}</body></html>;\n}\n",
        "app/page.js": "export default function Home() {\n  return <main><h1>{name}</h1><p>Scaffolded by OTHVM.</p></main>;\n}\n",
        "app/globals.css": "body { font-family: system-ui, sans-serif; margin: 2rem; }\n",
        "package.json": "{\n  \"name\": \"{slug}\",\n  \"version\": \"1.0.0\",\n  \"private\": true,\n  \"scripts\": { \"dev\": \"next dev\", \"build\": \"next build\", \"start\": \"next start\" },\n  \"dependencies\": { \"next\": \"^15.0.0\", \"react\": \"^19.0.0\", \"react-dom\": \"^19.0.0\" }\n}\n",
        "next.config.mjs": "export default {};\n",
        "README.md": "# {name}\n\nScaffolded by OTHVM.\n\n```bash\nnpm install\nnpm run dev\n```\n",
        ".gitignore": "node_modules/\n.next/\n.env\n",
    },
}


def op_code_create_project(body: dict) -> dict:
    require("write", "code.create_project")
    base = _code_dir(body)
    name = str(body.get("name") or "").strip()
    kind = str(body.get("type") or "python").lower().strip()
    if not name:
        return _fail("name is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) or ".." in name:
        return _fail("name must be 1-64 chars: letters, digits, dot, underscore, hyphen (no paths)")
    if kind not in PROJECT_TEMPLATES:
        return _fail(f'unknown project type "{kind}" (python | node | react | next)')
    root = base / name
    if root.exists() and any(root.iterdir()) and not body.get("force"):
        return _fail(f"target exists and is not empty: {root} (pass force=true to overwrite template files)")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    files = PROJECT_TEMPLATES[kind]
    written = []
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.replace("{name}", name).replace("{slug}", slug), encoding="utf-8")
        written.append(str(p))
    return _ok(action="code.create_project", path=str(root), name=name, type=kind,
               files_created=written, count=len(written),
               next_steps=(["python -m venv .venv", "pip install -r requirements.txt", "pytest"] if kind == "python"
                           else ["npm install", "npm run dev" if kind in ("react", "next") else "npm start"]))


# --------------------------------------------------------------------------- code sessions (C1)
CS_MAX_BYTES = 5_000_000
CS_MAX_SECTIONS = 200
CS_HISTORY_LIMIT = 40


def _cs_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve())).replace("\\", "/")


def _cs_sid(path: Path) -> str:
    return "cs_" + hashlib.sha1(_cs_key(path).encode("utf-8")).hexdigest()[:16]


def _cs_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cs_decode(raw: bytes) -> tuple[str, str, bool]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig", True
    for enc in ("utf-8", "cp1256", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc, False
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8", False


def _cs_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf and crlf >= lf and crlf >= cr:
        return "\r\n"
    if cr and cr > lf:
        return "\r"
    return "\n"


def _cs_read(path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) > CS_MAX_BYTES:
        raise ValueError(f"file is too large ({len(raw)} bytes, max {CS_MAX_BYTES})")
    if b"\x00" in raw[:8192]:
        raise ValueError("binary file — code sessions only support text files")
    text, encoding, had_bom = _cs_decode(raw)
    newline = _cs_newline(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    had_final = text.endswith("\n")
    if had_final:
        text = text[:-1]
    return {"content": text, "encoding": encoding, "newline": newline,
            "had_final_newline": had_final, "had_bom": had_bom,
            "bytes": len(raw), "hash": _cs_hash(text), "lines": len(text.split("\n")) if text else 0}


def _cs_block_lines(text: str) -> list[str]:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if text == "":
        return []
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def _cs_slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name or "").strip()).strip("-").lower()
    return (s or "section")[:60]


def _cs_make_section(seen: dict, name: str, kind: str, start: int, end: int) -> dict:
    base = _cs_slug(name)
    seen[base] = seen.get(base, 0) + 1
    sid = base if seen[base] == 1 else f"{base}_{seen[base]}"
    return {"id": sid, "name": str(name or base)[:120], "kind": kind,
            "start": int(start), "end": int(end)}


def _cs_header_section(lines: list[str], first: int, seen: dict) -> list[dict]:
    if first <= 1:
        return []
    if not any(line.strip() for line in lines[:first - 1]):
        return []
    return [_cs_make_section(seen, "header", "header", 1, first - 1)]


def _cs_strip_code_line(line: str) -> str:
    line = re.sub(r'"(\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(\\.|[^'\\])*'", "''", line)
    return line.split("//", 1)[0]


def _cs_brace_end(lines: list[str], start: int) -> int:
    depth = 0
    started = False
    for j in range(start, min(len(lines), start + 4000)):
        clean = _cs_strip_code_line(lines[j])
        for ch in clean:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
        if started and depth <= 0:
            return j + 1
        if not started and j == start:
            return start + 1
    return min(len(lines), start + 1)


def _cs_python_sections(text: str, lines: list[str]) -> list[dict] | None:
    try:
        tree = ast.parse(text)
    except Exception:
        return None
    seen: dict[str, int] = {}
    tops = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if not tops:
        return None
    first = min(getattr(n, "lineno", 1) for n in tops)
    for d in getattr(tops[0], "decorator_list", []):
        first = min(first, getattr(d, "lineno", first))
    out = _cs_header_section(lines, first, seen)
    for node in tops:
        start = getattr(node, "lineno", 1)
        for d in getattr(node, "decorator_list", []):
            start = min(start, getattr(d, "lineno", start))
        end = getattr(node, "end_lineno", None) or start
        kind = "class" if isinstance(node, ast.ClassDef) else ("async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
        out.append(_cs_make_section(seen, node.name, kind, start, end))
    return out


def _cs_js_sections(text: str, lines: list[str]) -> list[dict] | None:
    patterns = [
        ("function", re.compile(r"^\s{0,4}(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")),
        ("class", re.compile(r"^\s{0,4}(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s{0,4}(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")),
    ]
    found = []
    for i, line in enumerate(lines):
        for kind, rx in patterns:
            m = rx.match(line)
            if m:
                found.append((i + 1, kind, m.group(1)))
                break
    if not found:
        return None
    seen: dict[str, int] = {}
    out = _cs_header_section(lines, found[0][0], seen)
    for start, kind, name in found:
        s = start
        while s > 1 and start - s < 2 and lines[s - 2].strip().startswith("//"):
            s -= 1
        out.append(_cs_make_section(seen, name, kind, s, _cs_brace_end(lines, start - 1)))
    return out


def _cs_markdown_sections(text: str, lines: list[str]) -> list[dict] | None:
    heads = []
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", line)
        if m:
            heads.append((i + 1, m.group(1), m.group(2).strip()))
    if not heads:
        return None
    seen: dict[str, int] = {}
    out = []
    if heads[0][0] > 1 and any(line.strip() for line in lines[:heads[0][0] - 1]):
        out.append(_cs_make_section(seen, "intro", "markdown", 1, heads[0][0] - 1))
    for idx, (start, hashes, title) in enumerate(heads):
        end = heads[idx + 1][0] - 1 if idx + 1 < len(heads) else len(lines)
        out.append(_cs_make_section(seen, title, "h" + str(len(hashes)), start, end))
    return out


def _cs_key_sections(lines: list[str], rx: re.Pattern, kind: str) -> list[dict] | None:
    keys = [(i + 1, m.group(1)) for i, line in enumerate(lines) if (m := rx.match(line))]
    if not keys:
        return None
    seen: dict[str, int] = {}
    out = _cs_header_section(lines, keys[0][0], seen)
    for idx, (start, name) in enumerate(keys):
        end = keys[idx + 1][0] - 1 if idx + 1 < len(keys) else len(lines)
        out.append(_cs_make_section(seen, name, kind, start, end))
    return out


def _cs_css_sections(lines: list[str]) -> list[dict] | None:
    found = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith(("@", "/*", "*", "}", "//")):
            continue
        if "{" in s:
            found.append((i + 1, s.split("{")[0].strip()[:80]))
    if not found:
        return None
    seen: dict[str, int] = {}
    out = _cs_header_section(lines, found[0][0], seen)
    for start, name in found:
        out.append(_cs_make_section(seen, name, "selector", start, _cs_brace_end(lines, start - 1)))
    return out


def _cs_generic_sections(lines: list[str]) -> list[dict]:
    if not lines:
        return []
    seen: dict[str, int] = {}
    out = []
    start = 0
    n = 0
    for i, line in enumerate(lines):
        size = i - start + 1
        blank = (not line.strip()) and i + 1 < len(lines)
        if size >= 80 or (blank and size >= 18):
            n += 1
            out.append(_cs_make_section(seen, f"block {n}", "block", start + 1, i + 1))
            start = i + 1
    if start < len(lines):
        n += 1
        out.append(_cs_make_section(seen, f"block {n}", "block", start + 1, len(lines)))
    if len(out) == 1:
        out[0]["name"] = "full"
        out[0]["id"] = "full"
    return out


def _cs_sections(text: str, path: Path) -> list[dict]:
    lines = text.split("\n") if text else []
    ext = path.suffix.lower()
    found = None
    if ext == ".py":
        found = _cs_python_sections(text, lines)
    elif ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        found = _cs_js_sections(text, lines)
    elif ext in (".md", ".markdown"):
        found = _cs_markdown_sections(text, lines)
    elif ext == ".json":
        found = _cs_key_sections(lines, re.compile(r'^\s{2}"([^"]+)"\s*:'), "json_key")
    elif ext in (".yaml", ".yml"):
        found = _cs_key_sections(lines, re.compile(r"^([A-Za-z0-9_.-]+)\s*:"), "yaml_key")
    elif ext in (".css", ".scss", ".sass", ".less"):
        found = _cs_css_sections(lines)
    if not found:
        found = _cs_generic_sections(lines)
    return found


def _cs_outline(sections: list[dict]) -> list[dict]:
    return [{"index": i + 1, "id": s["id"], "name": s["name"], "kind": s["kind"],
             "start": s["start"], "end": s["end"], "lines": max(0, s["end"] - s["start"] + 1)}
            for i, s in enumerate(sections)]


def _cs_find_section(sections: list[dict], ref) -> dict | None:
    if ref is None or ref == "":
        return None
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        idx = int(ref) - 1
        return sections[idx] if 0 <= idx < len(sections) else None
    key = str(ref).strip().lower()
    for s in sections:
        if str(s.get("id", "")).lower() == key:
            return s
    for s in sections:
        if str(s.get("name", "")).lower() == key:
            return s
    return None


def _cs_preview(lines: list[str], start: int, end: int, number: bool = True) -> str:
    chunk = lines[max(0, start - 1):max(0, end)]
    if number:
        return "\n".join(f"{start + i}: {line}" for i, line in enumerate(chunk))
    return "\n".join(chunk)


def _cs_project_root(file_path: Path) -> str:
    cur = file_path.parent
    home = Path.home()
    for _ in range(7):
        if cur.parent == cur or cur == home:
            break
        if any((cur / marker).exists() for marker in (".git", "package.json", "pyproject.toml", "pytest.ini",
                                                      "requirements.txt", "setup.py", "tests")):
            return str(cur)
        cur = cur.parent
    return str(file_path.parent)


async def sb_upsert(table: str, rows: list[dict], on_conflict: str, columns: str = "*") -> list[dict]:
    url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/{table}?on_conflict={quote(on_conflict, safe='')}&select={quote(columns, safe='*,' )}"
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(url, headers={**_sb_headers(),
                                       "Prefer": "resolution=merge-duplicates,return=representation"}, json=rows)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase upsert {table} failed ({r.status_code}): {r.text[:600]}")
    try:
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def sb_patch(table: str, query: str, values: dict) -> list[dict]:
    url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/{table}?{query}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.patch(url, headers={**_sb_headers(), "Prefer": "return=representation"}, json=values)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Supabase update {table} failed ({r.status_code}): {r.text[:600]}")
    try:
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def _cs_load(body: dict) -> dict:
    sid = str(body.get("session_id") or "").strip()
    raw_path = body.get("path") or body.get("file")
    path = None
    if raw_path:
        p = Path(str(raw_path)).expanduser()
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        path = p
        if not sid:
            sid = _cs_sid(p)
    if not sid:
        raise ValueError("session_id or path is required (call open_file first)")
    rows = await sb_get("code_sessions", f"session_id=eq.{quote(sid, safe='')}&limit=1")
    if not rows:
        raise LookupError(f'code session not found: {sid}. Call code_session.open_file first.')
    row = rows[0]
    if path and os.path.normcase(str(Path(row["path"]))) != os.path.normcase(str(path.resolve())):
        raise ValueError(f'session path mismatch: session is for {row["path"]}, not {path}')
    return row


async def _cs_save_row(row: dict, backup: bool = True, force: bool = False) -> dict:
    path = Path(row["path"])
    if not path.exists() or not path.is_file():
        return _fail(f"file no longer exists: {path}")
    current = _cs_read(path)
    if not force and row.get("base_hash") and current["hash"] != row["base_hash"]:
        return _fail("file changed on disk since the session opened — reopen or pass force=true to overwrite",
                     conflict=True, disk_hash=current["hash"], base_hash=row["base_hash"])
    backup_path = None
    old_bytes = path.read_bytes()
    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bp = path.with_name(path.name + ".othvm.bak." + stamp)
        try:
            bp.write_bytes(old_bytes)
            backup_path = str(bp)
            olds = sorted(path.parent.glob(path.name + ".othvm.bak.*"))
            for old in olds[:-5]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception as e:
            return _fail(f"could not create backup, save aborted: {e}")
    content = row.get("content") or ""
    newline = row.get("newline") or "\n"
    had_final = bool(row.get("had_final_newline"))
    out_text = content.replace("\n", newline) + (newline if had_final else "")
    encoding = row.get("encoding") or "utf-8"
    try:
        data = out_text.encode(encoding)
    except Exception:
        encoding = "utf-8"
        data = out_text.encode("utf-8")
    try:
        path.write_bytes(data)
    except Exception as e:
        if backup_path:
            try:
                Path(backup_path).write_bytes(old_bytes)
            except Exception:
                pass
        return _fail(f"write failed: {type(e).__name__}: {e}")
    new_hash = _cs_hash(content)
    now = datetime.now(timezone.utc).isoformat()
    history = list(row.get("history") or [])
    history.append({"ts": now, "op": "save", "path": str(path), "backup": backup_path,
                    "bytes": len(data), "hash": new_hash})
    await sb_patch("code_sessions", f"session_id=eq.{quote(row['session_id'], safe='')}",
                   {"dirty": False, "base_hash": new_hash, "file_hash": new_hash,
                    "saved_at": now, "history": history[-CS_HISTORY_LIMIT:]})
    return _ok(action="code_session.save_file", session_id=row["session_id"], path=str(path),
               saved=True, bytes_written=len(data), lines=len(content.split("\n")) if content else 0,
               hash=new_hash, encoding=encoding, newline="CRLF" if newline == "\r\n" else ("CR" if newline == "\r" else "LF"),
               backup=backup_path)


async def op_cs_open_file(body: dict) -> dict:
    require("read", "code_session.open_file")
    try:
        path = _code_file(body)
    except ValueError as e:
        return _fail(str(e))
    if not path.exists() or not path.is_file():
        return _fail(f"file not found: {path}")
    try:
        disk = _cs_read(path)
    except ValueError as e:
        return _fail(str(e))
    key = _cs_key(path)
    sid = str(body.get("session_id") or "").strip() or _cs_sid(path)
    existing_rows = await sb_get("code_sessions", f"session_id=eq.{quote(sid, safe='')}&limit=1")
    existing = existing_rows[0] if existing_rows else None
    warning = None
    resumed = False
    dirty = False
    if existing:
        if existing.get("path_key") and existing["path_key"] != key:
            return _fail(f"session_id {sid} belongs to another file: {existing['path']}")
        resumed = True
        if existing.get("dirty") and not body.get("force_reload"):
            content = existing.get("content") or ""
            base_hash = existing.get("base_hash")
            dirty = True
            if base_hash and base_hash != disk["hash"]:
                warning = "unsaved session edits exist AND the file changed on disk; save/reopen carefully"
            else:
                warning = "resumed a session with unsaved edits (save_file to write them)"
            sections = existing.get("sections") or _cs_sections(content, path)
            encoding = existing.get("encoding") or disk["encoding"]
            newline = existing.get("newline") or disk["newline"]
            had_final = bool(existing.get("had_final_newline"))
            base_hash = base_hash or disk["hash"]
            file_hash = existing.get("file_hash") or _cs_hash(content)
        else:
            content = disk["content"]
            sections = _cs_sections(content, path)
            encoding, newline, had_final = disk["encoding"], disk["newline"], disk["had_final_newline"]
            base_hash = file_hash = disk["hash"]
    else:
        content = disk["content"]
        sections = _cs_sections(content, path)
        encoding, newline, had_final = disk["encoding"], disk["newline"], disk["had_final_newline"]
        base_hash = file_hash = disk["hash"]
    all_sections = sections
    sections = sections[:CS_MAX_SECTIONS]
    project_path = str(body.get("project_path") or (existing or {}).get("project_path") or _cs_project_root(path))
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "session_id": sid, "path": str(path), "path_key": key, "project_path": project_path,
        "content": content, "base_hash": base_hash, "file_hash": file_hash,
        "encoding": encoding, "newline": newline, "had_final_newline": had_final,
        "sections": sections, "dirty": dirty,
        "edits_count": int((existing or {}).get("edits_count") or 0),
        "history": list((existing or {}).get("history") or [])[-CS_HISTORY_LIMIT:],
    }
    try:
        await sb_upsert("code_sessions", [row], "session_id")
    except Exception as e:
        return _fail(f"could not persist code session: {e}")
    preview_lines = max(1, min(int(body.get("preview_lines", 40) or 40), 200))
    lines = content.split("\n") if content else []
    result = _ok(action="code_session.open_file", session_id=sid, path=str(path),
                 project_path=project_path, total_lines=len(lines), bytes=len(content.encode("utf-8")),
                 encoding=encoding, newline="CRLF" if newline == "\r\n" else ("CR" if newline == "\r" else "LF"),
                 sections=_cs_outline(sections), sections_count=len(all_sections),
                 sections_truncated=len(all_sections) > len(sections),
                 preview=_cs_preview(lines, 1, min(preview_lines, len(lines) or 1)),
                 dirty=dirty, resumed=resumed, base_hash=base_hash)
    if warning:
        result["warning"] = warning
    return result


async def op_cs_read_section(body: dict) -> dict:
    require("read", "code_session.read_section")
    try:
        row = await _cs_load(body)
    except (ValueError, LookupError) as e:
        return _fail(str(e))
    sections = row.get("sections") or []
    lines = (row.get("content") or "").split("\n") if row.get("content") else []
    start = body.get("start_line")
    end = body.get("end_line")
    ref = body.get("section", body.get("section_id", body.get("name")))
    section = None
    if start is None and ref is not None:
        section = _cs_find_section(sections, ref)
        if not section:
            return _fail(f'section not found: {ref}', available=_cs_outline(sections), total_lines=len(lines))
        start, end = section["start"], section["end"]
    if start is None:
        return _ok(action="code_session.read_section", session_id=row["session_id"], path=row["path"],
                   total_lines=len(lines), dirty=bool(row.get("dirty")),
                   sections=_cs_outline(sections), content=None,
                   hint="pass section (name/id/index) or start_line/end_line to read content")
    try:
        start = max(1, int(start))
        end = int(end) if end is not None else start
    except Exception:
        return _fail("start_line/end_line must be numbers")
    end = min(end, len(lines))
    if end < start:
        return _fail(f"invalid range: {start}-{end}")
    if end - start + 1 > 500:
        end = start + 499
    content = _cs_preview(lines, start, end)
    try:
        await sb_patch("code_sessions", f"session_id=eq.{quote(row['session_id'], safe='')}",
                       {"cursor_start": start, "cursor_end": end})
    except Exception:
        pass
    return _ok(action="code_session.read_section", session_id=row["session_id"], path=row["path"],
               section=section, start_line=start, end_line=end, lines_read=end - start + 1,
               total_lines=len(lines), content=content, dirty=bool(row.get("dirty")),
               file_hash=row.get("file_hash"),
               sections=_cs_outline(sections) if body.get("include_outline") else None)


async def op_cs_edit_section(body: dict) -> dict:
    require("write", "code_session.edit_section")
    try:
        row = await _cs_load(body)
    except (ValueError, LookupError) as e:
        return _fail(str(e))
    if body.get("expected_hash") and body["expected_hash"] != row.get("file_hash"):
        return _fail("hash mismatch — the working copy changed; re-read the section then retry",
                     conflict=True, current_hash=row.get("file_hash"))
    sections = row.get("sections") or []
    content = row.get("content") or ""
    lines = content.split("\n") if content else []
    mode = str(body.get("mode") or "replace").lower().strip()
    if mode not in ("replace", "delete", "insert_before", "insert_after"):
        return _fail(f'unknown edit mode "{mode}" (replace | delete | insert_before | insert_after)')
    ref = body.get("section", body.get("section_id", body.get("name")))
    section = None
    start = body.get("start_line")
    end = body.get("end_line")
    if start is None and ref is not None:
        section = _cs_find_section(sections, ref)
        if not section:
            return _fail(f'section not found: {ref}', available=_cs_outline(sections))
        start, end = section["start"], section["end"]
    if start is None:
        if mode == "insert_after" and body.get("append"):
            start = end = len(lines)
        else:
            return _fail("section (name/id/index) or start_line/end_line is required")
    try:
        start = max(1, int(start))
        end = max(start, int(end)) if end is not None else start
    except Exception:
        return _fail("start_line/end_line must be numbers")
    if start > len(lines) + 1:
        return _fail(f"start_line {start} is beyond the file ({len(lines)} lines)")
    end = min(end, len(lines))
    block = _cs_block_lines(body.get("content", ""))
    if mode == "delete":
        block = []
    if mode != "delete" and not block:
        return _fail("content is required for this edit mode")
    if mode == "insert_before":
        before, after = lines[:start - 1], lines[start - 1:]
    elif mode == "insert_after":
        before, after = lines[:end], lines[end:]
    else:
        before, after = lines[:start - 1], lines[end:]
    new_lines = before + block + after
    new_content = "\n".join(new_lines)
    if new_content == content:
        return _ok(action="code_session.edit_section", session_id=row["session_id"], path=row["path"],
                   changed=False, dirty=bool(row.get("dirty")), message="edit produced no change")
    old_hash = row.get("file_hash")
    new_hash = _cs_hash(new_content)
    new_sections = _cs_sections(new_content, Path(row["path"]))[:CS_MAX_SECTIONS]
    now = datetime.now(timezone.utc).isoformat()
    history = list(row.get("history") or [])
    history.append({"ts": now, "op": "edit", "mode": mode, "section": (section or {}).get("id"),
                    "start": start, "end": end, "old_hash": old_hash, "new_hash": new_hash,
                    "chars_before": len(content), "chars_after": len(new_content)})
    cursor_end = start + len(block) - 1 if block else start
    try:
        await sb_patch("code_sessions", f"session_id=eq.{quote(row['session_id'], safe='')}",
                       {"content": new_content, "file_hash": new_hash, "sections": new_sections,
                        "dirty": True, "edits_count": int(row.get("edits_count") or 0) + 1,
                        "history": history[-CS_HISTORY_LIMIT:], "cursor_start": start,
                        "cursor_end": max(start, cursor_end)})
    except Exception as e:
        return _fail(f"could not persist edit: {e}")
    diff = list(difflib.unified_diff(content.split("\n"), new_content.split("\n"),
                                     fromfile="a/" + Path(row["path"]).name,
                                     tofile="b/" + Path(row["path"]).name, lineterm=""))
    max_diff = max(2000, min(int(body.get("max_diff_chars", 20000) or 20000), 100000))
    diff_text = "\n".join(diff)
    return _ok(action="code_session.edit_section", session_id=row["session_id"], path=row["path"],
               changed=True, dirty=True, saved=False, mode=mode, section=section,
               start_line=start, end_line=end, new_lines=len(block),
               total_lines=len(new_lines), old_hash=old_hash, new_hash=new_hash,
               diff=diff_text[:max_diff], diff_truncated=len(diff_text) > max_diff,
               sections=_cs_outline(new_sections))


async def op_cs_save_file(body: dict) -> dict:
    require("write", "code_session.save_file")
    try:
        row = await _cs_load(body)
    except (ValueError, LookupError) as e:
        return _fail(str(e))
    if body.get("path"):
        try:
            p = _code_file(body)
        except ValueError as e:
            return _fail(str(e))
        if _cs_key(p) != _cs_key(Path(row["path"])):
            return _fail(f'session path mismatch: session is for {row["path"]}, not {p}')
    if not row.get("dirty") and not body.get("force"):
        return _ok(action="code_session.save_file", session_id=row["session_id"], path=row["path"],
                   saved=False, dirty=False, message="no unsaved changes")
    return await _cs_save_row(row, backup=bool(body.get("backup", True)), force=bool(body.get("force")))


def _cs_runner(project: Path, body: dict) -> tuple[str, str | None, list[str]]:
    explicit = str(body.get("runner") or "").lower().strip()
    if explicit:
        return explicit, None, []
    pkg = project / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8-sig"))
            if (data.get("scripts") or {}).get("test"):
                return "npm", str(pkg), []
        except Exception:
            pass
    py = _project_python(project)
    try:
        probe = subprocess.run([py, "-c", "import pytest"], capture_output=True, timeout=15)
        if probe.returncode == 0:
            return "pytest", None, []
    except Exception:
        pass
    if (project / "tests").is_dir() or list(project.glob("test_*.py")) or list(project.glob("*_test.py")):
        return "unittest", None, []
    return "none", None, []


async def op_cs_run_tests(body: dict) -> dict:
    require("full", "code_session.run_tests")
    row = None
    try:
        row = await _cs_load(body)
    except LookupError:
        if str(body.get("session_id") or "").strip():
            return _fail(f'code session not found: {body.get("session_id")}. Call code_session.open_file first.')
        row = None
    except ValueError as e:
        return _fail(str(e))
    if row and row.get("dirty") and body.get("save_first"):
        saved = await _cs_save_row(row, backup=bool(body.get("backup", True)))
        if not saved.get("ok"):
            return saved
        row = (await sb_get("code_sessions", f"session_id=eq.{quote(row['session_id'], safe='')}&limit=1"))[0]
    if body.get("project_path"):
        project = Path(str(body["project_path"])).expanduser()
        if not project.is_absolute():
            project = (BASE_DIR / project).resolve()
    elif row:
        project = Path(row.get("project_path") or Path(row["path"]).parent)
    elif body.get("path"):
        p = Path(str(body["path"])).expanduser()
        project = p if p.is_absolute() else (BASE_DIR / p).resolve()
        if project.is_file():
            project = project.parent
    else:
        return _fail("project_path, path, or session_id is required")
    if not project.exists() or not project.is_dir():
        return _fail(f"project directory not found: {project}")
    runner, _pkg, _ = _cs_runner(project, body)
    if runner == "none":
        return _fail("could not detect a test runner (no pytest, unittest tests, or npm test script). "
                     "Pass runner=explicit and test_path.")
    if runner not in ("pytest", "unittest", "npm", "node", "python"):
        return _fail(f'unknown runner "{runner}" (pytest | unittest | npm | node | python)')
    py = _project_python(project)
    npm = shutil.which("npm")
    node = shutil.which("node")
    test_path = str(body.get("test_path") or "").strip()
    args = body.get("args") or []
    if isinstance(args, str):
        args = [a for a in re.split(r"\s+", args.strip()) if a]
    args = [str(a) for a in args][:30]
    if runner == "pytest":
        cmd = [py, "-m", "pytest", "-q"] + args
        if test_path:
            cmd.append(test_path)
    elif runner == "unittest":
        tests_dir = str(project / "tests") if (project / "tests").is_dir() else str(project)
        cmd = [py, "-m", "unittest", "discover", "-s", tests_dir, "-v"] + args
    elif runner == "npm":
        if not npm:
            return _fail("npm executable not found on PATH")
        cmd = [npm, "test"] + (["--"] if args else []) + args
    elif runner == "node":
        if not node:
            return _fail("node executable not found on PATH")
        cmd = [node, "--test"] + ([test_path] if test_path else []) + args
    else:
        if not test_path:
            return _fail("runner=python requires test_path")
        cmd = [py, test_path] + args
    timeout = max(5, min(int(body.get("timeout", 120) or 120), 300))
    started = time.time()
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    timed_out = False
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(project),
                              encoding="utf-8", errors="replace", env=env)
        stdout, stderr, exit_code = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = e.stdout if isinstance(e.stdout, str) else ""
        stderr = e.stderr if isinstance(e.stderr, str) else ""
        exit_code = None
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}", project=str(project), runner=runner)
    out = stdout + "\n" + stderr
    def _num(label: str) -> int | None:
        m = re.search(r"(\d+)\s+" + label, out)
        return int(m.group(1)) if m else None
    summary: dict = {"passed": _num("passed"), "failed": _num("failed"), "errors": _num("errors"),
                     "skipped": _num("skipped")}
    # unittest / node:test report a total plus failure counters instead of "N passed".
    m_ran = re.search(r"Ran\s+(\d+)\s+tests?", out)
    if m_ran:
        total = int(m_ran.group(1))
        def _grp(rx: str) -> int:
            mm = re.search(rx, out)
            return int(mm.group(1)) if mm else 0
        fails = _grp(r"failures=(\d+)")
        errs = _grp(r"errors=(\d+)")
        skipped = _grp(r"skipped=(\d+)")
        summary.update({"total": total, "failed": fails, "errors": errs,
                        "skipped": skipped, "passed": max(0, total - fails - errs - skipped)})
    else:
        m_pass = re.search(r"#\s*pass\s+(\d+)", out)
        m_fail = re.search(r"#\s*fail\s+(\d+)", out)
        if m_pass:
            summary.update({"passed": int(m_pass.group(1)),
                            "failed": int(m_fail.group(1)) if m_fail else 0})
    summary = {k: v for k, v in summary.items() if v is not None}
    duration = int((time.time() - started) * 1000)
    result = _ok(action="code_session.run_tests", session_id=(row or {}).get("session_id"),
                 project=str(project), runner=runner, command=cmd, exit_code=exit_code,
                 timed_out=timed_out, duration_ms=duration, summary=summary,
                 stdout=stdout[-20000:], stderr=stderr[-10000:],
                 dirty=bool((row or {}).get("dirty")), save_first=bool(body.get("save_first")))
    if row and row.get("dirty") and not body.get("save_first"):
        result["warning"] = "tests ran against the saved file — unsaved session edits were NOT included (use save_first=true)"
    try:
        if row:
            await sb_patch("code_sessions", f"session_id=eq.{quote(row['session_id'], safe='')}",
                           {"last_test": {k: result.get(k) for k in ("runner", "command", "exit_code", "timed_out",
                                                                      "duration_ms", "summary", "dirty")}})
    except Exception:
        pass
    return result


# --------------------------------------------------------------------------- vision (C2)
VISION_MAX_BYTES = 8_000_000
VISION_MIMES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


def _vision_b64(value: str) -> bytes:
    s = re.sub(r"^data:[^;]+;base64,", "", str(value or "").strip())
    s = re.sub(r"\s+", "", s)
    if not s:
        raise ValueError("image_base64 is empty")
    pad = (-len(s)) % 4
    return base64.b64decode(s + "=" * pad, validate=False)


def _vision_mime(raw: bytes, hint: str = "") -> str:
    hint = str(hint or "").lower().strip()
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[4:8] == b"ftyp" and raw[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
        return "image/heic"
    if hint.startswith("image/"):
        return hint
    return "image/jpeg"


def _vision_ext(mime: str) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "image/heic": ".heic", "image/heif": ".heif", "image/gif": ".gif"}.get(mime, ".img")


async def _vision_fetch(body: dict) -> tuple[bytes, str, dict]:
    meta: dict = {"source": None}
    raw_b64 = body.get("image_base64") or body.get("base64") or body.get("data")
    if raw_b64:
        raw = _vision_b64(raw_b64)
        meta["source"] = "base64"
        return raw, _vision_mime(raw, body.get("mime_type") or ""), meta
    raw_path = body.get("image_path") or body.get("path") or body.get("file_path")
    if raw_path:
        p = Path(str(raw_path)).expanduser()
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        if not p.exists() or not p.is_file():
            raise ValueError(f"image not found: {p}")
        raw = p.read_bytes()
        meta.update({"source": "path", "path": str(p)})
        return raw, _vision_mime(raw, body.get("mime_type") or ""), meta
    raw_url = body.get("image_url") or body.get("url")
    if raw_url:
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as c:
            r = await c.get(str(raw_url))
        if r.status_code != 200:
            raise ValueError(f"image download failed ({r.status_code})")
        raw = r.content
        meta.update({"source": "url", "url": str(raw_url)})
        return raw, _vision_mime(raw, r.headers.get("content-type") or body.get("mime_type") or ""), meta
    file_id = str(body.get("file_id") or body.get("telegram_file_id") or "").strip()
    if file_id:
        token = str(CFG.get("telegram_bot_token") or "").strip()
        if not token:
            raise ValueError("telegram_bot_token is not configured")
        data = {}
        last_err = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=60) as c:
                    r = await c.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id})
                data = r.json() if r.status_code == 200 else {}
                if data.get("ok") and str((data.get("result") or {}).get("file_path") or ""):
                    break
                last_err = data.get("description") or r.text[:300]
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1.5)
        if not (data.get("ok") and (data.get("result") or {}).get("file_path")):
            raise ValueError(f"Telegram getFile failed: {last_err or 'no file_path'}")
        tg_path = str((data.get("result") or {}).get("file_path") or "")
        r2 = None
        last_err = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
                    r2 = await c.get(f"https://api.telegram.org/file/bot{token}/{tg_path}")
                if r2.status_code == 200:
                    break
                last_err = f"HTTP {r2.status_code}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1.0)
        if r2 is None or r2.status_code != 200:
            raise ValueError(f"Telegram file download failed ({last_err or 'unknown'})")
        raw = r2.content
        meta.update({"source": "telegram", "file_id": file_id, "telegram_file_path": tg_path})
        return raw, _vision_mime(raw, body.get("mime_type") or ""), meta
    raise ValueError("provide one of: file_id, image_path, image_url, image_base64")


async def op_vision_analyze(body: dict) -> dict:
    require("read", "vision.analyze")
    key = str(CFG.get("gemini_api_key") or "").strip()
    if not key:
        return _fail("gemini_api_key is not configured in mcp_server/config.json")
    try:
        raw, mime, meta = await _vision_fetch(body)
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")
    if not raw:
        return _fail("empty image")
    if len(raw) > VISION_MAX_BYTES:
        return _fail(f"image is too large ({len(raw)} bytes, max {VISION_MAX_BYTES}) — send a smaller photo")
    if mime == "image/gif":
        return _fail("GIF is not supported by Gemini Vision — send a PNG or JPEG")
    if mime not in VISION_MIMES:
        return _fail(f'unsupported image type "{mime}" (jpeg | png | webp | heic | heif)')
    prompt = str(body.get("prompt") or body.get("founder_text") or body.get("message")
                 or body.get("caption") or body.get("text") or "").strip()
    if not prompt:
        prompt = ("حلّل الصورة دي بالتفصيل بالعربي. اذكر كل النصوص والأرقام والأشخاص والأماكن والأشياء المهمة، "
                  "ولو فيها سؤال أو مهمة قول لي الخلاصة في أول سطر.")
    model = str(body.get("model") or CFG.get("vision_model") or CFG.get("summary_model") or "gemini-3.8-flash")
    max_tokens = max(200, min(int(body.get("max_output_tokens", 2500) or 2500), 8000))
    system = ("أنت مساعد OTHVM الشخصي. بتشوف الصور اللي المستخدم بيبعتها على تلجرام وترد عليه "
              "بالعربي المصري الواضح. رد على سؤال المستخدم اللي في النص لو موجود، واستخرج أي نصوص أو أرقام أو تفاصيل "
              "مهمة من الصورة. لو الصورة مش واضحة أو مش ليها علاقة، قول ده بصراحة. متخمّنش حاجة مش ظاهرة.")
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [
            {"text": prompt[:8000]},
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
    }
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await _gemini_post(
                c,
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": key}, json_body=payload,
            )
    except Exception as e:
        return _fail(f"Gemini request failed: {type(e).__name__}: {e}")
    if r.status_code != 200:
        return _fail(f"Gemini Vision failed ({r.status_code}): {r.text[:800]}", model=model)
    try:
        data = r.json()
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(str(p.get("text", "")) for p in parts).strip()
        finish = cand.get("finishReason")
    except Exception as e:
        return _fail(f"Gemini returned an unexpected response: {e}")
    if not text:
        return _fail(f"Gemini returned no text (finishReason={finish})", model=model)
    saved_path = None
    if body.get("save_temp", True):
        try:
            suffix = _vision_ext(mime)
            fp = TEMP_DIR / f"vision_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{suffix}"
            fp.write_bytes(raw)
            saved_path = str(fp)
        except Exception:
            pass
    usage = data.get("usageMetadata") or {}
    return _ok(action="vision.analyze", text=text, model=model, mime_type=mime,
               bytes=len(raw), source=meta.get("source"), file_id=meta.get("file_id"),
               telegram_file_path=meta.get("telegram_file_path"), image_path=meta.get("path"),
               image_url=meta.get("url"), saved_path=saved_path, finish_reason=finish,
               duration_ms=int((time.time() - started) * 1000),
               tokens={k: usage.get(k) for k in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")
                       if usage.get(k) is not None})


OCR_PROMPT = (
    "استخرج كل النص الظاهر في الصورة حرفيًا، من غير ترجمة ومن غير تعليق. "
    "حافظ على ترتيب السطور والجداول. لو مفيش نص واضح، اكتب: (لا يوجد نص)."
)


async def _ocr_async(body: dict) -> dict:
    req = {
        "prompt": str(body.get("prompt") or OCR_PROMPT),
        "max_output_tokens": body.get("max_output_tokens", 4000),
        "save_temp": body.get("save_temp", False),
    }
    for key in ("image_path", "path", "file_path", "image_url", "url", "image_base64",
                "base64", "file_id", "telegram_file_id", "mime_type", "model"):
        if body.get(key) is not None:
            req[key] = body[key]
    if _truthy(body.get("screen")):
        shot = await _proxy_to_desktop("system", {"action": "screenshot"}, 120)
        if not shot.get("ok"):
            return shot
        req["image_path"] = shot.get("path")
    if not any(req.get(k) for k in ("image_path", "image_url", "image_base64", "file_id")):
        return _fail("provide screen=true, image_path, image_url, image_base64, or file_id")
    result = await op_vision_analyze(req)
    if not result.get("ok"):
        return result
    return _ok(action="ocr", text=result.get("text"), model=result.get("model"),
               source=result.get("source"), image_path=result.get("image_path"),
               image_url=result.get("image_url"), tokens=result.get("tokens"))


def op_ocr(body: dict) -> dict:
    require("read", "ocr")
    try:
        return asyncio.run(_ocr_async(body))
    except RuntimeError as e:
        return _fail(f"ocr failed: {e}")


# --------------------------------------------------------------------------- documents (C3)
DOC_MAX_CHARS = 300_000
DOC_FONT_STACK = '"Segoe UI", "Tahoma", "Arial", "Noto Naskh Arabic", sans-serif'


def _doc_safe_name(body: dict, ext: str) -> str:
    raw = str(body.get("filename") or body.get("file_name") or body.get("title") or "document").strip()
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" .-_")
    if not raw:
        raw = "document"
    if len(raw) > 90:
        raw = raw[:90].rstrip(" .-_")
    if not raw.lower().endswith(ext):
        raw += ext
    return raw


def _doc_dir(body: dict) -> Path:
    raw = str(body.get("output_dir") or body.get("directory") or CFG.get("exports_dir")
              or (BASE_DIR.parent / "exports")).strip()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (BASE_DIR / p).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _doc_is_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text or ""))


def _doc_markdown(content: str) -> str:
    try:
        import markdown as mdlib
        return mdlib.markdown(
            content,
            extensions=["tables", "fenced_code", "sane_lists", "nl2br", "attr_list"],
            output_format="html5",
        )
    except Exception:
        # Minimal fallback: keep the text readable even if the markdown package is unavailable.
        esc = html_escape(content)
        paras = re.split(r"\n\s*\n", esc)
        return "\n".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paras if p.strip())


def _doc_html(body: dict, content: str) -> tuple[str, str]:
    title = str(body.get("title") or "").strip()
    fmt = str(body.get("format") or body.get("content_type") or "markdown").strip().lower()
    if fmt in ("md", "markdown", "text", "txt"):
        body_html = _doc_markdown(content)
    elif fmt in ("html", "htm"):
        body_html = content
    else:
        return "", f'unsupported content format "{fmt}" (markdown | html)'
    if title and fmt != "html" and not re.match(r"^\s*#\s+", content):
        body_html = f"<h1>{html_escape(title)}</h1>\n" + body_html
    if title and fmt == "html" and "<h1" not in body_html.lower():
        body_html = f"<h1>{html_escape(title)}</h1>\n" + body_html
    rtl_opt = str(body.get("rtl", "auto")).strip().lower()
    rtl = _doc_is_arabic(content + " " + title) if rtl_opt in ("", "auto", "true", "1", "yes") else rtl_opt in ("true", "1", "yes")
    if rtl_opt in ("false", "0", "no", "ltr"):
        rtl = False
    lang = str(body.get("lang") or ("ar" if rtl else "en")).strip() or ("ar" if rtl else "en")
    direction = "rtl" if rtl else "ltr"
    align = "right" if rtl else "left"
    page_size = str(body.get("page_size") or "A4").strip()
    if page_size.lower() not in ("a4", "letter", "legal", "a3", "a5"):
        page_size = "A4"
    orientation = str(body.get("orientation") or "portrait").strip().lower()
    if orientation not in ("portrait", "landscape"):
        orientation = "portrait"
    margin = str(body.get("margin") or "16mm").strip()
    if not re.match(r"^\d+(\.\d+)?(mm|cm|in|pt|px)$", margin):
        margin = "16mm"
    page_break = '<div class="page-break"></div>'
    body_html = body_html.replace("<!-- pagebreak -->", page_break).replace("<!-- page-break -->", page_break)
    doc = f"""<!DOCTYPE html>
<html lang="{html_escape(lang)}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_escape(title or "Document")}</title>
<style>
  @page {{ size: {page_size} {orientation}; margin: {margin}; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{ font-family: {DOC_FONT_STACK}; font-size: 11.5pt; line-height: 1.75; color: #1f2937; direction: {direction}; text-align: {align}; }}
  h1 {{ font-size: 22pt; line-height: 1.3; color: #0f172a; margin: 0 0 14px; padding-bottom: 7px; border-bottom: 2.5px solid #2563eb; }}
  h2 {{ font-size: 16pt; color: #1d4ed8; margin: 22px 0 8px; }}
  h3 {{ font-size: 13pt; color: #1e3a8a; margin: 16px 0 6px; }}
  h4 {{ font-size: 11.5pt; color: #334155; margin: 12px 0 4px; }}
  p {{ margin: 0 0 9px; }}
  ul, ol {{ margin: 4px 0 10px; padding-inline-start: 22px; }}
  li {{ margin: 2px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0 16px; font-size: 10pt; page-break-inside: avoid; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 6px 8px; vertical-align: top; }}
  th {{ background: #eff6ff; color: #1e3a8a; font-weight: 700; }}
  tr:nth-child(even) td {{ background: #f8fafc; }}
  blockquote {{ margin: 10px 0; padding: 8px 12px; border-inline-start: 4px solid #93c5fd; background: #f8fafc; color: #334155; }}
  code {{ font-family: "Consolas", "Courier New", monospace; font-size: 9.5pt; background: #f1f5f9; color: #be123c; padding: 1px 4px; border-radius: 3px; }}
  pre {{ background: #0f172a; color: #e2e8f0; padding: 10px 12px; border-radius: 6px; white-space: pre-wrap; word-break: break-word; page-break-inside: avoid; }}
  pre code {{ background: transparent; color: inherit; padding: 0; }}
  a {{ color: #1d4ed8; text-decoration: none; }}
  hr {{ border: 0; border-top: 1px solid #cbd5e1; margin: 18px 0; }}
  img {{ max-width: 100%; height: auto; }}
  .page-break {{ page-break-after: always; }}
  .avoid-break {{ page-break-inside: avoid; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""
    return doc, ""


def html_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def op_doc_prepare_export_dir(body: dict) -> dict:
    require("read", "doc.prepare_export_dir")
    try:
        p = _doc_dir(body)
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")
    return _ok(action="doc.prepare_export_dir", dir=str(p))


def op_doc_create_pdf(body: dict) -> dict:
    require("read", "doc.create_pdf")
    content = body.get("content")
    if content is None:
        content = body.get("markdown") or body.get("html") or body.get("text")
    content = str(content or "")
    if not content.strip():
        return _fail("content is required (Markdown or HTML)")
    if len(content) > DOC_MAX_CHARS:
        return _fail(f"content is too large ({len(content)} chars, max {DOC_MAX_CHARS})")
    doc_html, err = _doc_html(body, content)
    if err:
        return _fail(err)
    title = str(body.get("title") or "").strip()
    out_dir = _doc_dir(body)
    name = _doc_safe_name(body, ".pdf")
    out = out_dir / name
    if out.exists() and not body.get("overwrite"):
        out = out_dir / f"{out.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    html_path = TEMP_DIR / f"doc_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.html"
    profile = TEMP_DIR / f"chrome_pdf_{uuid.uuid4().hex[:10]}"
    html_path.write_text(doc_html, encoding="utf-8")
    chrome = str(CFG.get("chrome_path") or r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not Path(chrome).exists():
        chrome = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium") or chrome
    if not Path(chrome).exists():
        return _fail(f'Chrome not found at "{chrome}" — set "chrome_path" in mcp_server/config.json')
    started = time.time()
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--disable-background-networking", "--disable-sync", "--mute-audio",
        f"--user-data-dir={profile}", "--virtual-time-budget=5000",
        "--no-pdf-header-footer", f"--print-to-pdf={out}", html_path.as_uri(),
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            _, stderr = proc.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return _fail("Chrome timed out while creating the PDF (90s)")
        err_text = (stderr or b"").decode("utf-8", "replace")
    except Exception as e:
        return _fail(f"Chrome failed to start: {type(e).__name__}: {e}")
    finally:
        try:
            html_path.unlink(missing_ok=True)
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
    if not out.exists() or out.stat().st_size < 1000:
        return _fail(f"PDF was not created (Chrome exit {proc.returncode}): {err_text[-1200:]}")
    pages = None
    try:
        from pypdf import PdfReader
        pages = len(PdfReader(str(out)).pages)
    except Exception:
        pass
    stat = out.stat()
    return _ok(action="doc.create_pdf", file_path=str(out), file_name=out.name,
               bytes=stat.st_size, pages=pages, title=title or None,
               direction="rtl" if 'dir="rtl"' in doc_html else "ltr",
               engine="chrome-headless", duration_ms=int((time.time() - started) * 1000))


# --------------------------------------------------------------------------- registry
# =========================================================================== comprehensive Windows control (Stage 6)
# Everything below is exposed through the same /tools/{category} contract.
# Read-only actions require "read"; state changes require "write"; destructive
# or security-sensitive actions require "full" and an explicit confirm=true.

CATALOG = {
    "system": [
        "info", "catalog", "battery", "uptime", "disk", "memory", "cpu", "gpu",
        "screenshot", "screen_record", "get_processes", "process_start", "process_details",
        "kill_process", "system_command", "shutdown", "restart", "sleep", "hibernate",
        "lock", "cancel_shutdown", "clipboard_get", "clipboard_set", "volume_get",
        "volume_set", "mute", "brightness_get", "brightness_set", "service_list",
        "service_control", "task_list", "task_create", "task_delete", "task_run",
        "task_end", "audit_log", "ocr", "event_log", "installed_apps", "notification",
        "sessions", "users", "windows_updates", "wsl_list", "wsl_run", "docker_list",
        "docker_control", "emergency_stop", "stop_clear",
    ],
    "files": [
        "file_read", "file_write", "file_append", "file_delete", "file_move", "file_copy",
        "file_list", "file_search", "folder_create", "folder_delete", "trash_list",
        "restore", "undo", "trash_empty", "hash", "zip", "unzip", "permissions", "owner",
        "search_content", "duplicates", "large_files", "share_list", "share_create",
    ],
    "apps": [
        "open_application", "close_application", "focus_window", "list_windows",
        "minimize_window", "maximize_window", "installed", "uninstall", "default_get",
        "launch_as_admin",
    ],
    "windows": [
        "list", "focus", "minimize", "maximize", "restore", "move", "resize", "close",
        "always_on_top", "snap",
    ],
    "input": [
        "keyboard_type", "keyboard_shortcut", "keyboard_press", "mouse_click",
        "mouse_move", "mouse_scroll", "mouse_drag", "mouse_down", "mouse_up", "media_key",
    ],
    "browser": [
        "browser_open", "browser_navigate", "browser_click", "browser_type",
        "browser_extract", "browser_screenshot", "browser_execute_js", "browser_scroll",
        "browser_close", "browser_get_url", "browser_get_title", "fill_form",
        "extract_table", "monitor_page", "login_and_action",
    ],
    "network": [
        "interfaces", "ip", "wifi_status", "wifi_list", "wifi_connect", "wifi_disconnect",
        "ping", "dns", "public_ip", "ports", "connections", "vpn_list", "proxy_get",
        "proxy_set", "hosts_read", "hosts_write", "firewall_status", "firewall_rules",
        "firewall_add", "firewall_remove",
    ],
    "display": [
        "monitors", "resolution_get", "resolution_set", "refresh_rate_set",
        "wallpaper_get", "wallpaper_set", "theme_get", "theme_set",
        "notifications_get", "notifications_set", "screen_off",
    ],
    "audio": [
        "devices", "default_get", "media_play_pause", "media_next", "media_prev",
        "media_stop", "now_playing",
    ],
    "devices": [
        "bluetooth_status", "bluetooth_on", "bluetooth_off", "bluetooth_devices",
        "usb_list", "printer_list", "printer_default", "print_file", "drivers",
        "problem_devices",
    ],
    "security": [
        "defender_status", "defender_scan", "defender_update", "firewall_status",
        "firewall_rules", "firewall_add", "firewall_remove", "bitlocker_status",
        "secure_boot", "tpm", "users", "sessions", "event_log", "windows_updates",
    ],
    "power": [
        "shutdown", "restart", "sleep", "hibernate", "lock", "signout", "cancel_shutdown",
        "screen_off", "battery", "plans", "plan_set", "last_wake", "requests",
        "sleep_states",
    ],
    "startup": ["list", "add", "remove"],
    "registry": ["read", "list", "write", "delete"],
    "permission": ["get", "set_mode", "request", "verify"],
}


def op_catalog(body: dict) -> dict:
    require("read", "catalog")
    total = sum(len(v) for v in CATALOG.values())
    return _ok(action="catalog", categories=CATALOG, category_count=len(CATALOG),
               action_count=total)


def _ps_list(script: str, timeout: int = 60) -> list:
    data = _ps_json(script, timeout=timeout)
    if isinstance(data, dict):
        return [data]
    return data if isinstance(data, list) else []


def _ps_text(script: str, timeout: int = 60) -> str:
    proc = _ps_run(script, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "powershell failed").strip()[:800])
    return (proc.stdout or "").strip()


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _decode_console(raw: bytes) -> str:
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw[:200]:
        try:
            return raw.decode("utf-16-le", errors="replace")
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def _run_cmd(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, timeout=timeout)
    return proc.returncode, _decode_console(proc.stdout), _decode_console(proc.stderr)


# --------------------------------------------------------------------------- system diagnostics
def op_system_battery(body: dict) -> dict:
    require("read", "battery")
    b = psutil.sensors_battery()
    if not b:
        return _ok(action="battery", supported=False, note="no battery reported by Windows")
    return _ok(action="battery", supported=True, percent=round(float(b.percent), 1),
               plugged=bool(b.power_plugged),
               seconds_left=None if b.secsleft in (-1, -2) else int(b.secsleft),
               minutes_left=None if b.secsleft in (-1, -2) else round(b.secsleft / 60))


def op_system_uptime(body: dict) -> dict:
    require("read", "uptime")
    boot = psutil.boot_time()
    seconds = int(time.time() - boot)
    return _ok(action="uptime", seconds=seconds,
               human=str(timedelta(seconds=seconds)),
               boot_time=datetime.fromtimestamp(boot, timezone.utc).isoformat())


def op_system_disk(body: dict) -> dict:
    require("read", "disk")
    volumes = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
            volumes.append({
                "device": part.device, "mount": part.mountpoint, "fstype": part.fstype,
                "total_gb": round(u.total / 1e9, 1), "used_gb": round(u.used / 1e9, 1),
                "free_gb": round(u.free / 1e9, 1), "percent": u.percent,
            })
        except Exception:
            continue
    physical = []
    try:
        physical = _ps_list(
            "Get-PhysicalDisk -ErrorAction SilentlyContinue | "
            "Select-Object FriendlyName,MediaType,HealthStatus,OperationalStatus,Size | "
            "ConvertTo-Json -Depth 3 -Compress", timeout=45)
    except Exception:
        pass
    return _ok(action="disk", volumes=volumes, physical=physical)


def op_system_memory(body: dict) -> dict:
    require("read", "memory")
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    return _ok(action="memory", total_gb=round(vm.total / 1e9, 1),
               used_gb=round(vm.used / 1e9, 1), available_gb=round(vm.available / 1e9, 1),
               percent=vm.percent, swap_total_gb=round(sm.total / 1e9, 1),
               swap_used_gb=round(sm.used / 1e9, 1), swap_percent=sm.percent)


def op_system_cpu(body: dict) -> dict:
    require("read", "cpu")
    freq = psutil.cpu_freq()
    return _ok(action="cpu", percent=psutil.cpu_percent(interval=0.4),
               per_core=psutil.cpu_percent(interval=0.2, percpu=True),
               cores_logical=psutil.cpu_count(),
               cores_physical=psutil.cpu_count(logical=False),
               freq_mhz=round(freq.current, 1) if freq else None,
               load_avg=[round(x, 2) for x in (psutil.getloadavg() if hasattr(psutil, "getloadavg") else [])])


def op_system_gpu(body: dict) -> dict:
    require("read", "gpu")
    try:
        rows = _ps_list(
            "Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | "
            "Select-Object Name,AdapterRAM,DriverVersion,VideoModeDescription,Status | "
            "ConvertTo-Json -Depth 3 -Compress", timeout=45)
    except Exception as e:
        return _fail(f"GPU query failed: {e}")
    return _ok(action="gpu", count=len(rows), gpus=rows)


def op_process_start(body: dict) -> dict:
    require("write", "process_start")
    command = str(body.get("command") or body.get("path") or body.get("program") or "").strip()
    if not command:
        return _fail("command or path is required")
    denied = _deny_command(command)
    if denied:
        return _fail(denied, blocked=True)
    args = body.get("args") or []
    cwd = body.get("cwd") or None
    try:
        if isinstance(args, list) and args:
            proc = subprocess.Popen([command] + [str(a) for a in args], cwd=cwd,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            proc = subprocess.Popen(command, cwd=cwd, shell=True,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        return _ok(action="process_start", pid=proc.pid, command=command)
    except Exception as e:
        return _fail(f"could not start process: {e}")


def op_process_details(body: dict) -> dict:
    require("read", "process_details")
    pid = body.get("pid")
    name = str(body.get("name") or "").strip()
    if not pid and not name:
        return _fail("pid or name is required")
    procs = []
    if pid:
        try:
            procs = [psutil.Process(int(pid))]
        except Exception as e:
            return _fail(f"process {pid} not found: {e}")
    else:
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info.get("name") or "").lower() == name.lower():
                    procs.append(p)
            except Exception:
                continue
        if not procs:
            return _fail(f"no process named {name}")
    out = []
    for p in procs[:10]:
        try:
            info = p.as_dict(attrs=[
                "pid", "name", "exe", "cmdline", "cwd", "username", "status",
                "create_time", "cpu_percent", "memory_percent", "num_threads",
            ])
            info["memory_mb"] = round(p.memory_info().rss / 1024 / 1024, 1)
            info["create_time"] = datetime.fromtimestamp(
                info["create_time"], timezone.utc).isoformat() if info.get("create_time") else None
            try:
                info["connections"] = [dict(c._asdict()) for c in p.connections()][:20]
            except Exception:
                info["connections"] = []
            out.append(info)
        except Exception as e:
            out.append({"pid": getattr(p, "pid", None), "error": str(e)})
    return _ok(action="process_details", count=len(out), processes=out)


def op_event_log(body: dict) -> dict:
    require("read", "event_log")
    hours = max(1, min(int(body.get("hours", 24) or 24), 720))
    limit = max(1, min(int(body.get("limit", 50) or 50), 300))
    logname = re.sub(r"[^A-Za-z0-9 _-]", "", str(body.get("log") or "System"))[:60] or "System"
    levels = re.sub(r"[^0-9,]", "", str(body.get("levels") or "1,2")) or "1,2"
    script = (
        f"$start=(Get-Date).AddHours(-{hours}); "
        f"Get-WinEvent -FilterHashtable @{{LogName={_ps_quote(logname)};Level={levels};StartTime=$start}} "
        f"-MaxEvents {limit} -ErrorAction SilentlyContinue | "
        "Select-Object TimeCreated,Id,LevelDisplayName,ProviderName,"
        "@{n='Message';e={if($_.Message){$_.Message.Substring(0,[Math]::Min(400,$_.Message.Length))}}} | "
        "ConvertTo-Json -Depth 4 -Compress"
    )
    try:
        rows = _ps_list(script, timeout=90)
    except Exception as e:
        return _fail(f"event log failed: {e}")
    return _ok(action="event_log", log=logname, hours=hours, count=len(rows), events=rows)


def op_installed_apps(body: dict) -> dict:
    require("read", "installed_apps")
    script = (
        "$paths=@('HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'); "
        "Get-ItemProperty $paths -ErrorAction SilentlyContinue | "
        "Where-Object {$_.DisplayName} | "
        "Select-Object DisplayName,DisplayVersion,Publisher,InstallDate,InstallLocation | "
        "Sort-Object DisplayName | ConvertTo-Json -Depth 3 -Compress"
    )
    try:
        rows = _ps_list(script, timeout=90)
    except Exception as e:
        return _fail(f"installed apps failed: {e}")
    needle = str(body.get("filter") or body.get("name") or "").lower().strip()
    if needle:
        rows = [r for r in rows if needle in str(r.get("DisplayName") or "").lower()]
    limit = max(1, min(int(body.get("limit", 200) or 200), 1000))
    return _ok(action="installed_apps", count=len(rows), apps=rows[:limit])


def op_notification(body: dict) -> dict:
    require("write", "notification")
    title = str(body.get("title") or "OTHVM")[:80]
    message = str(body.get("message") or body.get("text") or "")[:300]
    if not message:
        return _fail("message is required")
    script = (
        "$ErrorActionPreference='Stop'; "
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null; "
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        "$n=$t.GetElementsByTagName('text'); "
        f"$n.Item(0).AppendChild($t.CreateTextNode({_ps_quote(title)})) | Out-Null; "
        f"$n.Item(1).AppendChild($t.CreateTextNode({_ps_quote(message)})) | Out-Null; "
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($t); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($toast); 'sent'"
    )
    try:
        out = _ps_text(script, timeout=30)
    except Exception as e:
        return _fail(f"notification failed: {e}")
    return _ok(action="notification", title=title, result=out)


def op_sessions(body: dict) -> dict:
    require("read", "sessions")
    try:
        code, out, err = _run_cmd(["query", "user"], timeout=30)
    except Exception as e:
        return _fail(f"sessions failed: {e}")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return _ok(action="sessions", count=max(0, len(lines) - 1), raw=out[:4000],
               stderr=err[:500] if code else None)


def op_users(body: dict) -> dict:
    require("read", "users")
    try:
        rows = _ps_list(
            "Get-LocalUser -ErrorAction SilentlyContinue | "
            "Select-Object Name,Enabled,LastLogon,Description | "
            "ConvertTo-Json -Depth 3 -Compress", timeout=45)
    except Exception as e:
        return _fail(f"users failed: {e}")
    return _ok(action="users", count=len(rows), users=rows)


def op_windows_updates(body: dict) -> dict:
    require("read", "windows_updates")
    installed = []
    try:
        installed = _ps_list(
            "Get-CimInstance Win32_QuickFixEngineering -ErrorAction SilentlyContinue | "
            "Select-Object HotFixID,Description,InstalledOn | "
            "Sort-Object InstalledOn -Descending | Select-Object -First 30 | "
            "ConvertTo-Json -Depth 3 -Compress", timeout=60)
    except Exception:
        pass
    pending, pending_error = [], None
    try:
        pending = _ps_list(
            "$s=New-Object -ComObject Microsoft.Update.Session; "
            "$q=$s.CreateUpdateSearcher(); "
            "$r=$q.Search('IsInstalled=0 and IsHidden=0'); "
            "$r.Updates | Select-Object Title,IsDownloaded,IsMandatory | "
            "ConvertTo-Json -Depth 3 -Compress", timeout=120)
    except Exception as e:
        pending_error = str(e)[:400]
    return _ok(action="windows_updates", installed=installed, pending=pending,
               pending_error=pending_error)


def op_wsl_list(body: dict) -> dict:
    require("read", "wsl_list")
    try:
        code, out, err = _run_cmd(["wsl.exe", "-l", "-v"], timeout=30)
    except Exception as e:
        return _fail(f"wsl failed: {e}")
    if code != 0:
        return _fail(f"wsl list failed: {(err or out)[:600]}")
    lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
    return _ok(action="wsl_list", raw=out[:4000], lines=lines)


def op_wsl_run(body: dict) -> dict:
    require("full", "wsl_run")
    command = str(body.get("command") or "").strip()
    if not command:
        return _fail("command is required")
    if not _truthy(body.get("confirm")):
        return _fail("wsl_run needs confirm=true", needs_approval=True)
    denied = _deny_command(command)
    if denied:
        return _fail(denied, blocked=True)
    distro = str(body.get("distro") or "").strip()
    args = ["wsl.exe"]
    if distro:
        args += ["-d", distro]
    args += ["--", "bash", "-lc", command]
    try:
        code, out, err = _run_cmd(args, timeout=max(5, min(int(body.get("timeout", 120) or 120), 600)))
    except Exception as e:
        return _fail(f"wsl run failed: {e}")
    return _ok(action="wsl_run", exit_code=code, stdout=out[-12000:], stderr=err[-4000:])


def op_docker_list(body: dict) -> dict:
    require("read", "docker_list")
    if not shutil.which("docker"):
        return _fail("docker is not installed or not in PATH")
    try:
        code, out, err = _run_cmd(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=60)
    except Exception as e:
        return _fail(f"docker list failed: {e}")
    if code != 0:
        return _fail(f"docker list failed: {(err or out)[:600]}")
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return _ok(action="docker_list", count=len(rows), containers=rows)


def op_docker_control(body: dict) -> dict:
    require("full", "docker_control")
    action = str(body.get("docker_action") or body.get("mode") or body.get("op") or "").lower()
    container = str(body.get("container") or body.get("name") or body.get("id") or "").strip()
    if action not in ("start", "stop", "restart", "rm"):
        return _fail("docker_action must be start, stop, restart, or rm")
    if not container:
        return _fail("container is required")
    if action == "rm" and not _truthy(body.get("confirm")):
        return _fail("docker rm needs confirm=true", needs_approval=True)
    if not shutil.which("docker"):
        return _fail("docker is not installed or not in PATH")
    args = ["docker", action]
    if action == "rm":
        args.append("-f")
    args.append(container)
    try:
        code, out, err = _run_cmd(args, timeout=120)
    except Exception as e:
        return _fail(f"docker {action} failed: {e}")
    if code != 0:
        return _fail(f"docker {action} failed: {(err or out)[:600]}")
    return _ok(action=f"docker_{action}", container=container, output=out[:2000])


# --------------------------------------------------------------------------- windows
def _window_target(body: dict):
    title = str(body.get("title") or body.get("window") or "").strip()
    handle = body.get("handle") or body.get("hwnd")
    win32gui, _ = _win32()
    if handle:
        try:
            hwnd = int(handle)
            return hwnd, win32gui.GetWindowText(hwnd) or ""
        except Exception:
            return None, ""
    matches = _find_windows(title)
    return matches[0] if matches else (None, "")


def op_windows(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    if action == "list":
        return op_list_windows(body)
    if action in ("focus", "minimize", "maximize"):
        fn = {"focus": op_focus_window, "minimize": op_minimize_window,
              "maximize": op_maximize_window}[action]
        return fn(body)
    require("write", f"windows.{action}")
    win32gui, win32con = _win32()
    hwnd, title = _window_target(body)
    if not hwnd:
        return _fail("no matching window")
    try:
        if action == "restore":
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            return _ok(action="windows.restore", window=title, handle=hwnd)
        if action == "close":
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return _ok(action="windows.close", window=title, handle=hwnd)
        if action == "move":
            x, y = int(body.get("x", 0)), int(body.get("y", 0))
            win32gui.SetWindowPos(hwnd, 0, x, y, 0, 0,
                                  win32con.SWP_NOSIZE | win32con.SWP_NOZORDER)
            return _ok(action="windows.move", window=title, handle=hwnd, x=x, y=y)
        if action == "resize":
            width = int(body.get("width") or body.get("w") or 0)
            height = int(body.get("height") or body.get("h") or 0)
            if width <= 0 or height <= 0:
                return _fail("width and height must be positive")
            win32gui.SetWindowPos(hwnd, 0, 0, 0, width, height,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOZORDER)
            return _ok(action="windows.resize", window=title, handle=hwnd,
                       width=width, height=height)
        if action == "always_on_top":
            top = _truthy(body.get("top", body.get("enabled", True)))
            insert_after = -1 if top else -2  # HWND_TOPMOST / HWND_NOTOPMOST
            win32gui.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            return _ok(action="windows.always_on_top", window=title, handle=hwnd, top=top)
        if action == "snap":
            import win32api
            side = str(body.get("side") or "left").lower()
            sw = win32api.GetSystemMetrics(0)
            sh = win32api.GetSystemMetrics(1)
            half = sw // 2
            if side == "left":
                x, y, w, h = 0, 0, half, sh
            elif side == "right":
                x, y, w, h = half, 0, half, sh
            elif side == "top":
                x, y, w, h = 0, 0, sw, sh // 2
            elif side == "bottom":
                x, y, w, h = 0, sh // 2, sw, sh // 2
            else:
                return _fail("side must be left, right, top, or bottom")
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(hwnd, 0, x, y, w, h, win32con.SWP_NOZORDER)
            return _ok(action="windows.snap", window=title, handle=hwnd, side=side,
                       rect={"x": x, "y": y, "width": w, "height": h})
        return _fail(f'unknown windows action "{action}"')
    except Exception as e:
        return _fail(f"window action failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- network
def _parse_netsh(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


def op_network(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    read_actions = {"interfaces", "ip", "wifi_status", "wifi_list", "ping", "dns",
                    "public_ip", "ports", "connections", "vpn_list", "proxy_get",
                    "hosts_read", "firewall_status", "firewall_rules"}
    require("read" if action in read_actions else "write", f"network.{action}")
    try:
        if action == "interfaces":
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            rows = []
            for name, items in addrs.items():
                row = {"name": name, "up": bool(stats.get(name).isup) if stats.get(name) else None,
                       "speed_mbps": stats.get(name).speed if stats.get(name) else None,
                       "addresses": []}
                for a in items:
                    row["addresses"].append({"family": str(a.family), "address": a.address,
                                             "netmask": a.netmask, "broadcast": a.broadcast})
                rows.append(row)
            return _ok(action="network.interfaces", count=len(rows), interfaces=rows)
        if action == "ip":
            rows = _ps_list(
                "Get-NetIPConfiguration -ErrorAction SilentlyContinue | "
                "Select-Object InterfaceAlias,InterfaceDescription,"
                "@{n='IPv4';e={$_.IPv4Address.IPAddress}},"
                "@{n='Gateway';e={$_.IPv4DefaultGateway.NextHop}},"
                "@{n='DNS';e={$_.DNSServer.ServerAddresses}} | "
                "ConvertTo-Json -Depth 4 -Compress", timeout=45)
            return _ok(action="network.ip", count=len(rows), interfaces=rows)
        if action == "wifi_status":
            code, out, err = _run_cmd(["netsh", "wlan", "show", "interfaces"], timeout=30)
            if code != 0:
                return _fail(f"wifi status failed: {(err or out)[:600]}")
            return _ok(action="network.wifi_status", parsed=_parse_netsh(out), raw=out[:4000])
        if action == "wifi_list":
            code, out, err = _run_cmd(["netsh", "wlan", "show", "networks", "mode=bssid"], timeout=45)
            if code != 0:
                return _fail(f"wifi list failed: {(err or out)[:600]}")
            return _ok(action="network.wifi_list", raw=out[:8000])
        if action == "wifi_connect":
            ssid = str(body.get("ssid") or body.get("name") or "").strip()
            if not ssid:
                return _fail("ssid is required")
            code, out, err = _run_cmd(["netsh", "wlan", "connect", "name=" + ssid], timeout=45)
            if code != 0:
                return _fail(f"wifi connect failed: {(err or out)[:600]}")
            return _ok(action="network.wifi_connect", ssid=ssid, output=out[:1000])
        if action == "wifi_disconnect":
            code, out, err = _run_cmd(["netsh", "wlan", "disconnect"], timeout=30)
            if code != 0:
                return _fail(f"wifi disconnect failed: {(err or out)[:600]}")
            return _ok(action="network.wifi_disconnect", output=out[:1000])
        if action == "ping":
            host = str(body.get("host") or body.get("target") or "").strip()
            if not host:
                return _fail("host is required")
            count = max(1, min(int(body.get("count", 4) or 4), 20))
            code, out, err = _run_cmd(["ping", "-n", str(count), host],
                                      timeout=max(10, count * 3 + 10))
            return _ok(action="network.ping", host=host, count=count, exit_code=code,
                       output=out[-6000:], stderr=err[-1000:])
        if action == "dns":
            host = str(body.get("name") or body.get("host") or "").strip()
            if not host:
                return _fail("name is required")
            rows = _ps_list(
                f"Resolve-DnsName -Name {_ps_quote(host)} -ErrorAction SilentlyContinue | "
                "Select-Object Name,Type,IPAddress,NameHost | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=45)
            return _ok(action="network.dns", name=host, count=len(rows), records=rows)
        if action == "public_ip":
            data = {}
            try:
                with httpx.Client(timeout=15) as c:
                    r = c.get("https://api.ipify.org?format=json")
                    data = r.json()
            except Exception:
                try:
                    with httpx.Client(timeout=15) as c:
                        r = c.get("https://ifconfig.me/ip")
                        data = {"ip": r.text.strip()}
                except Exception as e:
                    return _fail(f"public ip failed: {e}")
            return _ok(action="network.public_ip", **data)
        if action in ("ports", "connections"):
            state = "Listen" if action == "ports" else "Established"
            rows = _ps_list(
                f"Get-NetTCPConnection -State {state} -ErrorAction SilentlyContinue | "
                "Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,OwningProcess | "
                "Sort-Object LocalPort | ConvertTo-Json -Depth 3 -Compress", timeout=45)
            for row in rows:
                try:
                    row["process"] = psutil.Process(int(row.get("OwningProcess"))).name()
                except Exception:
                    row["process"] = None
            return _ok(action=f"network.{action}", count=len(rows), items=rows[:300])
        if action == "vpn_list":
            rows = []
            for scope in ("", "-AllUserConnection"):
                try:
                    script = f"Get-VpnConnection {scope} -ErrorAction SilentlyContinue | Select-Object Name,ServerAddress,ConnectionStatus | ConvertTo-Json -Depth 3 -Compress"
                    rows.extend(_ps_list(script, timeout=30))
                except Exception:
                    continue
            return _ok(action="network.vpn_list", count=len(rows), vpns=rows)
        if action == "proxy_get":
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            try:
                enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            except Exception:
                enabled = 0
            try:
                server = winreg.QueryValueEx(key, "ProxyServer")[0]
            except Exception:
                server = ""
            winreg.CloseKey(key)
            return _ok(action="network.proxy_get", enabled=bool(enabled), server=server)
        if action == "proxy_set":
            if not _truthy(body.get("confirm")):
                return _fail("proxy_set needs confirm=true", needs_approval=True)
            enabled = 1 if _truthy(body.get("enabled", True)) else 0
            server = str(body.get("server") or "")
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                                 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, enabled)
            if server:
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
            winreg.CloseKey(key)
            return _ok(action="network.proxy_set", enabled=bool(enabled), server=server)
        if action == "hosts_read":
            path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
            return _ok(action="network.hosts_read", path=str(path),
                       content=path.read_text(encoding="utf-8", errors="replace")[:20000])
        if action == "hosts_write":
            if not _truthy(body.get("confirm")):
                return _fail("hosts_write needs confirm=true", needs_approval=True)
            content = str(body.get("content") or "")
            if not content.strip():
                return _fail("content is required")
            path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
            backup = path.with_suffix(".othvm.bak")
            try:
                backup.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                path.write_text(content, encoding="utf-8")
            except Exception as e:
                return _fail(f"hosts write failed: {e}")
            return _ok(action="network.hosts_write", path=str(path), backup=str(backup))
        if action == "firewall_status":
            rows = _ps_list(
                "Get-NetFirewallProfile -ErrorAction SilentlyContinue | "
                "Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=45)
            return _ok(action="network.firewall_status", profiles=rows)
        if action == "firewall_rules":
            name = str(body.get("name") or body.get("filter") or "").strip()
            where = f" -DisplayName *{name}*" if name else ""
            rows = _ps_list(
                f"Get-NetFirewallRule{where} -ErrorAction SilentlyContinue | "
                "Select-Object DisplayName,Direction,Action,Enabled,Profile | "
                "Sort-Object DisplayName | Select-Object -First 300 | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=60)
            return _ok(action="network.firewall_rules", count=len(rows), rules=rows)
        if action == "firewall_add":
            if not _truthy(body.get("confirm")):
                return _fail("firewall_add needs confirm=true", needs_approval=True)
            name = str(body.get("name") or "").strip()
            port = str(body.get("port") or "").strip()
            protocol = str(body.get("protocol") or "TCP").upper()
            direction = str(body.get("direction") or "In").capitalize()
            if not name or not port:
                return _fail("name and port are required")
            if protocol not in ("TCP", "UDP"):
                return _fail("protocol must be TCP or UDP")
            if direction not in ("In", "Out"):
                return _fail("direction must be In or Out")
            script = (
                f"New-NetFirewallRule -DisplayName {_ps_quote(name)} -Direction {direction} "
                f"-Action Allow -Protocol {protocol} -LocalPort {port} | Out-Null; 'created'"
            )
            out = _ps_text(script, timeout=45)
            return _ok(action="network.firewall_add", name=name, port=port,
                       protocol=protocol, direction=direction, result=out)
        if action == "firewall_remove":
            if not _truthy(body.get("confirm")):
                return _fail("firewall_remove needs confirm=true", needs_approval=True)
            name = str(body.get("name") or "").strip()
            if not name:
                return _fail("name is required")
            out = _ps_text(
                f"Remove-NetFirewallRule -DisplayName {_ps_quote(name)} -ErrorAction Stop; 'removed'",
                timeout=45)
            return _ok(action="network.firewall_remove", name=name, result=out)
        return _fail(f'unknown network action "{action}"')
    except Exception as e:
        return _fail(f"network action failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- display
def _display_mode(body: dict):
    import ctypes

    class DEVMODE(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", ctypes.c_wchar * 32),
            ("dmSpecVersion", ctypes.c_ushort),
            ("dmDriverVersion", ctypes.c_ushort),
            ("dmSize", ctypes.c_ushort),
            ("dmDriverExtra", ctypes.c_ushort),
            ("dmFields", ctypes.c_uint32),
            ("dmPositionX", ctypes.c_int32),
            ("dmPositionY", ctypes.c_int32),
            ("dmDisplayOrientation", ctypes.c_uint32),
            ("dmDisplayFixedOutput", ctypes.c_uint32),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short),
            ("dmFormName", ctypes.c_wchar * 32),
            ("dmLogPixels", ctypes.c_ushort),
            ("dmBitsPerPel", ctypes.c_uint32),
            ("dmPelsWidth", ctypes.c_uint32),
            ("dmPelsHeight", ctypes.c_uint32),
            ("dmDisplayFlags", ctypes.c_uint32),
            ("dmDisplayFrequency", ctypes.c_uint32),
            ("dmICMMethod", ctypes.c_uint32),
            ("dmICMIntent", ctypes.c_uint32),
            ("dmMediaType", ctypes.c_uint32),
            ("dmDitherType", ctypes.c_uint32),
            ("dmReserved1", ctypes.c_uint32),
            ("dmReserved2", ctypes.c_uint32),
            ("dmPanningWidth", ctypes.c_uint32),
            ("dmPanningHeight", ctypes.c_uint32),
        ]

    dm = DEVMODE()
    dm.dmSize = ctypes.sizeof(DEVMODE)
    if not ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
        raise RuntimeError("EnumDisplaySettings failed")
    return dm


def op_display(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    read_actions = {"monitors", "resolution_get", "wallpaper_get", "theme_get",
                    "notifications_get"}
    require("read" if action in read_actions else "write", f"display.{action}")
    try:
        if action == "monitors":
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.Screen]::AllScreens | ForEach-Object { "
                "[pscustomobject]@{DeviceName=$_.DeviceName;Primary=$_.Primary;"
                "Width=$_.Bounds.Width;Height=$_.Bounds.Height;X=$_.Bounds.X;Y=$_.Bounds.Y} } | "
                "ConvertTo-Json -Depth 3 -Compress"
            )
            screens = _ps_list(script, timeout=30)
            return _ok(action="display.monitors", count=len(screens), monitors=screens)
        if action == "resolution_get":
            dm = _display_mode(body)
            return _ok(action="display.resolution_get", width=dm.dmPelsWidth,
                       height=dm.dmPelsHeight, refresh_hz=dm.dmDisplayFrequency,
                       bits_per_pixel=dm.dmBitsPerPel,
                       orientation=dm.dmDisplayOrientation)
        if action in ("resolution_set", "refresh_rate_set"):
            import ctypes
            width = body.get("width")
            height = body.get("height")
            hz = body.get("refresh_hz") or body.get("hz") or body.get("refresh")
            dm = _display_mode(body)
            fields = 0
            if action == "resolution_set":
                if not width or not height:
                    return _fail("width and height are required")
                width, height = int(width), int(height)
                if not (320 <= width <= 16384 and 200 <= height <= 16384):
                    return _fail("resolution out of range")
                dm.dmPelsWidth, dm.dmPelsHeight = width, height
                fields |= 0x80000 | 0x100000  # DM_PELSWIDTH | DM_PELSHEIGHT
            if hz:
                dm.dmDisplayFrequency = int(hz)
                fields |= 0x400000  # DM_DISPLAYFREQUENCY
            if not fields:
                return _fail("nothing to change (width/height or refresh_hz required)")
            dm.dmFields = fields
            result = ctypes.windll.user32.ChangeDisplaySettingsW(ctypes.byref(dm), 0)
            if result != 0:
                return _fail(f"ChangeDisplaySettings failed with code {result}",
                             hint="the requested mode may not be supported by this display")
            return _ok(action=f"display.{action}", width=dm.dmPelsWidth,
                       height=dm.dmPelsHeight, refresh_hz=dm.dmDisplayFrequency,
                       result=result)
        if action == "wallpaper_get":
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop")
            try:
                path = winreg.QueryValueEx(key, "Wallpaper")[0]
            except Exception:
                path = ""
            winreg.CloseKey(key)
            return _ok(action="display.wallpaper_get", path=path,
                       exists=bool(path) and Path(path).exists())
        if action == "wallpaper_set":
            import ctypes
            path = Path(str(body.get("path") or "").strip()).expanduser()
            denied = _deny_path(path)
            if denied:
                return _fail(denied, blocked=True)
            if not path.is_file():
                return _fail(f"wallpaper not found: {path}")
            result = ctypes.windll.user32.SystemParametersInfoW(
                20, 0, str(path), 3)  # SPI_SETDESKWALLPAPER, UPDATEINIFILE|SENDCHANGE
            if not result:
                return _fail("SystemParametersInfo failed to set wallpaper")
            return _ok(action="display.wallpaper_set", path=str(path))
        if action == "theme_get":
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            try:
                apps = int(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
            except Exception:
                apps = 1
            try:
                system = int(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
            except Exception:
                system = 1
            winreg.CloseKey(key)
            return _ok(action="display.theme_get",
                       apps_theme="light" if apps else "dark",
                       system_theme="light" if system else "dark")
        if action == "theme_set":
            theme = str(body.get("theme") or "").lower().strip()
            if theme not in ("light", "dark"):
                return _fail("theme must be light or dark")
            value = 1 if theme == "light" else 0
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                                 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, value)
            winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, value)
            winreg.CloseKey(key)
            return _ok(action="display.theme_set", theme=theme,
                       note="some apps apply the theme after restart")
        if action == "notifications_get":
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\PushNotifications")
            try:
                enabled = int(winreg.QueryValueEx(key, "ToastEnabled")[0])
            except Exception:
                enabled = 1
            winreg.CloseKey(key)
            return _ok(action="display.notifications_get", enabled=bool(enabled))
        if action == "notifications_set":
            enabled = 1 if _truthy(body.get("enabled", True)) else 0
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\PushNotifications",
                                 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "ToastEnabled", 0, winreg.REG_DWORD, enabled)
            winreg.CloseKey(key)
            return _ok(action="display.notifications_set", enabled=bool(enabled))
        if action == "screen_off":
            import ctypes
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
            return _ok(action="display.screen_off")
        return _fail(f'unknown display action "{action}"')
    except Exception as e:
        return _fail(f"display action failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- audio
_MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
               "mute": 0xAD, "volume_up": 0xAF, "volume_down": 0xAE}


def _media_key(name: str) -> None:
    import ctypes
    vk = _MEDIA_KEYS.get(name)
    if not vk:
        raise ValueError(f"unknown media key: {name}")
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


def op_audio(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    read_actions = {"devices", "default_get", "now_playing"}
    require("read" if action in read_actions else "write", f"audio.{action}")
    try:
        if action == "devices":
            rows = _ps_list(
                "Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue | "
                "Select-Object FriendlyName,Status,InstanceId | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=45)
            return _ok(action="audio.devices", count=len(rows), devices=rows)
        if action == "default_get":
            rows = _ps_list(
                "Get-CimInstance Win32_SoundDevice -ErrorAction SilentlyContinue | "
                "Select-Object Name,Manufacturer,Status,DeviceID | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=45)
            return _ok(action="audio.default_get", count=len(rows), devices=rows)
        if action in _MEDIA_KEYS:
            _media_key(action)
            return _ok(action=f"audio.{action}")
        if action == "now_playing":
            names = {"spotify", "vlc", "wmplayer", "musicbee", "foobar2000", "itunes",
                     "groove", "mediaplayer", "tidal", "deezer", "amazon music"}
            rows = []
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    pname = (p.info.get("name") or "").lower()
                    base = pname[:-4] if pname.endswith(".exe") else pname
                    if base not in names:
                        continue
                    title = ""
                    try:
                        proc = psutil.Process(p.info["pid"])
                        title = proc.name()
                        import win32gui
                        import win32process
                        def cb(hwnd, _):
                            nonlocal title
                            try:
                                if not win32gui.IsWindowVisible(hwnd):
                                    return
                                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                                if pid == p.info["pid"]:
                                    t = win32gui.GetWindowText(hwnd) or ""
                                    if t.strip():
                                        title = t
                            except Exception:
                                return
                        win32gui.EnumWindows(cb, None)
                    except Exception:
                        pass
                    rows.append({"process": pname, "pid": p.info["pid"], "title": title})
                except Exception:
                    continue
            return _ok(action="audio.now_playing", count=len(rows), players=rows,
                       note="window title based; media metadata is not exposed by Windows")
        return _fail(f'unknown audio action "{action}"')
    except Exception as e:
        return _fail(f"audio action failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- devices
def op_devices(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    read_actions = {"bluetooth_status", "bluetooth_devices", "usb_list", "printer_list",
                    "printer_default", "drivers", "problem_devices"}
    require("read" if action in read_actions else "write", f"devices.{action}")
    try:
        if action == "bluetooth_status":
            svc = _ps_list(
                "Get-CimInstance Win32_Service -ErrorAction SilentlyContinue | "
                "Where-Object {$_.Name -in @('bthserv','bthhfsrv')} | "
                "Select-Object Name,State,StartMode | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=45)
            radios = _ps_list(
                "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
                "Select-Object FriendlyName,Status,InstanceId | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=45)
            return _ok(action="devices.bluetooth_status", services=svc,
                       count=len(radios), devices=radios)
        if action in ("bluetooth_on", "bluetooth_off"):
            if action == "bluetooth_off" and not _truthy(body.get("confirm")):
                return _fail("bluetooth_off needs confirm=true", needs_approval=True)
            if action == "bluetooth_on":
                out = _ps_text("Start-Service bthserv -ErrorAction Stop; 'started'", timeout=45)
            else:
                out = _ps_text("Stop-Service bthserv -Force -ErrorAction Stop; 'stopped'", timeout=45)
            return _ok(action=f"devices.{action}", result=out)
        if action == "bluetooth_devices":
            rows = _ps_list(
                "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
                "Select-Object FriendlyName,Status,Class,InstanceId | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=45)
            return _ok(action="devices.bluetooth_devices", count=len(rows), devices=rows)
        if action == "usb_list":
            rows = _ps_list(
                "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
                "Where-Object {$_.InstanceId -like 'USB*'} | "
                "Select-Object FriendlyName,Class,Status,InstanceId | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=60)
            return _ok(action="devices.usb_list", count=len(rows), devices=rows)
        if action == "printer_list":
            rows = _ps_list(
                "Get-Printer -ErrorAction SilentlyContinue | "
                "Select-Object Name,DriverName,PortName,PrinterStatus,Shared | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=45)
            return _ok(action="devices.printer_list", count=len(rows), printers=rows)
        if action == "printer_default":
            rows = _ps_list(
                "Get-CimInstance Win32_Printer -ErrorAction SilentlyContinue | "
                "Where-Object {$_.Default -eq $true} | "
                "Select-Object Name,DriverName,PortName | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=45)
            return _ok(action="devices.printer_default", count=len(rows), printers=rows)
        if action == "print_file":
            path = Path(str(body.get("path") or "").strip()).expanduser()
            denied = _deny_path(path)
            if denied:
                return _fail(denied, blocked=True)
            if not path.is_file():
                return _fail(f"file not found: {path}")
            printer = str(body.get("printer") or "").strip()
            if printer:
                _ps_text(
                    f"Start-Process -FilePath {_ps_quote(str(path))} -Verb PrintTo "
                    f"-ArgumentList {_ps_quote(printer)}", timeout=60)
            else:
                _ps_text(f"Start-Process -FilePath {_ps_quote(str(path))} -Verb Print", timeout=60)
            return _ok(action="devices.print_file", path=str(path), printer=printer or None)
        if action == "drivers":
            rows = _ps_list(
                "Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue | "
                "Select-Object DeviceName,DriverVersion,DriverDate,Manufacturer | "
                "Sort-Object DeviceName | Select-Object -First 300 | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=90)
            return _ok(action="devices.drivers", count=len(rows), drivers=rows)
        if action == "problem_devices":
            rows = _ps_list(
                "Get-PnpDevice -ErrorAction SilentlyContinue | "
                "Where-Object {$_.Status -ne 'OK'} | "
                "Select-Object FriendlyName,Class,Status,InstanceId | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=60)
            return _ok(action="devices.problem_devices", count=len(rows), devices=rows)
        return _fail(f'unknown devices action "{action}"')
    except Exception as e:
        return _fail(f"devices action failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- security
def op_security(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    read_actions = {"defender_status", "bitlocker_status", "secure_boot", "tpm",
                    "firewall_status", "firewall_rules", "users", "sessions",
                    "event_log", "windows_updates"}
    require("read" if action in read_actions else "write", f"security.{action}")
    try:
        if action == "defender_status":
            rows = _ps_list(
                "Get-MpComputerStatus -ErrorAction SilentlyContinue | "
                "Select-Object AMServiceEnabled,AntivirusEnabled,RealTimeProtectionEnabled,"
                "AntispywareEnabled,BehaviorMonitorEnabled,IoavProtectionEnabled,"
                "QuickScanEndTime,FullScanEndTime,AntivirusSignatureVersion,"
                "AntivirusSignatureLastUpdated | ConvertTo-Json -Depth 3 -Compress",
                timeout=60)
            return _ok(action="security.defender_status",
                       status=rows[0] if rows else None)
        if action in ("defender_scan", "defender_update"):
            if not _truthy(body.get("confirm")):
                return _fail(f"{action} needs confirm=true", needs_approval=True)
            if action == "defender_update":
                out = _ps_text("Update-MpSignature -ErrorAction Stop; 'updated'", timeout=180)
                return _ok(action="security.defender_update", result=out)
            scan = str(body.get("scan_type") or "Quick").capitalize()
            if scan not in ("Quick", "Full", "Custom"):
                return _fail("scan_type must be Quick or Full")
            out = _ps_text(f"Start-MpScan -ScanType {scan} -ErrorAction Stop; 'started'",
                           timeout=180)
            return _ok(action="security.defender_scan", scan_type=scan, result=out)
        if action == "bitlocker_status":
            rows = _ps_list(
                "Get-BitLockerVolume -ErrorAction SilentlyContinue | "
                "Select-Object MountPoint,VolumeStatus,ProtectionStatus,"
                "EncryptionPercentage,EncryptionMethod | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=60)
            volume_states = {0: "fully_decrypted", 1: "fully_encrypted",
                             2: "encryption_in_progress", 3: "decryption_in_progress",
                             4: "encryption_paused", 5: "decryption_paused"}
            protection_states = {0: "off", 1: "on", 2: "unknown"}
            for row in rows:
                try:
                    row["volume_status_human"] = volume_states.get(
                        int(row.get("VolumeStatus")), str(row.get("VolumeStatus")))
                    row["protection_status_human"] = protection_states.get(
                        int(row.get("ProtectionStatus")), str(row.get("ProtectionStatus")))
                except Exception:
                    continue
            return _ok(action="security.bitlocker_status", count=len(rows), volumes=rows)
        if action == "secure_boot":
            proc = _ps_run("try { Confirm-SecureBootUEFI } catch { $_.Exception.Message }",
                           timeout=30)
            raw = (proc.stdout or "").strip()
            enabled = raw.lower() == "true"
            return _ok(action="security.secure_boot", enabled=enabled, raw=raw[:500],
                       supported="not supported" not in raw.lower())
        if action == "tpm":
            rows = _ps_list(
                "Get-Tpm -ErrorAction SilentlyContinue | "
                "Select-Object TpmPresent,TpmReady,TpmEnabled,TpmActivated,"
                "ManufacturerIdTxt,ManufacturerVersion | "
                "ConvertTo-Json -Depth 3 -Compress", timeout=45)
            return _ok(action="security.tpm", tpm=rows[0] if rows else None)
        if action in ("firewall_status", "firewall_rules", "firewall_add", "firewall_remove"):
            return op_network(body)
        if action == "users":
            return op_users(body)
        if action == "sessions":
            return op_sessions(body)
        if action == "event_log":
            return op_event_log(body)
        if action == "windows_updates":
            return op_windows_updates(body)
        return _fail(f'unknown security action "{action}"')
    except Exception as e:
        return _fail(f"security action failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- power
def op_power(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    read_actions = {"battery", "plans", "last_wake", "requests", "sleep_states"}
    require("read" if action in read_actions else "write", f"power.{action}")
    try:
        if action == "battery":
            return op_system_battery(body)
        if action == "shutdown":
            return op_shutdown(body)
        if action == "restart":
            return op_restart(body)
        if action == "sleep":
            return op_sleep(body)
        if action == "hibernate":
            return op_hibernate(body)
        if action == "lock":
            return op_lock(body)
        if action == "cancel_shutdown":
            return op_cancel_shutdown(body)
        if action == "screen_off":
            return op_display({"action": "screen_off"})
        if action == "signout":
            if not _truthy(body.get("confirm")):
                return _fail("signout needs confirm=true", needs_approval=True)
            _run_cmd(["shutdown", "/l"], timeout=30)
            return _ok(action="power.signout")
        if action == "plans":
            code, out, err = _run_cmd(["powercfg", "/L"], timeout=30)
            return _ok(action="power.plans", exit_code=code, output=out[-6000:],
                       stderr=err[-1000:])
        if action == "plan_set":
            guid = str(body.get("guid") or body.get("plan") or "").strip()
            if not re.match(r"^[0-9a-fA-F-]{36}$", guid):
                return _fail("guid is required (use power.plans to list them)")
            if not _truthy(body.get("confirm")):
                return _fail("plan_set needs confirm=true", needs_approval=True)
            code, out, err = _run_cmd(["powercfg", "/S", guid], timeout=30)
            if code != 0:
                return _fail(f"powercfg failed: {(err or out)[:600]}")
            return _ok(action="power.plan_set", guid=guid)
        if action == "last_wake":
            code, out, err = _run_cmd(["powercfg", "/lastwake"], timeout=30)
            return _ok(action="power.last_wake", output=out[-6000:], stderr=err[-1000:])
        if action == "requests":
            code, out, err = _run_cmd(["powercfg", "/requests"], timeout=30)
            return _ok(action="power.requests", output=out[-8000:], stderr=err[-1000:])
        if action == "sleep_states":
            code, out, err = _run_cmd(["powercfg", "/a"], timeout=30)
            return _ok(action="power.sleep_states", output=out[-6000:], stderr=err[-1000:])
        return _fail(f'unknown power action "{action}"')
    except Exception as e:
        return _fail(f"power action failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- startup
def _startup_entries() -> list:
    rows = []
    run_keys = [
        ("user", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        ("machine", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        ("machine", winreg.HKEY_LOCAL_MACHINE,
         r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ]
    for scope, hive, path in run_keys:
        try:
            key = winreg.OpenKey(hive, path)
        except Exception:
            continue
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            rows.append({"scope": scope, "source": "registry", "key": path,
                         "name": name, "command": value})
            i += 1
        winreg.CloseKey(key)
    folders = [
        ("user", Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
         "Start Menu" / "Programs" / "Startup"),
        ("machine", Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft" /
         "Windows" / "Start Menu" / "Programs" / "Startup"),
    ]
    for scope, folder in folders:
        try:
            for p in folder.iterdir():
                rows.append({"scope": scope, "source": "folder", "key": str(folder),
                             "name": p.name, "command": str(p)})
        except Exception:
            continue
    return rows


def op_startup(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    require("read" if action == "list" else "full", f"startup.{action}")
    try:
        if action == "list":
            rows = _startup_entries()
            return _ok(action="startup.list", count=len(rows), entries=rows)
        if action not in ("add", "remove"):
            return _fail(f'unknown startup action "{action}"')
        if not _truthy(body.get("confirm")):
            return _fail(f"startup.{action} needs confirm=true", needs_approval=True)
        name = str(body.get("name") or "").strip()
        if not name:
            return _fail("name is required")
        scope = str(body.get("scope") or "user").lower().strip()
        if scope not in ("user", "machine"):
            return _fail("scope must be user or machine")
        hive = winreg.HKEY_CURRENT_USER if scope == "user" else winreg.HKEY_LOCAL_MACHINE
        path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        if action == "add":
            command = str(body.get("command") or body.get("path") or "").strip()
            if not command:
                return _fail("command is required")
            denied = _deny_command(command)
            if denied:
                return _fail(denied, blocked=True)
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
            winreg.CloseKey(key)
            return _ok(action="startup.add", name=name, scope=scope, command=command)
        key = winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, name)
        finally:
            winreg.CloseKey(key)
        return _ok(action="startup.remove", name=name, scope=scope)
    except FileNotFoundError:
        return _fail("startup entry not found")
    except Exception as e:
        return _fail(f"startup action failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- registry
_HIVES = {
    "HKCU": winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    "HKLM": winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
    "HKCR": winreg.HKEY_CLASSES_ROOT, "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT,
    "HKU": winreg.HKEY_USERS, "HKEY_USERS": winreg.HKEY_USERS,
    "HKCC": winreg.HKEY_CURRENT_CONFIG, "HKEY_CURRENT_CONFIG": winreg.HKEY_CURRENT_CONFIG,
}
_REG_DENY = ("sam\\sam", "security\\policy", "bcd00000000",
             "currentcontrolset\\control\\lsa",
             "currentcontrolset\\control\\session manager",
             "currentcontrolset\\services\\windefend")
_REG_TYPES = {"string": winreg.REG_SZ, "expand": winreg.REG_EXPAND_SZ,
              "dword": winreg.REG_DWORD, "qword": winreg.REG_QWORD,
              "multi": winreg.REG_MULTI_SZ, "binary": winreg.REG_BINARY}


def _registry_target(body: dict):
    hive_name = str(body.get("hive") or "HKCU").upper().strip()
    if hive_name not in _HIVES:
        raise ValueError("hive must be HKCU, HKLM, HKCR, HKU, or HKCC")
    path = str(body.get("path") or body.get("key") or "").strip().strip("\\")
    if not path:
        raise ValueError("path is required (for example Software\\Microsoft\\Windows\\CurrentVersion\\Run)")
    lowered = path.lower()
    for blocked in _REG_DENY:
        if blocked in lowered:
            raise PermissionError(f"registry path is blocked for safety: {blocked}")
    return _HIVES[hive_name], path


def op_registry(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    require("read" if action in ("read", "list") else "full", f"registry.{action}")
    try:
        hive, path = _registry_target(body)
        name = str(body.get("name") or body.get("value_name") or "").strip()
        if action == "read":
            key = winreg.OpenKey(hive, path)
            try:
                value, vtype = winreg.QueryValueEx(key, name)
            finally:
                winreg.CloseKey(key)
            return _ok(action="registry.read", path=path, name=name, value=value,
                       type=vtype)
        if action == "list":
            key = winreg.OpenKey(hive, path)
            values, subkeys = [], []
            try:
                i = 0
                while True:
                    try:
                        vname, vvalue, vtype = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    values.append({"name": vname, "value": vvalue, "type": vtype})
                    i += 1
                i = 0
                while True:
                    try:
                        subkeys.append(winreg.EnumKey(key, i))
                    except OSError:
                        break
                    i += 1
            finally:
                winreg.CloseKey(key)
            return _ok(action="registry.list", path=path, count=len(values),
                       values=values[:500], subkeys=subkeys[:500])
        if not _truthy(body.get("confirm")):
            return _fail(f"registry.{action} needs confirm=true", needs_approval=True)
        if action == "write":
            if not name:
                return _fail("name is required")
            value = body.get("value")
            kind = str(body.get("type") or "string").lower().strip()
            if kind not in _REG_TYPES:
                return _fail("type must be string, expand, dword, qword, multi, or binary")
            if kind in ("dword", "qword"):
                value = int(value)
            elif kind == "multi":
                value = value if isinstance(value, list) else [str(value)]
            elif kind == "binary":
                if isinstance(value, str):
                    value = bytes.fromhex(value.replace(" ", ""))
            key = winreg.CreateKeyEx(hive, path, 0, winreg.KEY_SET_VALUE)
            try:
                winreg.SetValueEx(key, name, 0, _REG_TYPES[kind], value)
            finally:
                winreg.CloseKey(key)
            return _ok(action="registry.write", path=path, name=name, type=kind)
        if action == "delete":
            if name:
                key = winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE)
                try:
                    winreg.DeleteValue(key, name)
                finally:
                    winreg.CloseKey(key)
                return _ok(action="registry.delete", path=path, name=name)
            winreg.DeleteKey(hive, path)
            return _ok(action="registry.delete", path=path, deleted_key=True)
        return _fail(f'unknown registry action "{action}"')
    except FileNotFoundError:
        return _fail("registry key or value not found")
    except PermissionError as e:
        return _fail(str(e), blocked=True)
    except Exception as e:
        return _fail(f"registry action failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- files extras
def op_file_hash(body: dict) -> dict:
    require("read", "file_hash")
    import hashlib
    path = Path(str(body.get("path") or "").strip()).expanduser()
    denied = _deny_path(path)
    if denied:
        return _fail(denied, blocked=True)
    if not path.is_file():
        return _fail(f"file not found: {path}")
    algo = str(body.get("algorithm") or "sha256").lower()
    if algo not in hashlib.algorithms_available:
        return _fail(f"unsupported algorithm: {algo}")
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return _ok(action="file_hash", path=str(path), algorithm=algo,
               hash=h.hexdigest(), size_bytes=path.stat().st_size)


def op_file_zip(body: dict) -> dict:
    require("write", "file_zip")
    raw = body.get("paths") or body.get("path") or body.get("source")
    sources = raw if isinstance(raw, list) else [raw]
    sources = [Path(str(p)).expanduser() for p in sources if str(p or "").strip()]
    if not sources:
        return _fail("paths is required")
    for src in sources:
        denied = _deny_path(src)
        if denied:
            return _fail(denied, blocked=True)
        if not src.exists():
            return _fail(f"source not found: {src}")
    dest = Path(str(body.get("destination") or body.get("dest") or "").strip()).expanduser()
    if not dest.suffix:
        dest = dest / (sources[0].stem + ".zip")
    denied = _deny_path(dest)
    if denied:
        return _fail(denied, blocked=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for src in sources:
            if src.is_dir():
                for p in src.rglob("*"):
                    if p.is_file():
                        z.write(p, p.relative_to(src.parent))
            else:
                z.write(src, src.name)
    return _ok(action="file_zip", destination=str(dest), sources=[str(s) for s in sources],
               size_bytes=dest.stat().st_size)


def op_file_unzip(body: dict) -> dict:
    require("write", "file_unzip")
    src = Path(str(body.get("path") or body.get("archive") or "").strip()).expanduser()
    denied = _deny_path(src)
    if denied:
        return _fail(denied, blocked=True)
    if not src.is_file():
        return _fail(f"archive not found: {src}")
    dest = Path(str(body.get("destination") or body.get("dest") or src.with_suffix("")).strip()).expanduser()
    denied = _deny_path(dest)
    if denied:
        return _fail(denied, blocked=True)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    count = 0
    with zipfile.ZipFile(src) as z:
        for member in z.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(root)):
                return _fail(f"zip entry escapes destination: {member.filename}", blocked=True)
            z.extract(member, dest)
            count += 1
    return _ok(action="file_unzip", archive=str(src), destination=str(dest), entries=count)


def op_file_permissions(body: dict) -> dict:
    require("read", "file_permissions")
    path = Path(str(body.get("path") or "").strip()).expanduser()
    denied = _deny_path(path)
    if denied:
        return _fail(denied, blocked=True)
    if not path.exists():
        return _fail(f"path not found: {path}")
    try:
        code, out, err = _run_cmd(["icacls", str(path)], timeout=45)
    except Exception as e:
        return _fail(f"icacls failed: {e}")
    return _ok(action="file_permissions", path=str(path), exit_code=code,
               output=out[-8000:], stderr=err[-1000:])


def op_file_owner(body: dict) -> dict:
    require("read", "file_owner")
    path = Path(str(body.get("path") or "").strip()).expanduser()
    denied = _deny_path(path)
    if denied:
        return _fail(denied, blocked=True)
    if not path.exists():
        return _fail(f"path not found: {path}")
    try:
        rows = _ps_list(
            f"(Get-Acl -LiteralPath {_ps_quote(str(path))}) | "
            "Select-Object Owner,Group,@{n='Access';e={$_.Access | ForEach-Object {"
            "$_.IdentityReference.Value + ':' + $_.FileSystemRights + ':' + $_.AccessControlType}}} | "
            "ConvertTo-Json -Depth 4 -Compress", timeout=45)
    except Exception as e:
        return _fail(f"owner query failed: {e}")
    return _ok(action="file_owner", path=str(path), acl=rows[0] if rows else None)


def op_search_content(body: dict) -> dict:
    body = dict(body)
    body["content"] = body.get("content") or body.get("query") or body.get("text") or ""
    if not body["content"]:
        return _fail("content or query is required")
    return op_file_search(body)


def op_duplicates(body: dict) -> dict:
    require("read", "duplicates")
    import hashlib
    root = Path(str(body.get("path") or ".")).expanduser()
    if not root.is_dir():
        return _fail(f"directory not found: {root}")
    min_size = max(1, int(body.get("min_size", 1024) or 1024))
    limit = max(1, min(int(body.get("limit", 200) or 200), 2000))
    by_size = {}
    scanned = 0
    for p in root.rglob("*"):
        if scanned >= limit * 50:
            break
        try:
            if not p.is_file():
                continue
            scanned += 1
            size = p.stat().st_size
            if size < min_size:
                continue
            by_size.setdefault(size, []).append(p)
        except Exception:
            continue
    groups = []
    for size, files in by_size.items():
        if len(files) < 2:
            continue
        by_hash = {}
        for p in files[:40]:
            try:
                h = hashlib.sha256()
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                by_hash.setdefault(h.hexdigest(), []).append(str(p))
            except Exception:
                continue
        for digest, paths in by_hash.items():
            if len(paths) > 1:
                groups.append({"sha256": digest, "size_bytes": size,
                               "count": len(paths), "paths": paths})
    groups.sort(key=lambda g: g["size_bytes"] * (g["count"] - 1), reverse=True)
    wasted = sum(g["size_bytes"] * (g["count"] - 1) for g in groups)
    return _ok(action="duplicates", root=str(root), scanned=scanned,
               groups=groups[:limit], group_count=len(groups),
               wasted_bytes=wasted, wasted_mb=round(wasted / 1e6, 1))


def op_large_files(body: dict) -> dict:
    require("read", "large_files")
    root = Path(str(body.get("path") or ".")).expanduser()
    if not root.is_dir():
        return _fail(f"directory not found: {root}")
    raw_min = body.get("min_mb")
    min_mb = 50.0 if raw_min in (None, "") else max(0.0, float(raw_min))
    limit = max(1, min(int(body.get("limit", 50) or 50), 500))
    files = []
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
            size = p.stat().st_size
            if size >= min_mb * 1e6:
                files.append({"path": str(p), "size_bytes": size,
                              "size_mb": round(size / 1e6, 1),
                              "modified": datetime.fromtimestamp(
                                  p.stat().st_mtime, timezone.utc).isoformat()})
        except Exception:
            continue
    files.sort(key=lambda x: x["size_bytes"], reverse=True)
    return _ok(action="large_files", root=str(root), min_mb=min_mb,
               count=len(files), files=files[:limit])


def op_share_list(body: dict) -> dict:
    require("read", "share_list")
    try:
        rows = _ps_list(
            "Get-SmbShare -ErrorAction SilentlyContinue | "
            "Select-Object Name,Path,Description,CurrentUsers,ShareType | "
            "ConvertTo-Json -Depth 3 -Compress", timeout=45)
    except Exception as e:
        return _fail(f"share list failed: {e}")
    return _ok(action="share_list", count=len(rows), shares=rows)


def op_share_create(body: dict) -> dict:
    require("full", "share_create")
    if not _truthy(body.get("confirm")):
        return _fail("share_create needs confirm=true", needs_approval=True)
    name = str(body.get("name") or "").strip()
    path = Path(str(body.get("path") or "").strip()).expanduser()
    if not name or not path:
        return _fail("name and path are required")
    denied = _deny_path(path)
    if denied:
        return _fail(denied, blocked=True)
    if not path.is_dir():
        return _fail(f"folder not found: {path}")
    access = str(body.get("full_access") or body.get("account") or "Everyone").strip()
    script = (
        f"New-SmbShare -Name {_ps_quote(name)} -Path {_ps_quote(str(path))} "
        f"-FullAccess {_ps_quote(access)} -ErrorAction Stop | Out-Null; 'created'"
    )
    try:
        out = _ps_text(script, timeout=60)
    except Exception as e:
        return _fail(f"share create failed: {e}")
    return _ok(action="share_create", name=name, path=str(path), access=access, result=out)


# --------------------------------------------------------------------------- apps extras
def op_apps(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    if action == "installed":
        return op_installed_apps(body)
    if action == "open_application":
        return op_open_application(body)
    if action == "close_application":
        return op_close_application(body)
    if action == "focus_window":
        return op_focus_window(body)
    if action == "list_windows":
        return op_list_windows(body)
    if action == "minimize_window":
        return op_minimize_window(body)
    if action == "maximize_window":
        return op_maximize_window(body)
    if action == "default_get":
        require("read", "apps.default_get")
        results = {}
        base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
        for ext in (".pdf", ".txt", ".jpg", ".png", ".mp4", ".docx", ".xlsx"):
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                     base + "\\" + ext + "\\UserChoice")
                results[ext] = winreg.QueryValueEx(key, "ProgId")[0]
                winreg.CloseKey(key)
            except Exception:
                results[ext] = None
        for proto in ("http", "https", "mailto"):
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
                    "\\" + proto + r"\UserChoice")
                results[proto] = winreg.QueryValueEx(key, "ProgId")[0]
                winreg.CloseKey(key)
            except Exception:
                results[proto] = None
        return _ok(action="apps.default_get", associations=results)
    if action == "uninstall":
        require("full", "apps.uninstall")
        if not _truthy(body.get("confirm")):
            return _fail("uninstall needs confirm=true", needs_approval=True)
        target = str(body.get("id") or body.get("name") or "").strip()
        if not target:
            return _fail("id or name is required")
        if not shutil.which("winget"):
            return _fail("winget is not available; uninstall manually from Settings")
        args = ["winget", "uninstall", "--accept-source-agreements", "--silent"]
        if str(body.get("id") or "").strip():
            args += ["--id", target, "--exact"]
        else:
            args += ["--name", target]
        try:
            code, out, err = _run_cmd(args, timeout=300)
        except Exception as e:
            return _fail(f"winget failed: {e}")
        if code != 0:
            return _fail(f"uninstall failed: {(err or out)[-1200:]}")
        return _ok(action="apps.uninstall", target=target, output=out[-2000:])
    if action == "launch_as_admin":
        require("full", "apps.launch_as_admin")
        import ctypes
        target = str(body.get("path") or body.get("command") or "").strip()
        if not target:
            return _fail("path is required")
        denied = _deny_command(target)
        if denied:
            return _fail(denied, blocked=True)
        params = str(body.get("args") or "").strip()
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", target, params, None, 1)
        if result <= 32:
            return _fail(f"ShellExecute failed with code {result}")
        return _ok(action="apps.launch_as_admin", path=target, args=params or None)
    return _fail(f'unknown apps action "{action}"')


# --------------------------------------------------------------------------- input extras
def op_input(body: dict) -> dict:
    action = str(body.get("action") or "").lower().strip()
    if not action:
        return _fail("action is required")
    if action == "media_key":
        require("write", "input.media_key")
        key = str(body.get("key") or body.get("media") or "play_pause").lower()
        _media_key(key)
        return _ok(action="input.media_key", key=key)
    if action in ("mouse_drag", "mouse_down", "mouse_up"):
        require("write", f"input.{action}")
        try:
            import pyautogui
        except Exception as e:
            return _fail(f"pyautogui unavailable: {e}")
        if action == "mouse_drag":
            start_x = int(body.get("start_x", body.get("x1", 0)) or 0)
            start_y = int(body.get("start_y", body.get("y1", 0)) or 0)
            end_x = int(body.get("end_x", body.get("x2", 0)) or 0)
            end_y = int(body.get("end_y", body.get("y2", 0)) or 0)
            duration = max(0.1, min(float(body.get("duration", 0.5) or 0.5), 10))
            pyautogui.moveTo(start_x, start_y, duration=0.15)
            pyautogui.mouseDown()
            pyautogui.moveTo(end_x, end_y, duration=duration)
            pyautogui.mouseUp()
            return _ok(action="input.mouse_drag", start=[start_x, start_y],
                       end=[end_x, end_y])
        button = str(body.get("button") or "left").lower()
        if action == "mouse_down":
            pyautogui.mouseDown(button=button)
        else:
            pyautogui.mouseUp(button=button)
        return _ok(action=f"input.{action}", button=button)
    if action == "keyboard_type":
        return op_keyboard_type(body)
    if action == "keyboard_shortcut":
        return op_keyboard_shortcut(body)
    if action == "keyboard_press":
        return op_keyboard_press(body)
    if action == "mouse_click":
        return op_mouse_click(body)
    if action == "mouse_move":
        return op_mouse_move(body)
    if action == "mouse_scroll":
        return op_mouse_scroll(body)
    return _fail(f'unknown input action "{action}"')


SYNC_OPS = {
    "system": {
        "screenshot": op_screenshot,
        "screen_record": op_screen_record,
        "get_processes": op_get_processes,
        "kill_process": op_kill_process,
        "system_command": op_system_command,
        "shutdown": op_shutdown,
        "restart": op_restart,
        "sleep": op_sleep,
        "hibernate": op_hibernate,
        "cancel_shutdown": op_cancel_shutdown,
        "lock": op_lock,
        "get_system_info": op_get_system_info,
        "clipboard_get": op_clipboard_get,
        "clipboard_set": op_clipboard_set,
        "volume_get": op_volume_get,
        "volume_set": op_volume_set,
        "mute": op_mute,
        "brightness_get": op_brightness_get,
        "brightness_set": op_brightness_set,
        "service_list": op_service_list,
        "service_control": op_service_control,
        "task_list": op_task_list,
        "task_create": op_task_create,
        "task_delete": op_task_delete,
        "task_run": op_task_run,
        "task_end": op_task_end,
        "audit_log": op_audit_log,
        "ocr": op_ocr,
        "emergency_stop": op_emergency_stop,
        "stop_clear": op_stop_clear,
        "info": op_get_system_info,
        "catalog": op_catalog,
        "battery": op_system_battery,
        "uptime": op_system_uptime,
        "disk": op_system_disk,
        "memory": op_system_memory,
        "cpu": op_system_cpu,
        "gpu": op_system_gpu,
        "process_start": op_process_start,
        "process_details": op_process_details,
        "event_log": op_event_log,
        "installed_apps": op_installed_apps,
        "notification": op_notification,
        "sessions": op_sessions,
        "users": op_users,
        "windows_updates": op_windows_updates,
        "wsl_list": op_wsl_list,
        "wsl_run": op_wsl_run,
        "docker_list": op_docker_list,
        "docker_control": op_docker_control,
    },
    "files": {
        "file_read": op_file_read,
        "file_write": op_file_write,
        "file_append": op_file_append,
        "file_delete": op_file_delete,
        "file_move": op_file_move,
        "file_copy": op_file_copy,
        "file_list": op_file_list,
        "file_search": op_file_search,
        "folder_create": op_folder_create,
        "folder_delete": op_folder_delete,
        "trash_list": op_trash_list,
        "restore": op_restore,
        "undo": op_restore,
        "trash_empty": op_trash_empty,
        "hash": op_file_hash,
        "zip": op_file_zip,
        "unzip": op_file_unzip,
        "permissions": op_file_permissions,
        "owner": op_file_owner,
        "search_content": op_search_content,
        "duplicates": op_duplicates,
        "large_files": op_large_files,
        "share_list": op_share_list,
        "share_create": op_share_create,
    },
    "apps": {
        "open_application": op_open_application,
        "close_application": op_close_application,
        "focus_window": op_focus_window,
        "list_windows": op_list_windows,
        "minimize_window": op_minimize_window,
        "maximize_window": op_maximize_window,
        "installed": op_installed_apps,
        "uninstall": op_apps,
        "default_get": op_apps,
        "launch_as_admin": op_apps,
    },
    "input": {
        "keyboard_type": op_keyboard_type,
        "keyboard_shortcut": op_keyboard_shortcut,
        "keyboard_press": op_keyboard_press,
        "mouse_click": op_mouse_click,
        "mouse_move": op_mouse_move,
        "mouse_scroll": op_mouse_scroll,
        "mouse_drag": op_input,
        "mouse_down": op_input,
        "mouse_up": op_input,
        "media_key": op_input,
    },
    "windows": {
        "list": op_windows,
        "focus": op_windows,
        "minimize": op_windows,
        "maximize": op_windows,
        "restore": op_windows,
        "close": op_windows,
        "move": op_windows,
        "resize": op_windows,
        "always_on_top": op_windows,
        "snap": op_windows,
    },
    "network": {
        "interfaces": op_network,
        "ip": op_network,
        "wifi_status": op_network,
        "wifi_list": op_network,
        "wifi_connect": op_network,
        "wifi_disconnect": op_network,
        "ping": op_network,
        "dns": op_network,
        "public_ip": op_network,
        "ports": op_network,
        "connections": op_network,
        "vpn_list": op_network,
        "proxy_get": op_network,
        "proxy_set": op_network,
        "hosts_read": op_network,
        "hosts_write": op_network,
        "firewall_status": op_network,
        "firewall_rules": op_network,
        "firewall_add": op_network,
        "firewall_remove": op_network,
    },
    "display": {
        "monitors": op_display,
        "resolution_get": op_display,
        "resolution_set": op_display,
        "refresh_rate_set": op_display,
        "wallpaper_get": op_display,
        "wallpaper_set": op_display,
        "theme_get": op_display,
        "theme_set": op_display,
        "notifications_get": op_display,
        "notifications_set": op_display,
        "screen_off": op_display,
    },
    "audio": {
        "devices": op_audio,
        "default_get": op_audio,
        "media_play_pause": op_audio,
        "media_next": op_audio,
        "media_prev": op_audio,
        "media_stop": op_audio,
        "now_playing": op_audio,
    },
    "devices": {
        "bluetooth_status": op_devices,
        "bluetooth_on": op_devices,
        "bluetooth_off": op_devices,
        "bluetooth_devices": op_devices,
        "usb_list": op_devices,
        "printer_list": op_devices,
        "printer_default": op_devices,
        "print_file": op_devices,
        "drivers": op_devices,
        "problem_devices": op_devices,
    },
    "security": {
        "defender_status": op_security,
        "defender_scan": op_security,
        "defender_update": op_security,
        "firewall_status": op_security,
        "firewall_rules": op_security,
        "firewall_add": op_security,
        "firewall_remove": op_security,
        "bitlocker_status": op_security,
        "secure_boot": op_security,
        "tpm": op_security,
        "users": op_security,
        "sessions": op_security,
        "event_log": op_security,
        "windows_updates": op_security,
    },
    "power": {
        "shutdown": op_power,
        "restart": op_power,
        "sleep": op_power,
        "hibernate": op_power,
        "lock": op_power,
        "signout": op_power,
        "cancel_shutdown": op_power,
        "screen_off": op_power,
        "battery": op_power,
        "plans": op_power,
        "plan_set": op_power,
        "last_wake": op_power,
        "requests": op_power,
        "sleep_states": op_power,
    },
    "startup": {
        "list": op_startup,
        "add": op_startup,
        "remove": op_startup,
    },
    "registry": {
        "read": op_registry,
        "list": op_registry,
        "write": op_registry,
        "delete": op_registry,
    },
    "doc": {
        "create_pdf": op_doc_create_pdf,
        "prepare_export_dir": op_doc_prepare_export_dir,
    },
    "code": {
        "read_project": op_code_read_project,
        "analyze_file": op_code_analyze_file,
        "find_in_project": op_code_find_in_project,
        "git_status": op_code_git_status,
        "git_diff": op_code_git_diff,
        "git_log": op_code_git_log,
        "git_commit": op_code_git_commit,
        "git_push": op_code_git_push,
        "run_script": op_code_run_script,
        "install_packages": op_code_install_packages,
        "create_project": op_code_create_project,
    },
}

ASYNC_OPS = {
    "browser": {
        "browser_open": op_browser_open,
        "browser_navigate": op_browser_navigate,
        "browser_click": op_browser_click,
        "browser_type": op_browser_type,
        "browser_extract": op_browser_extract,
        "browser_screenshot": op_browser_screenshot,
        "browser_execute_js": op_browser_execute_js,
        "browser_scroll": op_browser_scroll,
        "browser_close": op_browser_close,
        "browser_get_url": op_browser_get_url,
        "browser_get_title": op_browser_get_title,
        "fill_form": op_browser_fill_form,
        "extract_table": op_browser_extract_table,
        "monitor_page": op_browser_monitor_page,
        "login_and_action": op_browser_login_and_action,
    },
    "code_session": {
        "open_file": op_cs_open_file,
        "read_section": op_cs_read_section,
        "edit_section": op_cs_edit_section,
        "save_file": op_cs_save_file,
        "run_tests": op_cs_run_tests,
    },
    "vision": {
        "analyze": op_vision_analyze,
    },
}

# Tools that must run in the interactive desktop session.
GUI_ACTIONS = {
    "system": {"screenshot", "screen_record", "clipboard_get", "clipboard_set", "lock",
               "volume_get", "volume_set", "mute", "brightness_get", "brightness_set",
               "notification", "installed_apps"},
    "apps": {"open_application", "close_application", "focus_window", "list_windows",
             "minimize_window", "maximize_window", "installed", "uninstall", "default_get",
             "launch_as_admin"},
    "input": {"keyboard_type", "keyboard_shortcut", "keyboard_press", "mouse_click",
              "mouse_move", "mouse_scroll", "mouse_drag", "mouse_down", "mouse_up",
              "media_key"},
    "windows": {"list", "focus", "minimize", "maximize", "restore", "close", "move",
                "resize", "always_on_top", "snap"},
    "display": {"monitors", "resolution_get", "resolution_set", "refresh_rate_set",
                "wallpaper_get", "wallpaper_set", "theme_get", "theme_set",
                "notifications_get", "notifications_set", "screen_off"},
    "audio": {"devices", "default_get", "media_play_pause", "media_next", "media_prev",
              "media_stop", "now_playing"},
    "devices": {"bluetooth_status", "bluetooth_devices", "usb_list", "printer_list",
                "printer_default", "print_file"},
    "power": {"signout", "screen_off", "lock"},
    "startup": {"list", "add", "remove"},
    "registry": {"read", "list", "write", "delete"},
    "browser": {"browser_open", "browser_navigate", "browser_click", "browser_type",
                "browser_extract", "browser_screenshot", "browser_execute_js",
                "browser_scroll", "browser_close", "browser_get_url", "browser_get_title",
                "fill_form", "extract_table", "monitor_page", "login_and_action"},
}


# Friendly aliases so the model can say `shell` or `browser.open` and still reach the real op.
ACTION_ALIASES = {
    "system": {
        "shell": "system_command", "run": "system_command", "exec": "system_command",
        "powershell": "system_command", "cmd": "system_command",
        "processes": "get_processes", "ps": "get_processes", "kill": "kill_process",
        "clipboard": "clipboard_get", "copy": "clipboard_set",
        "sysinfo": "get_system_info", "services": "service_list",
        "scheduled_tasks": "task_list", "tasks": "task_list",
        "apps": "installed_apps", "notifications": "notification",
        "screenrecord": "screen_record", "wsl": "wsl_list", "docker": "docker_list",
        "audit": "audit_log", "stop": "emergency_stop",
    },
    "browser": {
        "open": "browser_open", "navigate": "browser_navigate", "goto": "browser_navigate",
        "click": "browser_click", "type": "browser_type", "extract": "browser_extract",
        "screenshot": "browser_screenshot", "execute_js": "browser_execute_js",
        "js": "browser_execute_js", "scroll": "browser_scroll", "close": "browser_close",
        "get_url": "browser_get_url", "url": "browser_get_url",
        "get_title": "browser_get_title", "title": "browser_get_title",
    },
    "apps": {
        "open": "open_application", "close": "close_application", "focus": "focus_window",
        "list": "list_windows", "minimize": "minimize_window", "maximize": "maximize_window",
    },
    "files": {
        "read": "file_read", "write": "file_write", "append": "file_append",
        "delete": "file_delete", "move": "file_move", "copy": "file_copy",
        "list": "file_list", "search": "file_search", "mkdir": "folder_create",
        "rmdir": "folder_delete", "trash": "trash_list", "restore": "restore", "undo": "undo",
    },
    "network": {"wifi": "wifi_status", "publicip": "public_ip"},
    "power": {"off": "shutdown", "reboot": "restart"},
    "display": {"resolution": "resolution_get", "wallpaper": "wallpaper_get", "theme": "theme_get"},
    "audio": {"volume": "volume_get"},
    "security": {"defender": "defender_status", "bitlocker": "bitlocker_status"},
    "devices": {"bluetooth": "bluetooth_status", "usb": "usb_list", "printers": "printer_list"},
}


def _needs_desktop(category: str, action: str, body: dict | None = None) -> bool:
    if category == "registry":
        hive = str((body or {}).get("hive") or "HKCU").upper().strip()
        return hive not in ("HKLM", "HKEY_LOCAL_MACHINE")
    if category == "startup":
        scope = str((body or {}).get("scope") or "user").lower().strip()
        return scope != "machine"
    return action in GUI_ACTIONS.get(category, set())


async def _proxy_to_desktop(category: str, body: dict, timeout: float) -> dict:
    port = int(CFG.get("desktop_port", 8766))
    url = f"http://127.0.0.1:{port}/tools/{category}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json=body, headers={"X-API-Key": API_KEY})
            try:
                data = r.json()
            except Exception:
                data = {"ok": False, "error": r.text[:800]}
            data.setdefault("via", "desktop")
            return data
    except Exception as e:
        return _fail(
            "desktop helper is not reachable on port " + str(port) + " — GUI actions need an interactive session. "
            f"Start it with: python main.py --desktop ({e})"
        )


# =========================================================================== OTHVM restructure: db + workflows tools
# Consolidated database tool (db) and workflow tool (workflows).
# db merges: Long-Term Memory CRUD/DDL + DB Admin (Management API/Storage/Functions) + Context.
# workflows merges: Briefs + Workflow Builder + n8n API + Modes.
# Memory writes are verified by reading back; failures retry once then alert via Telegram.

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VERSIONED_TABLES = {"core_context", "othman_memory", "othvm_memory"}
_VERIFY_TABLES = {"core_context", "othman_memory", "othvm_memory", "briefs_registry"}
_DB_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "MERGE", "COPY", "ALTER", "CREATE", "GRANT", "REVOKE")
_SAFE_COL_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(\([0-9, ]+\))?(\[\])?$")
_N8N_BASE = str(CFG.get("n8n_base") or "http://localhost:5678").rstrip("/")
_N8N_API_KEY = str(CFG.get("n8n_api_key") or "")
_N8N_MCP_KEY = str(CFG.get("n8n_mcp_key") or "")


def _arg_str(body: dict, *keys, default: str = "") -> str:
    for k in keys:
        v = body.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s != "":
            return s
    return default


def _truthy(v: Any) -> bool:
    if v is True or v == 1:
        return True
    return str(v if v is not None else "").strip().lower() in (
        "true", "1", "yes", "y", "نعم", "موافق", "أكد", "اكد", "confirmed", "confirm")


def _json_arg(v: Any, default: Any = None) -> Any:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    s = str(v).strip()
    if s == "":
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _q_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sql_lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    return "'" + str(v).replace("'", "''") + "'"


def _db_filter_query(filter_value: Any) -> str:
    """Convert a filter (REST string or dict) into a PostgREST query fragment."""
    if isinstance(filter_value, str) and filter_value.strip():
        f = filter_value.strip()
        return f[1:] if f.startswith("?") else f
    if isinstance(filter_value, dict):
        operators = ("eq.", "neq.", "gt.", "gte.", "lt.", "lte.", "like.", "ilike.", "in.",
                     "is.", "not.", "fts.", "plfts.", "phfts.", "wfts.", "cs.", "cd.", "ov.",
                     "sl.", "sr.", "nxl.", "nxr.", "adj.", "match.", "imatch.")
        parts = []
        for k, v in filter_value.items():
            if v is None or str(v) == "":
                continue
            if not _IDENT_RE.match(str(k)):
                continue
            if isinstance(v, bool):
                sval = "true" if v else "false"
            else:
                sval = str(v)
            if not any(sval.startswith(p) for p in operators):
                sval = "eq." + sval
            parts.append(quote(str(k), safe="") + "=" + quote(sval, safe=""))
        return "&".join(parts)
    return ""


async def _db_rest(method: str, table: str, query: str = "", payload: Any = None,
                   prefer: str = "", timeout: int = 45) -> tuple[int, Any]:
    url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/{table}"
    if query:
        url += "?" + query
    headers = dict(_sb_headers())
    if prefer:
        headers["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, url, headers=headers,
                            json=payload if payload is not None else None)
    try:
        data = r.json()
    except Exception:
        data = None
    return r.status_code, data


async def _db_sql(sql: str) -> dict:
    """Execute SQL via the project's exec_sql RPC. Returns the raw RPC dict."""
    q = re.sub(r"\s+", " ", str(sql or "")).strip()
    if not q:
        return {"ok": False, "error": "SQL is empty"}
    try:
        res = await sb_rpc("exec_sql", {"query": q})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if isinstance(res, dict):
        return res
    return {"ok": False, "error": "exec_sql returned an unexpected payload"}


def _db_sql_rows(res: dict) -> list:
    rows = res.get("rows") if isinstance(res, dict) else None
    return rows if isinstance(rows, list) else []


def _db_sql_error(res: dict) -> str:
    if not isinstance(res, dict):
        return "unknown SQL error"
    parts = []
    for k in ("error", "message", "detail", "hint"):
        v = res.get(k)
        if v:
            parts.append(str(v))
    return " — ".join(parts) if parts else json.dumps(res, ensure_ascii=False)[:400]


def _db_first_keyword(sql: str) -> str:
    s = str(sql or "")
    s = re.sub(r"^\s*(--[^\n]*\n|/\*[\s\S]*?\*/|\s)+", "", s).strip()
    m = re.match(r"^([A-Za-z]+)", s)
    return (m.group(1) if m else "").upper()


def _needs_approval_sql(sql: str) -> tuple[bool, str]:
    kw = _db_first_keyword(sql)
    if kw in _DB_WRITE_KEYWORDS:
        if kw == "ALTER" and not re.search(r"\b(DROP\s+COLUMN|ALTER\s+COLUMN)\b", sql, re.I):
            return False, ""
        return True, f"تنفيذ أمر {kw}"
    if kw in ("DO", "CALL"):
        return True, f"تنفيذ أمر {kw}"
    return False, ""


def _db_table_name(body: dict) -> tuple[str, str]:
    t = _arg_str(body, "table_name", "table", "tableName", "name")
    if not _IDENT_RE.match(t or ""):
        return "", "table_name is missing or invalid (plain identifier like expenses)"
    return t, ""


async def _db_memory_fetch(table: str, data: Any, filter_query: str) -> tuple[dict | None, str]:
    """Locate the existing row for a memory-table update."""
    if isinstance(data, dict) and str(data.get("id") or "").strip():
        q = "id=eq." + quote(str(data["id"]).strip(), safe="")
        rows = await sb_table_get(table, q + "&limit=1")
        return (rows[0] if rows else None), q
    if filter_query:
        rows = await sb_table_get(table, filter_query + "&limit=1")
        return (rows[0] if rows else None), filter_query
    return None, ""


def _cmp_norm(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        if s and s[0] in "[{":
            try:
                return json.loads(s)
            except Exception:
                return s
    return v


def _db_verify_fields(payload: dict, row: dict) -> list[str]:
    skip = {"last_updated", "updated_at", "created_at"}
    mismatch = []
    for k, v in payload.items():
        if k in skip:
            continue
        if k not in row:
            mismatch.append(k)
            continue
        if _cmp_norm(row.get(k)) != _cmp_norm(v):
            mismatch.append(k)
    return mismatch


def _db_memory_label(table: str, payload: Any, row: dict | None) -> str:
    if isinstance(payload, dict):
        for k in ("key", "name", "founder_name"):
            if payload.get(k):
                return f"{table}.{payload[k]}"
        if payload.get("id"):
            return f"{table}.{payload['id']}"
    if row:
        for k in ("key", "name", "founder_name", "id"):
            if row.get(k):
                return f"{table}.{row[k]}"
    return table


async def _db_memory_write(table: str, mode: str, data: Any, filter_query: str,
                           on_conflict: str, expected_version: Any) -> dict:
    """Write to a memory table with version tracking + read-back verification."""
    if isinstance(data, dict):
        rows_in = [data]
    elif isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        rows_in = data
    else:
        return _fail("data مطلوب ويكون كائن أو قائمة كائنات JSON")

    current = None
    key_query = filter_query
    if table in _VERSIONED_TABLES and mode in ("update", "upsert"):
        current, key_query = await _db_memory_fetch(table, data, filter_query)
        if mode == "update" and not current:
            return _fail("لا يوجد صف مطابق في " + table + " — راجع الفلتر أو اقرأ أولاً")
        if mode == "upsert" and current:
            mode = "update"
    if mode == "update" and not key_query:
        return _fail("update يحتاج filter أو id للصف المستهدف")

    if table in _VERSIONED_TABLES and isinstance(rows_in[0], dict):
        row0 = rows_in[0]
        if mode == "update" and current is not None:
            if expected_version not in (None, ""):
                try:
                    if int(current.get("version") or 0) != int(expected_version):
                        return _fail("version mismatch", conflict=True, expected=int(expected_version),
                                     current=current, note="اقرأ الصف من جديد ثم أعد المحاولة بالنسخة الصحيحة")
                except Exception:
                    pass
            row0["version"] = int(current.get("version") or 0) + 1
        elif mode == "insert" and row0.get("version") in (None, ""):
            row0["version"] = 1

    payload: Any = rows_in[0] if len(rows_in) == 1 else rows_in
    method, prefer, query = "POST", "return=representation", ""
    if mode == "update":
        method, query = "PATCH", key_query
    elif mode == "upsert" and on_conflict:
        query = "on_conflict=" + quote(on_conflict, safe="")
        prefer = "resolution=merge-duplicates,return=representation"

    status, response = await _db_rest(method, table, query, payload, prefer)
    if not 200 <= status < 300:
        err = ""
        if isinstance(response, dict):
            err = str(response.get("message") or response.get("error") or response)[:400]
        return _fail(f"write failed ({status}): {err}", operation="db.write", table_name=table, mode=mode)

    written = response if isinstance(response, list) else ([response] if response else [])
    verify_row = written[0] if written else None
    verified = None
    mismatch: list[str] = []
    if table in _VERIFY_TABLES:
        readback_query = key_query
        if verify_row and verify_row.get("id"):
            readback_query = "id=eq." + quote(str(verify_row["id"]), safe="")
        if readback_query:
            for _attempt in (1, 2):
                rows = await sb_table_get(table, readback_query + "&limit=1")
                verify_row = rows[0] if rows else None
                mismatch = _db_verify_fields(payload if isinstance(payload, dict) else {}, verify_row or {})
                if verify_row is not None and not mismatch:
                    break
                if _attempt == 1:
                    await _db_rest(method, table, query, payload, prefer)
            verified = verify_row is not None and not mismatch
            if not verified:
                label = _db_memory_label(table, payload, verify_row)
                await telegram_send(f"⚠️ فشل حفظ الذاكرة — {label}")

    out = _ok(operation="db.write", table_name=table, mode=mode,
              row_count=len(written), rows=written[:5],
              version=verify_row.get("version") if verify_row else None)
    if table in _VERIFY_TABLES:
        out["verified"] = bool(verified)
        if mismatch:
            out["mismatch_fields"] = mismatch
            out["note"] = "تمت إعادة المحاولة مرة واحدة. راجع القيم أو أعد القراءة."
    return out


async def _db_read(body: dict) -> dict:
    require("read", "db.read")
    table, err = _db_table_name(body)
    if err:
        return _fail(err)
    filter_query = _db_filter_query(body.get("filter"))
    if _arg_str(body, "id"):
        eq = "id=eq." + quote(_arg_str(body, "id"), safe="")
        filter_query = (filter_query + "&" + eq) if filter_query else eq
    select = _arg_str(body, "select", default="*")
    parts = ["select=" + quote(select, safe="*,().:>-")]
    if filter_query:
        parts.append(filter_query)
    order = _arg_str(body, "order")
    if order:
        parts.append("order=" + quote(order, safe=".,-"))
    limit = body.get("limit")
    if limit not in (None, ""):
        try:
            parts.append("limit=" + str(max(1, min(int(limit), 1000))))
        except Exception:
            pass
    offset = body.get("offset")
    if offset not in (None, ""):
        try:
            parts.append("offset=" + str(max(0, int(offset))))
        except Exception:
            pass
    rows = await sb_table_get(table, "&".join(parts))
    return _ok(operation="db.read", table_name=table, row_count=len(rows), rows=rows)


async def _db_write(body: dict) -> dict:
    require("write", "db.write")
    table, err = _db_table_name(body)
    if err:
        return _fail(err)
    data = _json_arg(body.get("data"))
    if data is None:
        return _fail('"data" مطلوب (كائن JSON أو قائمة صفوف)')
    mode = _arg_str(body, "mode", "write_mode", default="upsert").lower()
    if mode not in ("insert", "upsert", "update"):
        mode = "upsert"
    filter_query = _db_filter_query(body.get("filter"))
    on_conflict = _arg_str(body, "on_conflict")
    expected = body.get("expected_version")
    if table in _VERIFY_TABLES:
        return await _db_memory_write(table, mode, data, filter_query, on_conflict, expected)
    payload: Any = data
    method, prefer, query = "POST", "return=representation", ""
    if mode == "update":
        if not filter_query:
            return _fail("update يحتاج filter أو id")
        eq = "id=eq." + quote(_arg_str(body, "id"), safe="") if _arg_str(body, "id") else ""
        if eq:
            filter_query = (filter_query + "&" + eq) if filter_query else eq
        method, query = "PATCH", filter_query
    elif mode == "upsert" and on_conflict:
        query = "on_conflict=" + quote(on_conflict, safe="")
        prefer = "resolution=merge-duplicates,return=representation"
    status, response = await _db_rest(method, table, query, payload, prefer)
    if not 200 <= status < 300:
        msg = ""
        if isinstance(response, dict):
            msg = str(response.get("message") or response.get("error") or response)[:400]
        return _fail(f"write failed ({status}): {msg}", operation="db.write", table_name=table, mode=mode)
    written = response if isinstance(response, list) else ([response] if response else [])
    return _ok(operation="db.write", table_name=table, mode=mode,
               row_count=len(written), rows=written[:5])


async def _db_delete(body: dict) -> dict:
    require("full", "db.delete")
    table, err = _db_table_name(body)
    if err:
        return _fail(err)
    filter_query = _db_filter_query(body.get("filter"))
    if _arg_str(body, "id"):
        eq = "id=eq." + quote(_arg_str(body, "id"), safe="")
        filter_query = (filter_query + "&" + eq) if filter_query else eq
    if not filter_query:
        return _fail("الرفض: delete يحتاج filter صريح حتى لا تُحذف كل الصفوف")
    status, response = await _db_rest("DELETE", table, filter_query, None, "return=representation")
    if not 200 <= status < 300:
        msg = ""
        if isinstance(response, dict):
            msg = str(response.get("message") or response.get("error") or response)[:400]
        return _fail(f"delete failed ({status}): {msg}", operation="db.delete", table_name=table)
    deleted = response if isinstance(response, list) else ([response] if response else [])
    return _ok(operation="db.delete", table_name=table, deleted_count=len(deleted), deleted=deleted[:20])


async def _db_query(body: dict) -> dict:
    require("read", "db.query")
    sql = _arg_str(body, "sql", "query")
    if not sql:
        return _fail("sql مطلوب")
    body_sql = sql.replace(";", " ; ").strip()
    statements = [s.strip() for s in body_sql.split(";") if s.strip()]
    if len(statements) > 1:
        return _fail("db.query يقبل جملة SELECT واحدة فقط (بدون ;)")
    kw = _db_first_keyword(sql)
    if kw not in ("SELECT", "WITH", "SHOW", "EXPLAIN", "TABLE", "VALUES"):
        return _fail("db.query للقراءة فقط. استخدم db.execute للأوامر المعدِّلة.")
    if kw == "WITH" and re.search(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", sql, re.I):
        return _fail("db.query للقراءة فقط. استخدم db.execute.")
    res = await _db_sql(sql)
    if not res.get("ok"):
        return _fail(_db_sql_error(res), operation="db.query", status=res.get("status"))
    rows = _db_sql_rows(res)
    return _ok(operation="db.query", row_count=len(rows), rows=rows[:500])


async def _db_execute(body: dict) -> dict:
    require("full", "db.execute")
    sql = _arg_str(body, "sql", "query")
    if not sql:
        return _fail("sql مطلوب")
    need, why = _needs_approval_sql(sql)
    if need and not _truthy(body.get("confirm")):
        return _fail("هذه العملية تحتاج تأكيد المؤسس أولاً (" + why + "). اسأله سؤالاً واحداً قصيراً، وبعد موافقته أعد الاستدعاء مع confirm=true.",
                     needs_approval=True)
    res = await _db_sql(sql)
    if not res.get("ok"):
        return _fail(_db_sql_error(res), operation="db.execute", status=res.get("status"))
    rows = _db_sql_rows(res)
    return _ok(operation="db.execute", mode=res.get("mode") or "exec",
               row_count=len(rows), rows=rows[:200])


async def _db_schema_list(body: dict) -> dict:
    require("read", "db.schema.list")
    sql = ("SELECT c.relname AS table_name,"
           " (SELECT count(*) FROM information_schema.columns col WHERE col.table_schema='public' AND col.table_name=c.relname)::int AS columns,"
           " c.relrowsecurity AS rls, coalesce(s.n_live_tup,0)::bigint AS approx_rows,"
           " obj_description(c.oid) AS comment"
           " FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
           " LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid"
           " WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname")
    res = await _db_sql(sql)
    if not res.get("ok"):
        return _fail(_db_sql_error(res), operation="db.schema.list")
    tables = _db_sql_rows(res)
    if _truthy(body.get("exact")):
        for row in tables:
            r = await _db_sql("SELECT count(*)::bigint AS n FROM public." + _q_ident(row.get("table_name")))
            if r.get("ok") and _db_sql_rows(r):
                row["approx_rows"] = _db_sql_rows(r)[0].get("n")
    return _ok(operation="db.schema.list", table_count=len(tables), tables=tables)


async def _db_schema_describe(body: dict) -> dict:
    require("read", "db.schema.describe")
    table, err = _db_table_name(body)
    if err:
        return _fail(err)
    esc = table.replace("'", "''")
    cols = await _db_sql(
        "SELECT column_name AS name, data_type AS type, is_nullable AS nullable,"
        " column_default AS default_value, character_maximum_length AS max_length"
        " FROM information_schema.columns WHERE table_schema='public' AND table_name='" + esc + "'"
        " ORDER BY ordinal_position")
    if not cols.get("ok"):
        return _fail(_db_sql_error(cols), operation="db.schema.describe", table_name=table)
    columns = _db_sql_rows(cols)
    if not columns:
        return _fail('الجدول "' + table + '" غير موجود في schema public', operation="db.schema.describe")
    pk = await _db_sql("SELECT a.attname AS column_name FROM pg_index i"
                       " JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)"
                       " WHERE i.indrelid='public." + _q_ident(table) + "'::regclass AND i.indisprimary")
    fks = await _db_sql(
        "SELECT con.conname AS constraint_name,"
        " (SELECT string_agg(att.attname, ',') FROM unnest(con.conkey) k JOIN pg_attribute att ON att.attrelid=con.conrelid AND att.attnum=k) AS columns,"
        " cnf.relname AS references_table,"
        " (SELECT string_agg(att.attname, ',') FROM unnest(con.confkey) k JOIN pg_attribute att ON att.attrelid=con.confrelid AND att.attnum=k) AS references_columns"
        " FROM pg_constraint con JOIN pg_class cnf ON cnf.oid=con.confrelid"
        " WHERE con.conrelid='public." + _q_ident(table) + "'::regclass AND con.contype='f'")
    idx = await _db_sql("SELECT indexname AS name, indexdef AS definition FROM pg_indexes WHERE schemaname='public' AND tablename='" + esc + "' ORDER BY indexname")
    pol = await _db_sql("SELECT policyname AS name, cmd AS command, roles::text AS roles FROM pg_policies WHERE schemaname='public' AND tablename='" + esc + "'")
    cnt = await _db_sql("SELECT count(*)::bigint AS n FROM public." + _q_ident(table))
    cnt_rows = _db_sql_rows(cnt)
    return _ok(operation="db.schema.describe", table_name=table,
               row_count=(cnt_rows[0].get("n") if cnt_rows else None),
               columns=columns,
               primary_key=[x.get("column_name") for x in _db_sql_rows(pk)],
               foreign_keys=_db_sql_rows(fks), indexes=_db_sql_rows(idx), rls_policies=_db_sql_rows(pol))


def _db_column_alias(t: str) -> str:
    aliases = {"string": "text", "str": "text", "number": "numeric", "num": "numeric",
               "int": "integer", "bool": "boolean", "datetime": "timestamptz",
               "timestamp": "timestamptz", "json": "jsonb", "uuid": "uuid", "date": "date"}
    return aliases.get(str(t or "").lower(), str(t or ""))


def _db_column_def(c: dict) -> tuple[str, str, bool]:
    name = _arg_str(c, "name", "column", "column_name")
    if not _IDENT_RE.match(name or ""):
        return "", f"invalid column name: {name}", False
    ctype = re.sub(r"\s+", " ", _arg_str(c, "type", "data_type", default="text"))
    ctype = _db_column_alias(ctype)
    if not _SAFE_COL_TYPE.match(ctype):
        return "", f"unsupported column type: {ctype}", False
    out = _q_ident(name) + " " + ctype
    has_pk = c.get("primary_key") is True or c.get("pk") is True
    if has_pk:
        out += " PRIMARY KEY"
    if c.get("unique") is True:
        out += " UNIQUE"
    if c.get("not_null") is True or c.get("nullable") is False:
        out += " NOT NULL"
    dv = c.get("default")
    if dv is not None and str(dv).strip() != "":
        dv = str(dv).strip()
        if re.search(r";|--|/\*", dv):
            return "", "invalid default for " + name, False
        bare = bool(re.match(r"^(now\(\)|gen_random_uuid\(\)|true|false|null|-?\d+(\.\d+)?)$", dv, re.I)) or bool(re.match(r"^'.*'$", dv))
        out += " DEFAULT " + (dv if bare else _sql_lit(dv))
    ref = _arg_str(c, "references")
    if ref:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$", ref):
            return "", "invalid references value for " + name, False
        out += " REFERENCES public." + ref
    return out, "", has_pk


async def _db_schema_create(body: dict) -> dict:
    require("full", "db.schema.create")
    table, err = _db_table_name(body)
    if err:
        return _fail(err)
    spec: Any = body.get("columns")
    if isinstance(spec, str) and spec.strip():
        spec_j = _json_arg(spec)
        if isinstance(spec_j, list):
            spec = spec_j
        else:
            frag = spec.strip().rstrip(";")
            if re.search(r";|--|/\*", frag):
                return _fail("column definition must be a single fragment without ; or comments")
            spec = [{"raw": frag}]
    if not isinstance(spec, list):
        spec = []
    defs: list[str] = []
    seen: set[str] = set()
    has_pk = False
    for c in spec:
        if isinstance(c, dict) and c.get("raw"):
            defs.append(str(c["raw"]))
            continue
        if not isinstance(c, dict):
            continue
        d, e, pk = _db_column_def(c)
        if e:
            return _fail(e)
        nm = _arg_str(c, "name", "column", "column_name").lower()
        if nm in seen:
            return _fail("duplicate column: " + nm)
        seen.add(nm)
        has_pk = has_pk or pk
        defs.append(d)
    no_defaults = _truthy(body.get("no_defaults"))
    if not no_defaults:
        if "id" not in seen and not has_pk:
            defs.insert(0, _q_ident("id") + " uuid PRIMARY KEY DEFAULT gen_random_uuid()")
        if "created_at" not in seen:
            defs.append(_q_ident("created_at") + " timestamptz NOT NULL DEFAULT now()")
    if not defs:
        return _fail("columns are required: pass [{name,type}] or a raw definition")
    if_not_exists = "" if body.get("if_not_exists") is False else "IF NOT EXISTS "
    stmts = ["CREATE TABLE " + if_not_exists + "public." + _q_ident(table) + " (\n  " + ",\n  ".join(defs) + "\n);"]
    comment = _arg_str(body, "comment")
    if comment:
        stmts.append("COMMENT ON TABLE public." + _q_ident(table) + " IS " + _sql_lit(comment) + ";")
    stmts.append("ALTER TABLE public." + _q_ident(table) + " ENABLE ROW LEVEL SECURITY;")
    stmts.append("DROP POLICY IF EXISTS " + _q_ident(table + "_service_all") + " ON public." + _q_ident(table) + ";")
    stmts.append("CREATE POLICY " + _q_ident(table + "_service_all") + " ON public." + _q_ident(table) + " FOR ALL TO service_role USING (true) WITH CHECK (true);")
    stmts.append("NOTIFY pgrst, 'reload schema';")
    res = await _db_sql("\n".join(stmts))
    if not res.get("ok"):
        return _fail(_db_sql_error(res), operation="db.schema.create", table_name=table)
    check = await _db_sql("SELECT count(*)::int AS n FROM information_schema.columns WHERE table_schema='public' AND table_name='" + table.replace("'", "''") + "'")
    check_rows = _db_sql_rows(check)
    return _ok(operation="db.schema.create", table_name=table,
               columns_created=(check_rows[0].get("n") if check_rows else len(defs)),
               note="الجدول جاهز. RLS مفعّل وسياسة service_role مضافة.")


async def _db_schema_alter(body: dict) -> dict:
    require("full", "db.schema.alter")
    table, err = _db_table_name(body)
    if err:
        return _fail(err)
    action = _arg_str(body, "action", "alter_action").lower()
    if not action:
        action = "add" if body.get("columns") else ""
    stmts: list[str] = []
    if action in ("add", "addcolumn", "add_column"):
        lst = body.get("columns") if isinstance(body.get("columns"), list) else ([body.get("column")] if body.get("column") else [])
        for c in lst:
            if isinstance(c, str):
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$", c.strip())
                if not m:
                    return _fail("invalid column definition: " + c)
                d, e, _pk = _db_column_def({"name": m.group(1), "type": m.group(2)})
            else:
                d, e, _pk = _db_column_def(c or {})
            if e:
                return _fail(e)
            stmts.append("ALTER TABLE public." + _q_ident(table) + " ADD COLUMN IF NOT EXISTS " + d + ";")
    elif action in ("rename", "renamecolumn", "rename_column"):
        frm = _arg_str(body, "column", "from", "old_name")
        to = _arg_str(body, "new_name", "to")
        if not _IDENT_RE.match(frm) or not _IDENT_RE.match(to):
            return _fail("column and new_name are required")
        stmts.append("ALTER TABLE public." + _q_ident(table) + " RENAME COLUMN " + _q_ident(frm) + " TO " + _q_ident(to) + ";")
    elif action in ("drop", "dropcolumn", "drop_column"):
        if not _truthy(body.get("confirm")):
            return _fail("هذه العملية تحتاج تأكيد المؤسس أولاً (حذف عمود).", needs_approval=True)
        cols = body.get("columns") if isinstance(body.get("columns"), list) else [body.get("column") or body.get("column_name")]
        for c in cols:
            n = _arg_str({"x": c}, "x") if not isinstance(c, str) else c
            if not _IDENT_RE.match(n or ""):
                return _fail("invalid column name: " + str(n))
            stmts.append("ALTER TABLE public." + _q_ident(table) + " DROP COLUMN IF EXISTS " + _q_ident(n) + ";")
    elif action in ("type", "altertype", "alter_type"):
        if not _truthy(body.get("confirm")):
            return _fail("هذه العملية تحتاج تأكيد المؤسس أولاً (تغيير نوع عمود).", needs_approval=True)
        c = _arg_str(body, "column", "column_name")
        t = _arg_str(body, "type", "column_type")
        if not _IDENT_RE.match(c or "") or not _SAFE_COL_TYPE.match(t or ""):
            return _fail("column and type are required")
        stmts.append("ALTER TABLE public." + _q_ident(table) + " ALTER COLUMN " + _q_ident(c) + " TYPE " + t + " USING " + _q_ident(c) + "::" + t + ";")
    elif action in ("default", "setdefault", "set_default"):
        c = _arg_str(body, "column", "column_name")
        if not _IDENT_RE.match(c or ""):
            return _fail("column is required")
        dv = body.get("default_value", body.get("default"))
        if dv is None or str(dv).strip() == "":
            stmts.append("ALTER TABLE public." + _q_ident(table) + " ALTER COLUMN " + _q_ident(c) + " DROP DEFAULT;")
        else:
            stmts.append("ALTER TABLE public." + _q_ident(table) + " ALTER COLUMN " + _q_ident(c) + " SET DEFAULT " + _sql_lit(str(dv)) + ";")
    elif action in ("notnull", "setnotnull", "set_not_null"):
        c = _arg_str(body, "column", "column_name")
        if not _IDENT_RE.match(c or ""):
            return _fail("column is required")
        on = body.get("not_null") is not False
        stmts.append("ALTER TABLE public." + _q_ident(table) + " ALTER COLUMN " + _q_ident(c) + (" SET NOT NULL;" if on else " DROP NOT NULL;"))
    else:
        return _fail("action غير معروف. استخدم: add | rename | drop | type | default | notnull")
    stmts.append("NOTIFY pgrst, 'reload schema';")
    res = await _db_sql("\n".join(stmts))
    if not res.get("ok"):
        return _fail(_db_sql_error(res), operation="db.schema.alter", table_name=table)
    return _ok(operation="db.schema.alter", table_name=table, action=action, statements=len(stmts) - 1)


async def _db_schema_drop(body: dict) -> dict:
    require("full", "db.schema.drop")
    table, err = _db_table_name(body)
    if err:
        return _fail(err)
    if not _truthy(body.get("confirm")):
        return _fail("هذه العملية تحتاج تأكيد المؤسس أولاً (حذف جدول بالكامل).", needs_approval=True)
    cascade = " CASCADE" if _truthy(body.get("cascade")) else ""
    res = await _db_sql("DROP TABLE IF EXISTS public." + _q_ident(table) + cascade + "; NOTIFY pgrst, 'reload schema';")
    if not res.get("ok"):
        return _fail(_db_sql_error(res), operation="db.schema.drop", table_name=table)
    return _ok(operation="db.schema.drop", table_name=table, dropped=True)


def _supabase_ref() -> str:
    m = re.match(r"^https?://([^.]+)\.", str(CFG.get("supabase_url") or ""))
    return m.group(1) if m else ""


_PAT_CACHE: dict = {"value": ""}


async def _supabase_pat() -> str:
    if _PAT_CACHE["value"]:
        return _PAT_CACHE["value"]
    res = await _db_sql("SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'supabase_pat' LIMIT 1")
    rows = _db_sql_rows(res)
    if not res.get("ok") or not rows or not rows[0].get("decrypted_secret"):
        return ""
    _PAT_CACHE["value"] = str(rows[0]["decrypted_secret"])
    return _PAT_CACHE["value"]


async def _db_admin_sql(body: dict) -> dict:
    require("full", "db.admin.runSQL")
    sql = _arg_str(body, "sql", "query")
    if not sql:
        return _fail("sql مطلوب")
    need, why = _needs_approval_sql(sql)
    if need and not _truthy(body.get("confirm")):
        return _fail("هذه العملية تحتاج تأكيد المؤسس أولاً (" + why + ").", needs_approval=True)
    res = await _db_sql(sql)
    if not res.get("ok"):
        return _fail(_db_sql_error(res), operation="db.admin.runSQL", status=res.get("status"))
    rows = _db_sql_rows(res)
    return _ok(operation="db.admin.runSQL", mode=res.get("mode") or "exec",
               row_count=len(rows), rows=rows[:200])


async def _db_admin_schema(body: dict) -> dict:
    require("read", "db.admin.getSchema")
    res = await _db_sql("SELECT table_name, column_name, data_type, is_nullable, column_default"
                        " FROM information_schema.columns WHERE table_schema='public'"
                        " ORDER BY table_name, ordinal_position")
    if not res.get("ok"):
        return _fail(_db_sql_error(res), operation="db.admin.getSchema")
    rows = _db_sql_rows(res)
    tables: dict = {}
    for r in rows:
        tables.setdefault(r.get("table_name"), []).append(r)
    return _ok(operation="db.admin.getSchema", table_count=len(tables),
               tables=[{"table_name": k, "columns": v} for k, v in tables.items()])


async def _db_admin_storage(body: dict, op: str) -> dict:
    require("read" if op in ("listbuckets", "listfiles", "getfileurl") else "write", "db.admin." + op)
    base = CFG["supabase_url"].rstrip("/")
    key = CFG.get("supabase_service_key") or ""
    headers = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            if op == "listbuckets":
                r = await c.get(base + "/storage/v1/bucket", headers=headers)
            elif op == "createbucket":
                name = _arg_str(body, "bucket", "name")
                if not name:
                    return _fail("bucket مطلوب")
                r = await c.post(base + "/storage/v1/bucket", headers=headers,
                                 json={"name": name, "public": _truthy(body.get("public"))})
            elif op == "listfiles":
                bucket = _arg_str(body, "bucket")
                if not bucket:
                    return _fail("bucket مطلوب")
                r = await c.post(base + "/storage/v1/object/list/" + quote(bucket, safe=""),
                                 headers=headers,
                                 json={"prefix": _arg_str(body, "prefix"), "limit": max(1, min(int(body.get("limit") or 100), 1000))})
            elif op == "getfileurl":
                bucket = _arg_str(body, "bucket")
                path = _arg_str(body, "path")
                if not bucket or not path:
                    return _fail("bucket و path مطلوبان")
                expires = max(1, min(int(body.get("expires_in") or 3600), 604800))
                r = await c.post(base + "/storage/v1/object/sign/" + quote(bucket, safe="") + "/" + quote(path, safe="/"),
                                 headers=headers, json={"expires_in": expires})
            else:
                return _fail("unknown storage op")
        data = None
        try:
            data = r.json()
        except Exception:
            data = None
        if r.status_code not in (200, 201):
            msg = data if isinstance(data, (dict, list)) else r.text[:300]
            return _fail(f"storage {op} failed ({r.status_code}): {msg}", operation="db.admin." + op)
        if op == "getfileurl" and isinstance(data, dict) and data.get("signedURL"):
            signed = str(data["signedURL"])
            if not signed.startswith("http"):
                signed = base + "/storage/v1" + ("" if signed.startswith("/") else "/") + signed
            data = {"signedURL": signed, "expires_in": expires}
        return _ok(operation="db.admin." + op, result=data)
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}", operation="db.admin." + op)


async def _db_admin_functions(body: dict, op: str) -> dict:
    require("read" if op == "listfunctions" else "full", "db.admin." + op)
    base = CFG["supabase_url"].rstrip("/")
    key = CFG.get("supabase_service_key") or ""
    try:
        if op == "listfunctions":
            pat = await _supabase_pat()
            if not pat:
                return _fail("supabase_pat غير موجود في Vault — لا يمكن قراءة قائمة الدوال")
            ref = _supabase_ref()
            async with httpx.AsyncClient(timeout=45) as c:
                r = await c.get(f"https://api.supabase.com/v1/projects/{ref}/functions",
                                headers={"Authorization": "Bearer " + pat, "Accept": "application/json"})
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:400]
            if r.status_code not in (200, 201):
                return _fail(f"listFunctions failed ({r.status_code}): {data}", operation="db.admin.listFunctions")
            return _ok(operation="db.admin.listFunctions", result=data)
        name = _arg_str(body, "function_name", "name")
        if not name:
            return _fail("function_name مطلوب")
        payload = _json_arg(body.get("body"), {}) or {}
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(base + "/functions/v1/" + quote(name, safe=""),
                             headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                             json=payload)
        try:
            data = r.json()
        except Exception:
            data = r.text[:600]
        if r.status_code not in (200, 201, 202):
            return _fail(f"invokeFunction failed ({r.status_code}): {data}", operation="db.admin.invokeFunction")
        return _ok(operation="db.admin.invokeFunction", status=r.status_code, result=data)
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}", operation="db.admin." + op)


async def _context_sync(limit: int = 400, full: bool = False) -> dict:
    """Append new n8n agent messages to conversation_log (deduplicated by message_ref)."""
    watermark = "" if full else str(await get_flag("conversation_log_last_at", "") or "")
    con = _sqlite_ro()
    try:
        if watermark:
            rows = con.execute(
                "SELECT id, threadId, role, content, createdAt FROM agents_messages"
                " WHERE createdAt > ? ORDER BY createdAt ASC LIMIT ?",
                (watermark, max(1, min(int(limit), 1000))),
            ).fetchall()
        else:
            rows = list(reversed(con.execute(
                "SELECT id, threadId, role, content, createdAt FROM agents_messages"
                " ORDER BY createdAt DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()))
    finally:
        con.close()
    items = []
    newest = watermark
    for r in rows:
        text = _parse_message_text(r["content"])
        if not text:
            continue
        raw_at = str(r["createdAt"])
        if raw_at > (newest or ""):
            newest = raw_at
        items.append({
            "session_id": str(r["threadId"] or ""),
            "thread_id": str(r["threadId"] or ""),
            "role": str(r["role"] or "user"),
            "content": text[:8000],
            "message_ref": str(r["id"]),
            "created_at": _sqlite_to_iso(raw_at),
            "summarized_at": None,
        })
    inserted = 0
    if items:
        url = f"{CFG['supabase_url'].rstrip('/')}/rest/v1/conversation_log?on_conflict=message_ref"
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"}
        async with httpx.AsyncClient(timeout=90) as c:
            resp = await c.post(url, headers=headers, json=items)
        if resp.status_code in (200, 201, 204):
            inserted = len(items)
    if newest and newest != watermark:
        await set_flag("conversation_log_last_at", newest, by="context_sync")
    return {"synced": len(items), "inserted": inserted, "watermark": newest,
            "mode": "full" if full or not watermark else "incremental"}


async def _context_summarize_log(force: bool = False, min_new: int = 10, max_messages: int = 40) -> dict:
    rows = await sb_table_get("conversation_log", "summarized_at=is.null&order=created_at.asc&limit=1000")
    if not rows:
        return {"skipped": True, "reason": "no new messages", "new_messages": 0}
    if len(rows) < int(min_new) and not force:
        return {"skipped": True, "reason": f"only {len(rows)} new messages (min {min_new})", "new_messages": len(rows)}
    batch = rows[-max(1, min(int(max_messages), 200)):]
    previous = await sb_table_get("conversation_summaries", "select=*&order=created_at.desc&limit=1")
    prev = previous[0] if previous else None
    lines = []
    for m in batch:
        role = "المستخدم" if str(m.get("role")) == "user" else "OTHVM"
        lines.append(f"{role}: {str(m.get('content') or '')[:1800]}")
    transcript = "\n\n".join(lines)
    result = await gemini_summarize(transcript, prev.get("summary") if prev else None)
    thread_id = str(batch[-1].get("thread_id") or CFG.get("main_telegram_thread_id"))
    row = {
        "thread_id": thread_id,
        "agent_id": CFG.get("agent_id"),
        "platform": "telegram",
        "title": (prev or {}).get("title"),
        "message_count": len(batch),
        "first_message_at": batch[0].get("created_at"),
        "last_message_at": batch[-1].get("created_at"),
        "last_message_id": str(batch[-1].get("message_ref") or batch[-1].get("id")),
        "summary": str(result.get("summary") or "")[:8000],
        "topics": result.get("topics") or [],
        "open_items": result.get("open_items") or [],
    }
    await sb_post("conversation_summaries", [row])
    ids = [str(m["id"]) for m in batch if m.get("id")]
    if ids:
        now = datetime.now(timezone.utc).isoformat()
        url = (f"{CFG['supabase_url'].rstrip('/')}/rest/v1/conversation_log"
               f"?id=in.({','.join(ids)})&summarized_at=is.null")
        headers = {**_sb_headers(), "Prefer": "return=minimal"}
        async with httpx.AsyncClient(timeout=60) as c:
            await c.patch(url, headers=headers, json={"summarized_at": now})
    return {"skipped": False, "new_messages": len(rows), "summarized_messages": len(batch),
            "summary": row["summary"], "topics": row["topics"], "open_items": row["open_items"],
            "last_message_at": row["last_message_at"]}


async def op_db(body: dict) -> dict:
    raw = _arg_str(body, "operation", "op", "action", default="read").lower()
    mode_hint = ""
    if raw.startswith("memory.") or raw in ("remember", "recall", "forget", "restore", "consolidate"):
        mem_op = raw.split(".", 1)[1] if raw.startswith("memory.") else raw
        return await op_memory({**body, "operation": mem_op})
    if raw.startswith("knowledge.") or raw in ("knowledge", "vector_search", "semantic_search"):
        knowledge_op = raw.split(".", 1)[1] if raw.startswith("knowledge.") else "search"
        return await op_knowledge({**body, "operation": knowledge_op})
    for prefix in ("db.", "memory.", "supabase_admin.", "database.", "supabase."):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    if raw in ("read", "select", "get", "list", "fetch"):
        op = "read"
    elif raw in ("write", "insert", "upsert", "update", "add", "save",
                 "insertmany", "bulkinsert"):
        mode_hint = "insert" if raw in ("insert", "insertmany", "bulkinsert", "add", "save") else raw
        op = "write"
    elif raw in ("bulkupdate",):
        mode_hint = "update"
        op = "write"
    elif raw in ("delete", "remove", "del"):
        op = "delete"
    elif raw in ("query", "sql_read", "search", "join", "aggregate"):
        op = "query"
    elif raw in ("execute", "exec"):
        op = "execute"
    elif raw in ("count",):
        op = "count"
    elif raw in ("tables", "listtables", "list_tables", "schema.tables", "schema.list"):
        op = "schema.list"
    elif raw in ("describe", "describetable", "describe_table", "schema.describe"):
        op = "schema.describe"
    elif raw in ("createtable", "create_table", "schema.create"):
        op = "schema.create"
    elif raw in ("altertable", "alter_table", "schema.alter"):
        op = "schema.alter"
    elif raw in ("droptable", "drop_table", "schema.drop"):
        op = "schema.drop"
    elif raw in ("runsql", "run_sql", "sql", "admin.runsql", "admin.run_sql"):
        op = "admin.runsql"
    elif raw in ("gettables", "get_tables", "admin.gettables"):
        op = "schema.list"
    elif raw in ("getschema", "get_schema", "admin.getschema"):
        op = "admin.getschema"
    elif raw in ("getcolumns", "get_columns", "admin.getcolumns"):
        op = "schema.describe"
    elif raw.startswith("context.") or raw in ("latest", "history", "threads", "summarize", "sync", "maintain"):
        ctx_op = raw.split(".", 1)[1] if raw.startswith("context.") else raw
        return await op_context({**body, "operation": ctx_op})
    elif raw in ("listbuckets", "admin.listbuckets"):
        return await _db_admin_storage(body, "listbuckets")
    elif raw in ("createbucket", "admin.createbucket"):
        return await _db_admin_storage(body, "createbucket")
    elif raw in ("listfiles", "admin.listfiles"):
        return await _db_admin_storage(body, "listfiles")
    elif raw in ("getfileurl", "admin.getfileurl"):
        return await _db_admin_storage(body, "getfileurl")
    elif raw in ("listfunctions", "admin.listfunctions"):
        return await _db_admin_functions(body, "listfunctions")
    elif raw in ("invokefunction", "admin.invokefunction"):
        return await _db_admin_functions(body, "invokefunction")
    else:
        return _fail(f'عملية غير معروفة "{raw}". المتاح: read | write | delete | query | execute | count | schema.* | context.* | admin.*')

    try:
        if op == "read":
            return await _db_read(body)
        if op == "write":
            if mode_hint and not _arg_str(body, "mode", "write_mode"):
                body = {**body, "mode": mode_hint}
            return await _db_write(body)
        if op == "delete":
            return await _db_delete(body)
        if op == "query":
            if raw in ("join", "aggregate", "search") and not _arg_str(body, "sql", "query"):
                return _fail("هذه العملية تحتاج sql مباشر في النسخة الموحدة")
            return await _db_query(body)
        if op == "execute":
            return await _db_execute(body)
        if op == "count":
            table, err = _db_table_name(body)
            if err:
                return _fail(err)
            n = await sb_table_count(table, _db_filter_query(body.get("filter")))
            return _ok(operation="db.count", table_name=table, count=n)
        if op == "schema.list":
            return await _db_schema_list(body)
        if op == "schema.describe":
            return await _db_schema_describe(body)
        if op == "schema.create":
            return await _db_schema_create(body)
        if op == "schema.alter":
            return await _db_schema_alter(body)
        if op == "schema.drop":
            return await _db_schema_drop(body)
        if op == "admin.runsql":
            return await _db_admin_sql(body)
        if op == "admin.getschema":
            return await _db_admin_schema(body)
        return _fail(f'عملية غير معروفة "{raw}"')
    except PermissionDenied:
        raise
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- workflows tool

async def _wf_api(method: str, path: str, payload: Any = None, timeout: int = 60) -> tuple[int, Any]:
    url = _N8N_BASE + "/api/v1" + path
    headers = {"X-N8N-API-KEY": _N8N_API_KEY, "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, url, headers=headers,
                            json=payload if payload is not None else None)
    try:
        data = r.json()
    except Exception:
        data = r.text[:600]
    return r.status_code, data


async def _wf_mcp(name: str, args: dict, timeout: int = 120) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": args}}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(_N8N_BASE + "/mcp-server/http", json=payload, headers={
                "Authorization": "Bearer " + _N8N_MCP_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            })
        raw = r.text
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if r.status_code < 200 or r.status_code >= 300:
        return {"ok": False, "error": f"MCP HTTP {r.status_code}: {raw[:300]}"}
    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        for line in raw.splitlines():
            if line.startswith("data: "):
                try:
                    cand = json.loads(line[6:])
                    if isinstance(cand, dict) and ("result" in cand or "error" in cand):
                        parsed = cand
                        break
                except Exception:
                    continue
    if not parsed:
        return {"ok": False, "error": "MCP returned an unreadable response"}
    if parsed.get("error"):
        return {"ok": False, "error": str(parsed["error"])[:400]}
    result = parsed.get("result") or {}
    if result.get("isError"):
        content = result.get("content") or []
        msg = "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
        return {"ok": False, "error": msg[:500] or "MCP tool failed"}
    return {"ok": True, "data": result.get("structuredContent") or result}


def _wf_err(status: int, data: Any) -> dict:
    msg = ""
    if isinstance(data, dict):
        msg = str(data.get("message") or data.get("error") or data)[:400]
    else:
        msg = str(data)[:400]
    return _fail(f"n8n API {status}: {msg}")


def _wf_public(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    return {"id": row.get("id"), "name": row.get("name"), "active": row.get("active"),
            "createdAt": row.get("createdAt"), "updatedAt": row.get("updatedAt"),
            "tags": row.get("tags")}


async def _wf_list(body: dict) -> dict:
    limit = max(1, min(int(body.get("limit") or 100), 250))
    path = f"/workflows?limit={limit}"
    if body.get("active") not in (None, ""):
        path += "&active=" + ("true" if _truthy(body.get("active")) else "false")
    if _arg_str(body, "name_filter", "name"):
        path += "&name=" + quote(_arg_str(body, "name_filter", "name"), safe="")
    status, data = await _wf_api("GET", path)
    if status not in (200, 201):
        return _wf_err(status, data)
    items = data.get("data") if isinstance(data, dict) else data
    items = items or []
    return _ok(operation="workflows.list", count=len(items), workflows=[_wf_public(x) for x in items])


async def _wf_get(body: dict) -> dict:
    wid = _arg_str(body, "workflow_id", "id")
    if not wid:
        return _fail("workflow_id مطلوب")
    status, data = await _wf_api("GET", "/workflows/" + quote(wid, safe=""))
    if status not in (200, 201):
        return _wf_err(status, data)
    return _ok(operation="workflows.get", workflow=_wf_public(data), detail=data)


def _wf_settings(settings: Any) -> dict:
    base = {"executionOrder": "v1", "timezone": "Africa/Cairo", "availableInMCP": True}
    if isinstance(settings, dict):
        base.update(settings)
    base["availableInMCP"] = True
    return base


async def _wf_create(body: dict) -> dict:
    name = _arg_str(body, "name")
    if not name:
        return _fail("name مطلوب")
    workflow_json = _json_arg(body.get("workflow_json"))
    if isinstance(workflow_json, dict):
        nodes = workflow_json.get("nodes")
        connections = workflow_json.get("connections")
        settings = workflow_json.get("settings")
    else:
        nodes = _json_arg(body.get("nodes_json"))
        connections = _json_arg(body.get("connections_json"))
        settings = _json_arg(body.get("settings_json"))
    if not isinstance(nodes, list) or not nodes:
        return _fail("workflow_json.nodes (أو nodes_json) مطلوب كمصفوفة غير فارغة")
    if not isinstance(connections, dict):
        connections = {}
    payload = {"name": name, "nodes": nodes, "connections": connections, "settings": _wf_settings(settings)}
    status, data = await _wf_api("POST", "/workflows", payload)
    if status not in (200, 201) or not isinstance(data, dict) or not data.get("id"):
        return _wf_err(status, data)
    wid = data["id"]
    activated = None
    if _truthy(body.get("is_active")):
        st, _ = await _wf_api("POST", f"/workflows/{wid}/activate")
        activated = st in (200, 201)
    try:
        meta = {
            "name": name,
            "n8n_workflow_id": wid,
            "description": _arg_str(body, "description"),
            "trigger_type": _arg_str(body, "trigger_type") or "custom",
            "trigger_config": _json_arg(body.get("trigger_config")),
            "steps": _json_arg(body.get("steps")),
            "is_active": bool(activated) if activated is not None else False,
        }
        await sb_table_upsert("custom_workflows", [meta], on_conflict="n8n_workflow_id")
    except Exception:
        pass
    return _ok(operation="workflows.create", workflow_id=wid, name=name,
               activated=activated, is_active=data.get("active", False))


async def _wf_update(body: dict) -> dict:
    wid = _arg_str(body, "workflow_id", "id")
    if not wid:
        return _fail("workflow_id مطلوب")
    payload: dict = {}
    if _arg_str(body, "name"):
        payload["name"] = _arg_str(body, "name")
    workflow_json = _json_arg(body.get("workflow_json"))
    if isinstance(workflow_json, dict):
        for k in ("nodes", "connections", "settings"):
            if workflow_json.get(k) is not None:
                payload[k] = workflow_json[k]
    for src, dest in (("nodes_json", "nodes"), ("connections_json", "connections"), ("settings_json", "settings")):
        v = _json_arg(body.get(src))
        if v is not None:
            payload[dest] = v
    if not payload:
        return _fail("مفيش حقول للتعديل (name / workflow_json / nodes_json / connections_json / settings_json)")
    if "settings" in payload:
        payload["settings"] = _wf_settings(payload["settings"])
    status, data = await _wf_api("PUT", "/workflows/" + quote(wid, safe=""), payload)
    if status not in (200, 201):
        return _wf_err(status, data)
    return _ok(operation="workflows.update", workflow_id=wid, workflow=_wf_public(data))


async def _wf_set_active(body: dict, active: bool) -> dict:
    wid = _arg_str(body, "workflow_id", "id")
    if not wid:
        return _fail("workflow_id مطلوب")
    action = "activate" if active else "deactivate"
    status, data = await _wf_api("POST", f"/workflows/{quote(wid, safe='')}/{action}")
    if status not in (200, 201):
        return _wf_err(status, data)
    try:
        rows = await sb_table_get("custom_workflows", "n8n_workflow_id=eq." + quote(wid, safe="") + "&select=id")
        if rows:
            await sb_table_patch("custom_workflows", "n8n_workflow_id=eq." + quote(wid, safe=""), {"is_active": active})
    except Exception:
        pass
    return _ok(operation="workflows." + ("resume" if active else "pause"), workflow_id=wid,
               is_active=bool(data.get("active", active)) if isinstance(data, dict) else active)


async def _wf_delete(body: dict) -> dict:
    wid = _arg_str(body, "workflow_id", "id")
    if not wid:
        return _fail("workflow_id مطلوب")
    if not _truthy(body.get("confirm")):
        return _fail("حذف الوركفلو محتاج تأكيد المؤسس. اسأل سؤالاً واحداً قصيراً وبعد الموافقة أعد الاستدعاء مع confirm=true.",
                     needs_approval=True, workflow_id=wid)
    try:
        await _wf_api("POST", f"/workflows/{quote(wid, safe='')}/deactivate")
    except Exception:
        pass
    status, data = await _wf_api("DELETE", "/workflows/" + quote(wid, safe=""))
    if status not in (200, 201, 204):
        return _wf_err(status, data)
    try:
        await sb_table_patch("custom_workflows", "n8n_workflow_id=eq." + quote(wid, safe=""), {"is_active": False})
    except Exception:
        pass
    return _ok(operation="workflows.delete", workflow_id=wid, deleted=True)


async def _wf_run(body: dict) -> dict:
    wid = _arg_str(body, "workflow_id", "id")
    if not wid:
        return _fail("workflow_id مطلوب")
    payload = _json_arg(body.get("payload_json"), {}) or {}
    res = await _wf_mcp("execute_workflow", {"workflowId": wid, "executionMode": "manual",
                                             "inputs": {"webhookData": {"body": payload, "method": "POST"}}} if payload else
                        {"workflowId": wid, "executionMode": "manual"})
    if not res.get("ok"):
        status, data = await _wf_api("POST", f"/workflows/{quote(wid, safe='')}/run", payload or None)
        if status not in (200, 201):
            return _fail("تعذر تشغيل الوركفلو: " + res.get("error", "") + " | " + str(data)[:200])
        return _ok(operation="workflows.run", workflow_id=wid, result=data)
    data = res.get("data") or {}
    exec_id = data.get("executionId") or (data.get("data") or {}).get("executionId")
    return _ok(operation="workflows.run", workflow_id=wid, execution_id=str(exec_id) if exec_id else None)


async def _wf_executions(body: dict, op: str) -> dict:
    limit = max(1, min(int(body.get("limit") or 20), 100))
    if op == "executions.list":
        path = f"/executions?limit={limit}"
        wid = _arg_str(body, "workflow_id")
        if wid:
            path += "&workflowId=" + quote(wid, safe="")
        if _arg_str(body, "status"):
            path += "&status=" + quote(_arg_str(body, "status"), safe="")
        status, data = await _wf_api("GET", path)
        if status not in (200, 201):
            return _wf_err(status, data)
        items = data.get("data") if isinstance(data, dict) else data
        return _ok(operation="workflows.executions.list", count=len(items or []), executions=items or [])
    if op == "executions.get":
        eid = _arg_str(body, "execution_id", "id")
        if not eid:
            return _fail("execution_id مطلوب")
        include = "true" if _truthy(body.get("include_data")) else "false"
        status, data = await _wf_api("GET", f"/executions/{quote(eid, safe='')}?includeData={include}")
        if status not in (200, 201):
            return _wf_err(status, data)
        return _ok(operation="workflows.executions.get", execution=data)
    eid = _arg_str(body, "execution_id", "id")
    if not eid:
        return _fail("execution_id مطلوب")
    status, data = await _wf_api("DELETE", "/executions/" + quote(eid, safe=""))
    if status not in (200, 201, 204):
        return _wf_err(status, data)
    return _ok(operation="workflows.executions.delete", execution_id=eid, deleted=True)


# ------------------------------------------------------------------ briefs (merged from Briefs tool)

_BRIEF_TG_CRED = {"telegramApi": {"id": str(CFG.get("brief_telegram_credential_id") or ""), "name": "Telegram account"}}
_BRIEF_CHAT = str(CFG.get("telegram_chat_id") or "")


def _brief_uid() -> str:
    return "ab" + secrets.token_hex(4) + secrets.token_hex(3)[:4]


def _brief_compose_js(name: str, content: str) -> str:
    return (
        "const now = new Date();"
        "let wd = ''; let dateStr = '';"
        "try {"
        " const TZ = 'Africa/Cairo';"
        " const F = (o) => new Intl.DateTimeFormat('ar-EG-u-nu-latn', Object.assign({ timeZone: TZ }, o)).format(now);"
        " wd = F({ weekday: 'long' });"
        " const dn = F({ day: 'numeric' });"
        " const mo = F({ month: 'long' });"
        " const yr = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, year: 'numeric' }).format(now);"
        " dateStr = dn + ' ' + mo + ' ' + yr;"
        "} catch (e) { dateStr = new Date(now.getTime() + 7200000).toISOString().slice(0, 10); }"
        "let text = " + json.dumps(content, ensure_ascii=False) + ";"
        "text = text.split('{date}').join(dateStr).split('{day}').join(wd);"
        "try {"
        " const sb = String($env.SUPABASE_URL || ''); const k = String($env.SUPABASE_SERVICE_KEY || '');"
        " if (sb && k) { await this.helpers.httpRequest({ method: 'PATCH', url: sb + '/rest/v1/briefs_registry?name=eq.' + encodeURIComponent(" + json.dumps(name, ensure_ascii=False) + "), headers: { apikey: k, Authorization: 'Bearer ' + k, 'Content-Type': 'application/json', Prefer: 'return=minimal' }, body: { last_run: new Date().toISOString(), updated_at: new Date().toISOString() }, json: true, timeout: 15000, ignoreHttpStatusErrors: true, returnFullResponse: true }); }"
        "} catch (e) {}"
        "return [{ json: { text: text } }];"
    )


def _brief_workflow_payload(name: str, content: str, cron: str) -> dict:
    return {
        "name": "Scheduled — Brief: " + name,
        "settings": {"timezone": "Africa/Cairo", "executionOrder": "v1", "availableInMCP": True},
        "nodes": [
            {"parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": cron}]}},
             "id": _brief_uid(), "name": "Schedule", "type": "n8n-nodes-base.scheduleTrigger",
             "typeVersion": 1.2, "position": [0, 0]},
            {"parameters": {"mode": "runOnceForAllItems", "jsCode": _brief_compose_js(name, content)},
             "id": _brief_uid(), "name": "Compose", "type": "n8n-nodes-base.code",
             "typeVersion": 2, "position": [240, 0]},
            {"parameters": {"resource": "message", "operation": "sendMessage", "chatId": _BRIEF_CHAT,
                            "text": "={{ $json.text }}",
                            "additionalFields": {"appendAttribution": False}},
             "id": _brief_uid(), "name": "Send", "type": "n8n-nodes-base.telegram",
             "typeVersion": 1.2, "position": [480, 0], "credentials": _BRIEF_TG_CRED},
        ],
        "connections": {"Schedule": {"main": [[{"node": "Compose", "type": "main", "index": 0}]]},
                        "Compose": {"main": [[{"node": "Send", "type": "main", "index": 0}]]}},
    }


async def _brief_row(name: str) -> dict | None:
    rows = await sb_table_get("briefs_registry", "select=*&name=eq." + quote(name, safe="") + "&limit=1")
    return rows[0] if rows else None


def _brief_is_dynamic(wf: Any) -> bool:
    return isinstance(wf, dict) and str(wf.get("name") or "").startswith("Scheduled — Brief: ")


async def _wf_briefs(body: dict, op: str) -> dict:
    if op == "list":
        rows = await sb_table_get("briefs_registry", "select=*&order=created_at.asc")
        out = [{"name": r.get("name"), "description": r.get("description"),
                "schedule_human": r.get("schedule_human"), "schedule_cron": r.get("schedule_cron"),
                "is_active": r.get("is_active"), "workflow_id": r.get("workflow_id"),
                "last_run": r.get("last_run"), "content_chars": len(str(r.get("content") or ""))} for r in rows]
        return _ok(operation="workflows.briefs.list", count=len(out), briefs=out)

    name = _arg_str(body, "name")
    if not name:
        return _fail("name مطلوب")
    if op == "create":
        dup = await _brief_row(name)
        if dup:
            return _fail("في بريف بنفس الاسم بالفعل: " + name)
        description = _arg_str(body, "description")
        schedule_human = _arg_str(body, "schedule_human")
        cron = _arg_str(body, "schedule_cron")
        content = str(body.get("content") or "").strip()
        explicit_wf = _arg_str(body, "workflow_id")
        workflow_id = explicit_wf
        source = "registered"
        activated = None
        if not explicit_wf:
            if not content:
                return _fail("content مطلوب للبريف الجديد (أو workflow_id لتسجيل وركفلو موجود)")
            if not cron or len(cron.split(" ")) != 5:
                return _fail("schedule_cron مطلوب بصيغة cron خماسية، مثال: 0 20 * * 0")
            payload = _brief_workflow_payload(name, content, cron)
            status, data = await _wf_api("POST", "/workflows", payload)
            if status not in (200, 201) or not isinstance(data, dict) or not data.get("id"):
                return _fail("فشل إنشاء الوركفلو: " + str(status) + " " + str(data)[:200])
            workflow_id = data["id"]
            source = "dynamic"
            st, _ = await _wf_api("POST", f"/workflows/{workflow_id}/activate")
            activated = st in (200, 201)
        try:
            await sb_table_upsert("briefs_registry", [{
                "name": name, "description": description, "content": content,
                "schedule_cron": cron, "schedule_human": schedule_human, "is_active": True,
                "workflow_id": workflow_id or None,
            }], on_conflict="name")
        except Exception as e:
            if source == "dynamic" and workflow_id:
                await _wf_api("DELETE", "/workflows/" + workflow_id)
            return _fail("فشل تسجيل البريف: " + str(e))
        return _ok(operation="workflows.briefs.create", brief={
            "name": name, "description": description, "schedule_cron": cron,
            "schedule_human": schedule_human, "is_active": True,
            "workflow_id": workflow_id or None, "source": source,
            **({"activated": activated} if source == "dynamic" else {})})

    row = await _brief_row(name)
    if not row:
        return _fail("مفيش بريف بالاسم ده: " + name)
    if op == "update":
        patch: dict = {"updated_at": _now_iso()}
        notes: list[str] = []
        if body.get("description") is not None:
            patch["description"] = _arg_str(body, "description")
        if body.get("schedule_human") is not None:
            patch["schedule_human"] = _arg_str(body, "schedule_human")
        if body.get("is_active") is not None:
            patch["is_active"] = _truthy(body.get("is_active"))
        if _arg_str(body, "workflow_id"):
            patch["workflow_id"] = _arg_str(body, "workflow_id")
        wf_id = patch.get("workflow_id") or row.get("workflow_id")
        content = str(body.get("content") or "").strip()
        cron = _arg_str(body, "schedule_cron")
        if content or cron:
            if not wf_id:
                notes.append("مفيش وركفلو مرتبط — تعديل المحتوى/الجدولة اترفض")
            else:
                st, wf = await _wf_api("GET", "/workflows/" + quote(str(wf_id), safe=""))
                if st in (200, 201) and _brief_is_dynamic(wf):
                    new_content = content or str(row.get("content") or "")
                    new_cron = cron or str(row.get("schedule_cron") or "")
                    if not new_content:
                        notes.append("مفيش محتوى محفوظ للبريف ده")
                    else:
                        payload = _brief_workflow_payload(name, new_content, new_cron)
                        ur, _ = await _wf_api("PUT", "/workflows/" + quote(str(wf_id), safe=""), payload)
                        if ur not in (200, 201):
                            notes.append("فشل تحديث الوركفلو: " + str(ur))
                        else:
                            if content:
                                patch["content"] = content
                            patch["schedule_cron"] = new_cron
                else:
                    notes.append("الوركفلو المرتبط مش ديناميكي — المحتوى والجدولة مش بيتعدلوا من هنا")
        try:
            await sb_table_patch("briefs_registry", "name=eq." + quote(name, safe=""), patch)
        except Exception as e:
            return _fail("فشل تحديث البريف: " + str(e))
        return _ok(operation="workflows.briefs.update", name=name, updated=list(patch.keys()),
                   notes=notes or None)

    if op in ("pause", "resume"):
        active = op == "resume"
        notes: list[str] = []
        wf_id = row.get("workflow_id")
        if wf_id:
            st, wf = await _wf_api("GET", "/workflows/" + quote(str(wf_id), safe=""))
            if st in (200, 201) and _brief_is_dynamic(wf):
                rr, _ = await _wf_api("POST", f"/workflows/{quote(str(wf_id), safe='')}/" + ("activate" if active else "deactivate"))
                if rr not in (200, 201):
                    notes.append("تعذر " + ("تشغيل" if active else "إيقاف") + " الوركفلو: " + str(rr))
            else:
                notes.append("الوركفلو مشترك/قديم — الحالة اتسجلت في السجل بس")
        try:
            await sb_table_patch("briefs_registry", "name=eq." + quote(name, safe=""),
                                 {"is_active": active, "updated_at": _now_iso()})
        except Exception as e:
            return _fail(str(e))
        return _ok(operation="workflows.briefs." + op, name=name, is_active=active,
                   workflow_id=wf_id, notes=notes or None)

    if op == "delete":
        if not _truthy(body.get("confirm")):
            return _fail("حذف البريف محتاج تأكيد المؤسس (confirm=true).", needs_approval=True, name=name)
        wf_deleted = False
        notes: list[str] = []
        wf_id = row.get("workflow_id")
        if wf_id:
            st, wf = await _wf_api("GET", "/workflows/" + quote(str(wf_id), safe=""))
            if st in (200, 201) and _brief_is_dynamic(wf):
                await _wf_api("POST", f"/workflows/{quote(str(wf_id), safe='')}/deactivate")
                dl, _ = await _wf_api("DELETE", "/workflows/" + quote(str(wf_id), safe=""))
                wf_deleted = dl in (200, 201, 204)
                if not wf_deleted:
                    notes.append("الوركفلو الديناميكي متمسحش: " + str(dl))
            else:
                notes.append("الوركفلو مشترك/قديم — اتشال التسجيل بس")
        status, data = await _db_rest("DELETE", "briefs_registry", "name=eq." + quote(name, safe=""), None, "return=minimal")
        if status not in (200, 201, 204):
            return _fail(f"فشل حذف التسجيل ({status}): {str(data)[:200]}")
        return _ok(operation="workflows.briefs.delete", deleted=name, workflow_deleted=wf_deleted,
                   notes=notes or None)

    if op == "run":
        wf_id = row.get("workflow_id")
        if not wf_id:
            return _fail("البريف ده مفيش وركفلو مرتبط بيه")
        res = await _wf_mcp("execute_workflow", {"workflowId": str(wf_id), "executionMode": "manual"})
        if not res.get("ok"):
            return _fail("تعذر تشغيل البريف: " + str(res.get("error"))[:300])
        data = res.get("data") or {}
        exec_id = data.get("executionId") or (data.get("data") or {}).get("executionId")
        return _ok(operation="workflows.briefs.run", name=name, workflow_id=wf_id,
                   execution_id=str(exec_id) if exec_id else None)

    return _fail("عملية بريف غير معروفة: " + op)


# ------------------------------------------------------------------ modes (merged from Modes tool)

_MODE_STEP_TYPES = ("app_open", "browser_navigate", "browser_new_tab", "app_close",
                    "app_close_all", "system_command", "message")
_MODE_ALIASES = {"open_app": "app_open", "open_application": "app_open",
                 "navigate": "browser_navigate", "new_tab": "browser_new_tab",
                 "close_app": "app_close", "close_all": "app_close_all",
                 "command": "system_command", "send_message": "message"}


def _mode_normalize_step(step: Any) -> dict | None:
    if not isinstance(step, dict) or isinstance(step, list):
        return None
    raw_type = str(step.get("type") or "").strip().lower()
    out = dict(step)
    out["type"] = _MODE_ALIASES.get(raw_type, raw_type)
    return out


def _mode_parse_steps(raw: Any) -> list | None:
    if isinstance(raw, list):
        return [s for s in (_mode_normalize_step(x) for x in raw) if s]
    if isinstance(raw, str) and raw.strip():
        parsed = _json_arg(raw)
        if isinstance(parsed, list):
            return [s for s in (_mode_normalize_step(x) for x in parsed) if s]
        return None
    return None


def _mode_validate_steps(steps: list) -> str:
    if not steps:
        return "steps must be a non-empty JSON array"
    for i, step in enumerate(steps):
        label = f"step {i + 1}"
        t = step.get("type")
        if t not in _MODE_STEP_TYPES:
            return f'{label} has unsupported type "{t}"'
        if t in ("app_open", "app_close", "app_close_all"):
            if not _arg_str(step, "target", "name", "application"):
                return label + " requires target"
        if t in ("browser_navigate", "browser_new_tab"):
            if not _arg_str(step, "url", "target"):
                return label + " requires url"
        if t == "system_command" and not _arg_str(step, "command"):
            return label + " requires command"
        if t == "message" and not _arg_str(step, "text"):
            return label + " requires text"
    return ""


def _mode_public(row: dict) -> dict:
    return {"id": row.get("id"), "name": row.get("name"), "description": row.get("description") or "",
            "steps": row.get("steps") if isinstance(row.get("steps"), list) else [],
            "is_active": row.get("is_active") is not False, "created_at": row.get("created_at")}


async def _mode_find(body: dict) -> dict | None:
    mid = _arg_str(body, "mode_id")
    if mid:
        rows = await sb_table_get("modes", "select=*&id=eq." + quote(mid, safe=""))
        return rows[0] if rows else None
    name = _arg_str(body, "name")
    if not name:
        return None
    rows = await sb_table_get("modes", "select=*&name=eq." + quote(name, safe=""))
    if rows:
        return rows[0]
    all_rows = await sb_table_get("modes", "select=*")
    return next((r for r in all_rows if str(r.get("name") or "").lower() == name.lower()), None)


async def _mode_run_step(step: dict) -> dict:
    t = step.get("type")
    if t == "app_open":
        target = _arg_str(step, "target", "name", "application")
        try:
            res = await asyncio.to_thread(op_open_application, {"name": target})
            return res
        except Exception as e:
            return _fail(f"{type(e).__name__}: {e}")
    if t in ("browser_navigate", "browser_new_tab"):
        url = _arg_str(step, "url", "target")
        browser = _arg_str(step, "browser", "app", default="chrome") or "chrome"
        try:
            res = await asyncio.to_thread(op_open_application, {"name": browser, "args": [url]})
            return res
        except Exception as e:
            return _fail(f"{type(e).__name__}: {e}")
    if t in ("app_close", "app_close_all"):
        target = _arg_str(step, "target", "name", "application")
        payload = {"title": target} if step.get("by") == "title" else {"name": target}
        try:
            res = await asyncio.to_thread(op_close_application, payload)
            return res
        except Exception as e:
            return _fail(f"{type(e).__name__}: {e}")
    if t == "system_command":
        command = _arg_str(step, "command")
        timeout = max(1, min(int(step.get("timeout") or 60), 300))
        try:
            res = await asyncio.to_thread(op_system_command, {"command": command, "timeout": timeout})
            return res
        except Exception as e:
            return _fail(f"{type(e).__name__}: {e}")
    if t == "message":
        text = _arg_str(step, "text")
        if not text:
            return _fail("message text is required")
        chat_id = _arg_str(step, "chat_id") or None
        sent = await telegram_send_payload({"text": text, "disable_web_page_preview": True}, chat_id)
        return _ok(action="message", sent=sent)
    return _fail("Unsupported step type: " + str(t))


async def _wf_modes(body: dict, op: str) -> dict:
    if op == "list":
        rows = await sb_table_get("modes", "select=*&order=created_at.asc")
        return _ok(operation="workflows.modes.list", count=len(rows), modes=[_mode_public(r) for r in rows])
    if op == "create":
        name = _arg_str(body, "name")
        if not name:
            return _fail("name is required")
        steps = _mode_parse_steps(body.get("steps"))
        if steps is None:
            return _fail("steps must be a JSON array")
        invalid = _mode_validate_steps(steps)
        if invalid:
            return _fail(invalid)
        try:
            status, created = await _db_rest("POST", "modes", "", {
                "name": name, "description": _arg_str(body, "description"),
                "steps": steps, "is_active": True if body.get("is_active") is None else _truthy(body.get("is_active")),
            }, "return=representation")
            if status not in (200, 201):
                msg = str(created)[:300] if created else f"HTTP {status}"
                if re.search(r"duplicate|unique|already exists", msg, re.I):
                    return _fail("A mode with this name already exists.")
                return _fail(msg)
            row0 = created[0] if isinstance(created, list) and created else {}
            return _ok(operation="workflows.modes.create", mode=_mode_public(row0))
        except Exception as e:
            msg = str(e)
            if re.search(r"duplicate|unique|already exists", msg, re.I):
                return _fail("A mode with this name already exists.")
            return _fail(msg)
    if op == "update":
        mode = await _mode_find(body)
        if not mode:
            return _fail("Mode not found. Use workflows.modes.list first.")
        patch: dict = {}
        if body.get("name") is not None:
            name = _arg_str(body, "name")
            if not name:
                return _fail("name cannot be empty")
            patch["name"] = name
        if body.get("description") is not None:
            patch["description"] = _arg_str(body, "description")
        if body.get("is_active") is not None:
            patch["is_active"] = _truthy(body.get("is_active"))
        if body.get("steps") is not None:
            steps = _mode_parse_steps(body.get("steps"))
            if steps is None:
                return _fail("steps must be a JSON array")
            invalid = _mode_validate_steps(steps)
            if invalid:
                return _fail(invalid)
            patch["steps"] = steps
        if not patch:
            return _fail("Provide at least one of name, description, steps, or is_active.")
        try:
            updated = await sb_table_patch("modes", "id=eq." + quote(str(mode.get("id")), safe=""), patch)
            return _ok(operation="workflows.modes.update", mode=_mode_public(updated[0] if updated else mode))
        except Exception as e:
            msg = str(e)
            if re.search(r"duplicate|unique|already exists", msg, re.I):
                return _fail("A mode with this name already exists.")
            return _fail(msg)
    if op == "delete":
        mode = await _mode_find(body)
        if not mode:
            return _fail("Mode not found. Use workflows.modes.list first.")
        if not _truthy(body.get("confirmed", body.get("confirm"))):
            return _fail("Delete requires confirmed=true after founder approval.",
                         needs_approval=True, mode={"id": mode.get("id"), "name": mode.get("name")})
        status, data = await _db_rest("DELETE", "modes", "id=eq." + quote(str(mode.get("id")), safe=""), None, "return=minimal")
        if status not in (200, 201, 204):
            return _fail(f"Supabase delete failed ({status}): {str(data)[:200]}")
        return _ok(operation="workflows.modes.delete", deleted=True,
                   mode={"id": mode.get("id"), "name": mode.get("name")})
    if op == "activate":
        mode = await _mode_find(body)
        if not mode:
            return _fail("Mode not found. Use workflows.modes.list first.")
        if mode.get("is_active") is False:
            return _fail("This mode is paused. Resume it first with workflows.modes.update is_active=true.")
        steps = _mode_parse_steps(mode.get("steps"))
        if steps is None:
            return _fail("Mode steps are invalid JSON.")
        invalid = _mode_validate_steps(steps)
        if invalid:
            return _fail(invalid)
        needs_laptop = any(s.get("type") != "message" for s in steps)
        needs_full = any(s.get("type") == "system_command" for s in steps)
        permission_mode = "not_required"
        if needs_laptop:
            permission_mode = read_mode()
            if permission_mode in ("none", "read"):
                return _fail("This mode needs laptop write access. Current permission mode is " + permission_mode + ".",
                             permission_mode=permission_mode, required_permission="write")
            if needs_full and permission_mode != "full":
                return _fail("This mode contains a system_command step and needs full permission. Current mode is " + permission_mode + ".",
                             permission_mode=permission_mode, required_permission="full")
        results = []
        for i, step in enumerate(steps):
            res = await _mode_run_step(step)
            entry = {"index": i + 1, "type": step.get("type"), "ok": bool(res.get("ok")),
                     "optional": step.get("optional") is True}
            if not res.get("ok"):
                entry["error"] = str(res.get("error"))[:300]
            results.append(entry)
            if not res.get("ok") and step.get("optional") is not True:
                return _fail(str(res.get("error"))[:300], operation="workflows.modes.activate",
                             mode={"id": mode.get("id"), "name": mode.get("name")},
                             permission_mode=permission_mode, failed_step={"index": i + 1, "type": step.get("type")},
                             results=results)
        return _ok(operation="workflows.modes.activate", mode={"id": mode.get("id"), "name": mode.get("name")},
                   permission_mode=permission_mode, steps_run=len(results),
                   message_sent=any(r["type"] == "message" and r["ok"] for r in results), results=results)
    return _fail("عملية مود غير معروفة: " + op)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def op_workflows(body: dict) -> dict:
    raw = _arg_str(body, "operation", "op", "action", default="list").lower()
    for prefix in ("workflows.", "wf.", "builder.", "n8n.", "n8n_api."):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    destructive = {"delete", "remove", "executions.delete", "briefs.delete", "modes.delete"}
    writes = {"create", "create_custom", "update", "modify", "pause", "deactivate", "stop",
              "resume", "activate", "start", "run"}
    if raw in destructive:
        require("full", "workflows." + raw)
    elif raw in writes or raw.startswith(("briefs.create", "briefs.update", "briefs.pause",
                                          "briefs.resume", "briefs.run", "modes.create",
                                          "modes.update", "modes.activate", "modes.deactivate")):
        require("write", "workflows." + raw)
    else:
        require("read", "workflows." + (raw or "list"))
    try:
        if raw == "list":
            return await _wf_list(body)
        if raw == "get":
            return await _wf_get(body)
        if raw in ("create", "create_custom"):
            return await _wf_create(body)
        if raw in ("update", "modify"):
            return await _wf_update(body)
        if raw in ("pause", "deactivate", "stop"):
            return await _wf_set_active(body, False)
        if raw in ("resume", "activate", "start"):
            return await _wf_set_active(body, True)
        if raw in ("delete", "remove"):
            return await _wf_delete(body)
        if raw == "run":
            return await _wf_run(body)
        if raw in ("executions.list", "executions", "list_executions"):
            return await _wf_executions(body, "executions.list")
        if raw == "executions.get":
            return await _wf_executions(body, "executions.get")
        if raw == "executions.delete":
            return await _wf_executions(body, "executions.delete")
        if raw.startswith("briefs."):
            return await _wf_briefs(body, raw[7:])
        if raw.startswith("modes."):
            return await _wf_modes(body, raw[6:])
        if raw in ("briefs",):
            return await _wf_briefs(body, "list")
        if raw in ("modes",):
            return await _wf_modes(body, "list")
        return _fail(f'عملية غير معروفة "{raw}". المتاح: list | get | create | update | pause | resume | delete | run | executions.* | briefs.* | modes.*')
    except Exception as e:
        return _fail(f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- app
app = FastAPI(title="OTHVM Laptop Control MCP", version="1.0.0")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "role": ROLE,
        "mode": read_mode(),
        "pid": os.getpid(),
        "session": _session_id(),
        "interactive": _session_id() != 0,
        "emergency_stop": EMERGENCY_FLAG.exists(),
        "time": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - STARTED_AT),
        "loops_worker": bool(LOOPS_WORKER_TASK and not LOOPS_WORKER_TASK.done()),
        "last_loop_tick": LAST_LOOP_TICK,
        "loop_ticks": LOOP_TICKS,
        "last_loop_error": LAST_LOOP_ERROR,
    }


def _session_id() -> int:
    try:
        import win32ts
        return win32ts.ProcessIdToSessionId(os.getpid())
    except Exception:
        return 0


@app.get("/permission")
async def permission_get(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        return JSONResponse({"ok": False, "error": "invalid api key"}, status_code=401)
    return {"ok": True, "mode": read_mode(), "role": ROLE}


@app.post("/tools/{category}")
async def tool_endpoint(category: str, request: Request, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        return JSONResponse({"ok": False, "error": "invalid api key"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    category = category.lower().strip()

    if EMERGENCY_FLAG.exists() and category not in ("commands", "sleep"):
        emergency_action = str(body.get("action", "")).strip()
        if not (category == "system" and emergency_action in ("emergency_stop", "stop_clear")):
            return JSONResponse({
                "ok": False,
                "error": "Emergency stop is active. Send /resume in Telegram or call system stop_clear first.",
                "emergency_stop": True,
            }, status_code=423)

    if category == "commands":
        started = time.time()
        result = await op_commands(body)
        log_action(category, str(body.get("command")), body,
                   "ok" if result.get("ok") else "error", result.get("error", ""),
                   int((time.time() - started) * 1000))
        return result

    if category == "sleep":
        started = time.time()
        result = await op_sleep_monitor(body)
        log_action(category, str(body.get("action")), body,
                   "ok" if result.get("ok") else "error", result.get("error", ""),
                   int((time.time() - started) * 1000))
        return result

    if category == "reminders":
        started = time.time()
        result = await op_reminders(body)
        log_action(category, str(body.get("action")), body,
                   "ok" if result.get("ok") else "error", result.get("error", ""),
                   int((time.time() - started) * 1000))
        return result

    if category == "android":
        started = time.time()
        result = await op_android(body)
        log_action(category, str(body.get("action")), body,
                   "ok" if result.get("ok") else "error", result.get("error", ""),
                   int((time.time() - started) * 1000))
        return result

    if category == "permission":
        started = time.time()
        result = await op_permission(body)
        log_action(category, str(body.get("action")), body,
                   "ok" if result.get("ok") else "error", result.get("error", ""),
                   int((time.time() - started) * 1000))
        return result

    if category == "knowledge":
        started = time.time()
        result = await op_knowledge(body)
        log_action(category, str(body.get("operation") or body.get("action")), body,
                   "ok" if result.get("ok") else "error", result.get("error", ""),
                   int((time.time() - started) * 1000))
        return result

    if category == "context":
        started = time.time()
        result = await op_context(body)
        log_action(category, str(body.get("operation") or body.get("action")), body,
                   "ok" if result.get("ok") else "error", result.get("error", ""),
                   int((time.time() - started) * 1000))
        return result

    if category == "db":
        started = time.time()
        result = await op_db(body)
        log_action(category, str(body.get("operation") or body.get("action")), body,
                   "ok" if result.get("ok") else "error", result.get("error", ""),
                   int((time.time() - started) * 1000))
        return result

    if category == "workflows":
        started = time.time()
        result = await op_workflows(body)
        log_action(category, str(body.get("operation") or body.get("action")), body,
                   "ok" if result.get("ok") else "error", result.get("error", ""),
                   int((time.time() - started) * 1000))
        return result

    if category == "loops":
        started = time.time()
        result = await op_loops(body)
        log_action(category, str(body.get("operation") or body.get("action")), body,
                   "ok" if result.get("ok") else "error", result.get("error", ""),
                   int((time.time() - started) * 1000))
        return result

    raw_action = str(body.get("action", "")).strip()
    action = ACTION_ALIASES.get(category, {}).get(raw_action.lower(), raw_action.lower())
    if action != raw_action:
        body = {**body, "action": action}
    started = time.time()

    if category not in SYNC_OPS and category not in ASYNC_OPS:
        return _fail(
            f'unknown category "{category}" (system | files | apps | windows | input | '
            "browser | network | display | audio | devices | security | power | startup | "
            "registry | code | code_session | vision | doc | knowledge | context | "
            "permission | db | workflows | loops | reminders | android | commands)")

    if not action:
        return _fail("action is required")

    # service -> desktop proxy for GUI-bound actions
    if (not DESKTOP_MODE) and _needs_desktop(category, action, body):
        timeout = float(body.get("timeout", 120)) + 30
        if action == "screen_record":
            timeout = float(body.get("duration", 10)) + 150
        result = await _proxy_to_desktop(category, body, timeout)
        log_action(category, action, body, "ok" if result.get("ok") else "error",
                   result.get("error", ""), int((time.time() - started) * 1000))
        return result

    try:
        if category in ASYNC_OPS:
            fn = ASYNC_OPS[category].get(action)
            if not fn:
                return _fail(f'unknown action "{action}" for {category}')
            result = await fn(body)
        else:
            fn = SYNC_OPS[category].get(action)
            if not fn:
                return _fail(f'unknown action "{action}" for {category}')
            result = await asyncio.to_thread(fn, body)
    except PermissionDenied as e:
        result = _fail(str(e), needs_permission=e.required, current_mode=e.current, action=action)
    except Exception as e:
        result = _fail(f"{type(e).__name__}: {e}", action=action)

    log_action(category, action, body, "ok" if result.get("ok") else "error",
               result.get("error", ""), int((time.time() - started) * 1000))
    return result


@app.on_event("startup")
async def _startup():
    global LOOPS_WORKER_TASK, HEARTBEAT_TASK
    if DESKTOP_MODE:
        return
    if LOOPS_WORKER_TASK is None or LOOPS_WORKER_TASK.done():
        LOOPS_WORKER_TASK = asyncio.create_task(_loops_worker())
    if HEARTBEAT_TASK is None or HEARTBEAT_TASK.done():
        HEARTBEAT_TASK = asyncio.create_task(_heartbeat_worker())


@app.on_event("shutdown")
async def _shutdown():
    global LOOPS_WORKER_TASK, HEARTBEAT_TASK
    for task in (LOOPS_WORKER_TASK, HEARTBEAT_TASK):
        if task is not None:
            task.cancel()
    LOOPS_WORKER_TASK = None
    HEARTBEAT_TASK = None
    if DESKTOP_MODE:
        await BROWSER.close()


def main():
    port = int(CFG.get("desktop_port", 8766)) if DESKTOP_MODE else int(CFG.get("service_port", 8765))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()



