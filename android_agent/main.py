#!/usr/bin/env python3
"""OTHVM Android Agent — local ADB bridge + emergency stop.

Runs on 127.0.0.1:8767 (Telegram Personal moved to 8768).
Endpoints are POST + X-API-Key except /health.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "connection_log.txt"
STOP_FLAG = BASE_DIR / "STOP_FLAG"
SCREEN_DIR = BASE_DIR / "temp"
SCREEN_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CFG = {
    "tailscale_phone_ip": "",
    "local_phone_ip": "",
    "adb_port": 5555,
    "server_port": 8767,
    "api_key": "",
    "adb_path": r"C:\ProgramData\chocolatey\bin\adb.exe",
    "tailscale_path": r"C:\Program Files\Tailscale\tailscale.exe",
    "reconnect_interval_seconds": 60,
    "command_timeout_seconds": 25,
}


def load_cfg() -> dict[str, Any]:
    cfg = dict(DEFAULT_CFG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except Exception as exc:  # keep defaults if config is malformed
            log(f"config parse error: {exc}")
    return cfg


CFG = load_cfg()
ADB = CFG.get("adb_path") or shutil.which("adb") or "adb"
TS = CFG.get("tailscale_path") or shutil.which("tailscale") or "tailscale"
CMD_TIMEOUT = int(CFG.get("command_timeout_seconds") or 25)
PORT = int(CFG.get("server_port") or 8767)
STATE_LOCK = threading.Lock()
LAST_STATE: dict[str, Any] = {"connected": False, "serial": None, "method": None, "device": None, "last_attempt": None}

app = FastAPI(title="OTHVM Android Agent", version="1.0.0")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"{now_iso()} {msg}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def verify_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = str(CFG.get("api_key") or "")
    if not expected:
        raise HTTPException(status_code=503, detail="api_key is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def ensure_running() -> None:
    if STOP_FLAG.exists():
        raise HTTPException(status_code=423, detail="Emergency stop is active. Clear /stop/clear first.")


def run(cmd: list[str], timeout: int | None = None) -> dict[str, Any]:
    timeout = timeout or CMD_TIMEOUT
    started = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return {
            "ok": p.returncode == 0,
            "exit_code": p.returncode,
            "stdout": (p.stdout or "").strip(),
            "stderr": (p.stderr or "").strip(),
            "duration_ms": int((time.time() - started) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": f"timeout after {timeout}s", "duration_ms": int((time.time() - started) * 1000)}
    except FileNotFoundError:
        return {"ok": False, "exit_code": -2, "stdout": "", "stderr": f"not found: {cmd[0]}", "duration_ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "exit_code": -3, "stdout": "", "stderr": str(exc), "duration_ms": int((time.time() - started) * 1000)}


def adb(args: list[str], serial: str | None = None, timeout: int | None = None) -> dict[str, Any]:
    cmd = [ADB]
    if serial:
        cmd += ["-s", serial]
    return run(cmd + args, timeout=timeout)


def adb_bytes(args: list[str], serial: str | None = None, timeout: int = 30) -> dict[str, Any]:
    """Run adb and keep stdout as bytes (screenshots, pulled files)."""
    cmd = [ADB]
    if serial:
        cmd += ["-s", serial]
    started = time.time()
    try:
        p = subprocess.run(cmd + args, capture_output=True, timeout=timeout)
        return {
            "ok": p.returncode == 0,
            "exit_code": p.returncode,
            "stdout": p.stdout or b"",
            "stderr": (p.stderr or b"").decode("utf-8", "replace").strip(),
            "duration_ms": int((time.time() - started) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": b"", "stderr": f"timeout after {timeout}s",
                "duration_ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "exit_code": -3, "stdout": b"", "stderr": str(exc),
                "duration_ms": int((time.time() - started) * 1000)}


def adb_devices() -> list[dict[str, Any]]:
    r = adb(["devices", "-l"])
    devices: list[dict[str, Any]] = []
    if not r["ok"]:
        return devices
    for line in r["stdout"].splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            serial, state = parts[0], parts[1]
            extra = " ".join(parts[2:])
            devices.append({"serial": serial, "state": state, "extra": extra, "network": ":" in serial})
    return devices


def first_online() -> dict[str, Any] | None:
    for d in adb_devices():
        if d["state"] == "device":
            return d
    return None


def tailscale_ip() -> str | None:
    r = run([TS, "ip", "-4"], timeout=10)
    if r["ok"]:
        for line in r["stdout"].splitlines():
            line = line.strip()
            if line.startswith("100."):
                return line
    return None


def tailscale_android_peers() -> list[dict[str, Any]]:
    r = run([TS, "status", "--json"], timeout=18)
    if not r["ok"] or not r["stdout"]:
        return []
    try:
        data = json.loads(r["stdout"])
    except Exception:
        return []
    peers = []
    for v in (data.get("Peer") or {}).values():
        os_name = str(v.get("OS") or "").lower()
        host = str(v.get("HostName") or "")
        dns = str(v.get("DNSName") or "")
        ips = [str(x) for x in (v.get("TailscaleIPs") or []) if str(x).startswith("100.")]
        if not ips:
            continue
        if os_name == "android" or "phone" in (host + dns).lower() or "galaxy" in (host + dns).lower() or "samsung" in (host + dns).lower():
            peers.append({"host": host, "dns": dns, "os": os_name, "ips": ips, "online": bool(v.get("Online"))})
    return peers


def local_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def set_state(connected: bool, serial: str | None = None, method: str | None = None, device: dict | None = None) -> None:
    with STATE_LOCK:
        LAST_STATE.update({"connected": connected, "serial": serial, "method": method, "device": device, "last_attempt": now_iso()})


def mdns_targets() -> list[tuple[str, str]]:
    """Discover Android wireless-debugging endpoints advertised over mDNS."""
    r = run([ADB, "mdns", "services"], timeout=12)
    found: list[tuple[str, str]] = []
    if not r["ok"]:
        return found
    for line in r["stdout"].splitlines():
        if "_adb-tls-connect._tcp" not in line and "_adb._tcp" not in line:
            continue
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", line)
        if m:
            found.append((f"{m.group(1)}:{m.group(2)}", "mdns"))
    return found


def connect_sequence(force: bool = False) -> dict[str, Any]:
    """Try Tailscale -> local Wi-Fi -> USB. Returns a report with every attempt."""
    if STOP_FLAG.exists():
        return {"ok": False, "stopped": True, "error": "emergency stop active"}
    attempts: list[dict[str, Any]] = []
    adb(["start-server"], timeout=15)
    current = first_online()
    if current and not force:
        attempts.append({"target": current["serial"], "method": "already-connected", "ok": True})
        set_state(True, current["serial"], "already-connected", current)
        return {"ok": True, "device": current, "attempts": attempts, "method": "already-connected"}

    port = int(CFG.get("adb_port") or 5555)
    targets: list[tuple[str, str]] = []
    ts_ip = (CFG.get("tailscale_phone_ip") or "").strip()
    local = (CFG.get("local_phone_ip") or "").strip()
    if ts_ip:
        targets.append((ts_ip, "tailscale-config"))
    else:
        for peer in tailscale_android_peers():
            for ip in peer["ips"]:
                targets.append((ip, "tailscale-peer:" + (peer.get("host") or peer.get("dns") or "android")))
    if local:
        targets.append((local, "local-config"))

    seen = set()
    for host, method in targets:
        if host in seen:
            continue
        seen.add(host)
        target = f"{host}:{port}"
        r = adb(["connect", target], timeout=12)
        ok = r["ok"] and "connected to" in (r["stdout"] + r["stderr"]).lower()
        attempts.append({"target": target, "method": method, "ok": ok, "output": (r["stdout"] or r["stderr"])[:400]})
        if ok:
            time.sleep(0.8)
            d = first_online()
            if d:
                set_state(True, d["serial"], method, d)
                log(f"connected via {method} -> {d['serial']}")
                return {"ok": True, "device": d, "attempts": attempts, "method": method}

    # mDNS wireless debugging (Android 11+ random port)
    for target, method in mdns_targets():
        if target in seen:
            continue
        seen.add(target)
        r = adb(["connect", target], timeout=12)
        ok = r["ok"] and "connected to" in (r["stdout"] + r["stderr"]).lower()
        attempts.append({"target": target, "method": method, "ok": ok, "output": (r["stdout"] or r["stderr"])[:400]})
        if ok:
            time.sleep(0.8)
            d = first_online()
            if d:
                set_state(True, d["serial"], method, d)
                log(f"connected via {method} -> {d['serial']}")
                return {"ok": True, "device": d, "attempts": attempts, "method": method}

    # USB / already-plugged device
    d = first_online()
    if d:
        method = "usb" if ":" not in d["serial"] else "network"
        set_state(True, d["serial"], method, d)
        log(f"connected via {method} -> {d['serial']}")
        attempts.append({"target": d["serial"], "method": method, "ok": True})
        return {"ok": True, "device": d, "attempts": attempts, "method": method}

    set_state(False, None, None, None)
    log("no device connected" + (f" (tried {len(attempts)} targets)" if attempts else ""))
    return {"ok": False, "attempts": attempts, "error": "no Android device connected. Enable USB debugging or wireless ADB."}


def require_device() -> dict[str, Any]:
    d = first_online()
    if not d:
        report = connect_sequence(force=False)
        d = report.get("device") if report.get("ok") else None
    if not d:
        raise HTTPException(status_code=503, detail="No Android device connected. POST /device/reconnect or connect USB.")
    return d


def sh(cmd: str | list[str], serial: str, timeout: int | None = None) -> dict[str, Any]:
    if isinstance(cmd, str):
        return adb(["shell", cmd], serial=serial, timeout=timeout)
    return adb(["shell"] + cmd, serial=serial, timeout=timeout)


class ReconnectBody(BaseModel):
    force: bool = False


class ConnectBody(BaseModel):
    host: str
    port: int = Field(default=5555, ge=1, le=65535)


class TcpipBody(BaseModel):
    port: int = Field(default=5555, ge=1, le=65535)


class PairBody(BaseModel):
    host: str
    pair_port: int = 0
    code: str = ""


class AlarmBody(BaseModel):
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    label: str = "OTHVM"
    skip_ui: bool = True


class VolumeBody(BaseModel):
    stream: int = Field(default=3, ge=0, le=10)
    level: int | None = Field(default=None, ge=0, le=100)


class KeyBody(BaseModel):
    keycode: int
    repeat: int = Field(default=1, ge=1, le=10)


class UrlBody(BaseModel):
    url: str


class ScreenshotBody(BaseModel):
    include_base64: bool = False


class TapBody(BaseModel):
    x: int
    y: int
    duration_ms: int = Field(default=0, ge=0, le=5000)


class SwipeBody(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int
    duration_ms: int = Field(default=300, ge=50, le=5000)


class TextBody(BaseModel):
    text: str
    submit: bool = False


class NamedKeyBody(BaseModel):
    key: str
    repeat: int = Field(default=1, ge=1, le=10)


class OpenAppBody(BaseModel):
    app: str = ""
    package: str = ""
    wait_ms: int = Field(default=800, ge=0, le=5000)


class UIBody(BaseModel):
    limit: int = Field(default=200, ge=1, le=500)


class TapTextBody(BaseModel):
    text: str
    exact: bool = False
    index: int = Field(default=0, ge=0, le=50)


class SmsBody(BaseModel):
    number: str
    text: str
    exit_on_sent: bool = True


class MediaBody(BaseModel):
    action: str = "status"
    level: int | None = Field(default=None, ge=0, le=100)


class DndBody(BaseModel):
    mode: str = "status"


class BrightnessBody(BaseModel):
    percent: int | None = Field(default=None, ge=0, le=100)


class NetworkBody(BaseModel):
    target: str
    enabled: bool = True


class ScreenBody(BaseModel):
    state: str = "on"


class StopBody(BaseModel):
    active: bool = True
    reason: str = ""


class ShellBody(BaseModel):
    command: str
    timeout: int = Field(default=20, ge=1, le=60)


@app.get("/health")
def health() -> dict[str, Any]:
    devices = adb_devices()
    online = [d for d in devices if d["state"] == "device"]
    return {
        "ok": True,
        "service": "othvm-android-agent",
        "port": PORT,
        "time": now_iso(),
        "stop_flag": STOP_FLAG.exists(),
        "adb": {"path": ADB, "installed": Path(ADB).exists() if ":" in ADB or "\\" in ADB else shutil.which(ADB) is not None, "devices": devices, "connected": bool(online)},
        "state": LAST_STATE,
        "tailscale": {"laptop_ip": tailscale_ip(), "android_peers": tailscale_android_peers(), "local_ip": local_ip()},
        "config": {"tailscale_phone_ip": CFG.get("tailscale_phone_ip"), "local_phone_ip": CFG.get("local_phone_ip"), "adb_port": CFG.get("adb_port")},
    }


@app.post("/device/reconnect", dependencies=[Depends(verify_key)])
def device_reconnect(body: ReconnectBody | None = None) -> dict[str, Any]:
    ensure_running()
    return connect_sequence(force=bool(body and body.force))


@app.post("/device/pair", dependencies=[Depends(verify_key)])
def device_pair(body: PairBody) -> dict[str, Any]:
    ensure_running()
    if not body.code:
        raise HTTPException(status_code=400, detail="pair code is required")
    target = f"{body.host}:{body.pair_port}" if body.pair_port else body.host
    r = adb(["pair", target, body.code], timeout=25)
    log(f"pair {target}: {'ok' if r['ok'] else 'failed'}")
    return {"ok": r["ok"], "target": target, "output": (r["stdout"] + "\n" + r["stderr"]).strip()}


@app.post("/device/connect", dependencies=[Depends(verify_key)])
def device_connect(body: ConnectBody) -> dict[str, Any]:
    ensure_running()
    target = f"{body.host}:{body.port}"
    r = adb(["connect", target], timeout=15)
    ok = r["ok"] and "connected to" in (r["stdout"] + r["stderr"]).lower()
    d = first_online() if ok else None
    if d:
        set_state(True, d["serial"], "manual", d)
        log(f"connected manually -> {d['serial']}")
    return {"ok": bool(d), "target": target, "device": d, "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:800]}


@app.post("/device/tcpip", dependencies=[Depends(verify_key)])
def device_tcpip(body: TcpipBody | None = None) -> dict[str, Any]:
    """Enable network ADB on a USB-connected phone (adb tcpip <port>)."""
    ensure_running()
    d = first_online()
    if not d or ":" in d["serial"]:
        raise HTTPException(status_code=400, detail="Connect the phone by USB first (no network device found).")
    port = (body.port if body else 5555)
    r = adb(["tcpip", str(port)], serial=d["serial"], timeout=20)
    log(f"tcpip {port} on {d['serial']}: {'ok' if r['ok'] else 'failed'}")
    return {"ok": r["ok"], "serial": d["serial"], "port": port, "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:500]}


@app.post("/device/info", dependencies=[Depends(verify_key)])
def device_info() -> dict[str, Any]:
    ensure_running()
    d = require_device()
    s = d["serial"]
    props = {}
    for prop in ["ro.product.brand", "ro.product.model", "ro.product.device", "ro.build.version.release", "ro.build.version.sdk", "ro.serialno"]:
        r = sh(["getprop", prop], s, timeout=10)
        props[prop] = r["stdout"] if r["ok"] else None
    battery = sh("dumpsys battery", s, timeout=12)["stdout"]
    storage = sh("df -h /data", s, timeout=12)["stdout"]
    wifi = sh("ip addr show wlan0", s, timeout=12)["stdout"]
    return {
        "ok": True,
        "serial": s,
        "method": LAST_STATE.get("method"),
        "brand": props.get("ro.product.brand"),
        "model": props.get("ro.product.model"),
        "device": props.get("ro.product.device"),
        "android": props.get("ro.build.version.release"),
        "sdk": props.get("ro.build.version.sdk"),
        "battery": battery,
        "storage": storage,
        "wifi": wifi,
    }


@app.post("/device/alarms/list", dependencies=[Depends(verify_key)])
def alarms_list(limit: int = 50) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    raw = sh("dumpsys alarm", d["serial"], timeout=25)["stdout"]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    alarms = []
    seen: set[tuple[str | None, str | None, int]] = set()
    for m in re.finditer(r"Alarm\{[^}]*\}", raw):
        chunk = m.group(0)
        # The tag and triggerTime live on the lines after the Alarm{...} header.
        end = raw.find("Alarm{", m.end())
        block = raw[m.start(): end if end != -1 else m.end() + 700]
        when = (
            re.search(r"origWhen\s+(\d{12,})", chunk)
            or re.search(r"origWhen=(\d{12,})", block)
            or re.search(r"when=(\d{12,})", chunk)
        )
        if not when:
            continue
        when_ms = int(when.group(1))
        # Skip alarms that already fired or are more than a minute in the past.
        if when_ms < now_ms - 60_000:
            continue
        tag = re.search(r"tag=([^\s}]+)", block)
        tag_value = tag.group(1) if tag else None
        if tag_value and any(x in tag_value for x in ("UPCOMING_ALERT", "MIDNIGHT", "DATE_CHANGED")):
            continue
        op = re.search(r"operation=([^\s}]+)", block)
        pkg = re.search(r"\b([A-Za-z0-9_.]+)\}$", chunk)
        pkg_value = pkg.group(1) if pkg else None
        # Only the user's Clock app alarms, not the hundreds of system timers.
        if not (
            (pkg_value and "clock" in pkg_value.lower())
            or (tag_value and "clockpackage" in tag_value.lower())
        ):
            continue
        try:
            when_iso = datetime.fromtimestamp(when_ms / 1000, tz=timezone.utc).isoformat()
        except (OSError, ValueError, OverflowError):
            continue
        key = (pkg_value, tag_value, when_ms)
        if key in seen:
            continue
        seen.add(key)
        alarms.append({
            "when_ms": when_ms,
            "when": when_iso,
            "tag": tag_value,
            "operation": op.group(1) if op else None,
            "package": pkg_value,
            "raw": block[:500],
        })
    # Prefer clock-app alarms, then nearest.
    alarms.sort(key=lambda a: (0 if (a.get("package") and "clock" in a["package"].lower()) or (a.get("operation") and "clock" in a["operation"].lower()) else 1, a.get("when_ms") or 9e18))
    return {"ok": True, "count": len(alarms), "alarms": alarms[: int(limit)]}


@app.post("/device/alarm/create", dependencies=[Depends(verify_key)])
def alarm_create(body: AlarmBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    # adb shell joins its arguments with spaces without quoting, so the label
    # must be shell-quoted as part of one command string; otherwise a label
    # such as "اختبار OTHVM" is split and am start fails with pkg=...
    label = shlex.quote(str(body.label or "OTHVM"))
    clock_pkg = str(CFG.get("clock_package") or "com.sec.android.app.clockpackage").strip()
    command = (
        "am start -a android.intent.action.SET_ALARM "
        f"-p {shlex.quote(clock_pkg)} "
        f"--ei android.intent.extra.alarm.HOUR {int(body.hour)} "
        f"--ei android.intent.extra.alarm.MINUTES {int(body.minute)} "
        f"--es android.intent.extra.alarm.MESSAGE {label}"
    )
    if body.skip_ui:
        command += " --ez android.intent.extra.alarm.SKIP_UI true"
    r = sh(command, d["serial"], timeout=20)
    log(f"alarm create {body.hour:02d}:{body.minute:02d} {body.label}: {'ok' if r['ok'] else 'failed'}")
    return {
        "ok": r["ok"],
        "device": d["serial"],
        "time": f"{body.hour:02d}:{body.minute:02d}",
        "label": body.label,
        "skip_ui": body.skip_ui,
        "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:1000],
        "note": "Samsung Clock may ignore SKIP_UI and show the editor; in that case the alarm is created after the on-screen save.",
    }


@app.post("/device/alarms/show", dependencies=[Depends(verify_key)])
def alarms_show() -> dict[str, Any]:
    ensure_running()
    d = require_device()
    r = sh(["am", "start", "-a", "android.intent.action.SHOW_ALARMS"], d["serial"], timeout=15)
    return {"ok": r["ok"], "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:500]}


@app.post("/device/notifications", dependencies=[Depends(verify_key)])
def notifications(limit: int = 20) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    raw = sh("dumpsys notification --noredact", d["serial"], timeout=25)["stdout"]
    items = []
    blocks = re.split(r"\n\s*(?=NotificationRecord\{)", raw)
    for block in blocks:
        if "NotificationRecord{" not in block:
            continue
        pkg = re.search(r"pkg=([^\s]+)", block)
        title = re.search(r"android\.title=(.*)", block)
        text = re.search(r"android\.text=(.*)", block)
        if not (pkg or title or text):
            continue
        items.append({
            "package": pkg.group(1) if pkg else None,
            "title": (title.group(1).strip() if title else None),
            "text": (text.group(1).strip() if text else None),
        })
    return {"ok": True, "count": len(items), "notifications": items[: int(limit)]}


@app.post("/device/volume", dependencies=[Depends(verify_key)])
def volume(body: VolumeBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    if body.level is None:
        r = sh(["media", "volume", "--stream", str(body.stream), "--get"], d["serial"], timeout=10)
        m = re.search(r"volume is (\d+)", r["stdout"])
        return {"ok": r["ok"], "stream": body.stream, "level": int(m.group(1)) if m else None, "output": r["stdout"]}
    r = sh(["media", "volume", "--stream", str(body.stream), "--set", str(body.level)], d["serial"], timeout=10)
    return {"ok": r["ok"], "stream": body.stream, "level": body.level, "output": (r["stdout"] + "\n" + r["stderr"]).strip()}


@app.post("/device/keyevent", dependencies=[Depends(verify_key)])
def keyevent(body: KeyBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    results = []
    for _ in range(body.repeat):
        results.append(sh(["input", "keyevent", str(body.keycode)], d["serial"], timeout=10))
    return {"ok": all(r["ok"] for r in results), "keycode": body.keycode, "output": results[-1]["stdout"] if results else ""}


@app.post("/device/open_url", dependencies=[Depends(verify_key)])
def open_url(body: UrlBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    r = sh(["am", "start", "-a", "android.intent.action.VIEW", "-d", body.url], d["serial"], timeout=15)
    return {"ok": r["ok"], "url": body.url, "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:800]}


KEYCODES = {
    "home": 3, "back": 4, "recents": 187, "power": 26, "wakeup": 224, "sleep": 223,
    "enter": 66, "delete": 67, "volume_up": 24, "volume_down": 25, "mute": 164,
    "play_pause": 85, "next": 87, "previous": 88, "stop": 86, "camera": 27,
    "notification": 83, "menu": 82, "search": 84, "tab": 61, "escape": 111,
    "space": 62, "move_home": 122, "move_end": 123,
}
APP_ALIASES = {
    "chrome": "com.android.chrome", "youtube": "com.google.android.youtube",
    "whatsapp": "com.whatsapp", "telegram": "org.telegram.messenger",
    "instagram": "com.instagram.android", "facebook": "com.facebook.katana",
    "camera": "com.sec.android.app.camera", "gallery": "com.sec.android.gallery3d",
    "settings": "com.android.settings", "phone": "com.samsung.android.dialer",
    "dialer": "com.samsung.android.dialer", "messages": "com.samsung.android.messaging",
    "sms": "com.samsung.android.messaging", "clock": "com.sec.android.app.clockpackage",
    "calculator": "com.sec.android.app.popupcalculator", "calendar": "com.samsung.android.calendar",
    "maps": "com.google.android.apps.maps", "gmail": "com.google.android.gm",
    "spotify": "com.spotify.music", "files": "com.sec.android.app.myfiles",
    "playstore": "com.android.vending", "store": "com.android.vending",
}


def _bounds_center(raw: str) -> tuple[int, int] | None:
    m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", str(raw or ""))
    if not m:
        return None
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _installed_packages(serial: str, include_system: bool = True) -> list[str]:
    flag = "" if include_system else " -3"
    r = sh(f"pm list packages{flag}".strip(), serial, timeout=25)
    return sorted({ln.strip().replace("package:", "") for ln in r["stdout"].splitlines()
                   if ln.strip().startswith("package:")})


def _resolve_package(serial: str, query: str) -> tuple[str, list[str]]:
    q = query.strip().lower()
    if not q:
        return "", []
    alias = APP_ALIASES.get(q)
    if alias:
        return alias, []
    packages = _installed_packages(serial)
    if q in packages:
        return q, []
    if "." in q:
        matches = [p for p in packages if p == q or p.endswith("." + q)]
    else:
        matches = [p for p in packages if p.split(".")[-1] == q]
        if not matches:
            matches = [p for p in packages if q in p.lower()]
    if len(matches) == 1:
        return matches[0], []
    return (matches[0] if matches else ""), matches[:15]


def _ui_nodes(serial: str, limit: int = 200) -> dict[str, Any]:
    dump = sh(["uiautomator", "dump", "/sdcard/othvm_ui.xml"], serial, timeout=25)
    if not dump["ok"]:
        return {"ok": False, "error": "uiautomator dump failed",
                "output": (dump["stdout"] + "\n" + dump["stderr"]).strip()[:1000]}
    xml = adb(["exec-out", "cat", "/sdcard/othvm_ui.xml"], serial=serial, timeout=20)
    if not xml["ok"] or not xml["stdout"].strip().startswith("<"):
        return {"ok": False, "error": "could not read the UI dump",
                "output": (xml["stdout"] + "\n" + xml["stderr"]).strip()[:1000]}
    try:
        root = ET.fromstring(xml["stdout"])
    except ET.ParseError as exc:
        return {"ok": False, "error": f"UI XML parse failed: {exc}"}
    nodes = []
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        if not text and not desc:
            continue
        center = _bounds_center(node.get("bounds") or "")
        if not center:
            continue
        nodes.append({
            "text": text or None,
            "desc": desc or None,
            "class": node.get("class"),
            "package": node.get("package"),
            "clickable": str(node.get("clickable") or "").lower() == "true",
            "enabled": str(node.get("enabled") or "").lower() == "true",
            "bounds": node.get("bounds"),
            "center": {"x": center[0], "y": center[1]},
        })
    return {"ok": True, "count": len(nodes), "nodes": nodes[: int(limit)]}


@app.post("/device/screenshot", dependencies=[Depends(verify_key)])
def screenshot(body: ScreenshotBody | None = None) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    raw = adb_bytes(["exec-out", "screencap", "-p"], serial=d["serial"], timeout=30)
    data = raw.get("stdout") or b""
    if not raw.get("ok") or not data.startswith(b"\x89PNG"):
        return {"ok": False, "error": "screencap failed", "stderr": (raw.get("stderr") or "")[:800]}
    path = SCREEN_DIR / f"screen_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{d['serial'].replace(':', '_')}.png"
    path.write_bytes(data)
    size = sh("wm size", d["serial"], timeout=10)["stdout"]
    m = re.search(r"(\d+)x(\d+)", size)
    out: dict[str, Any] = {
        "ok": True, "serial": d["serial"], "path": str(path), "size_bytes": len(data),
        "width": int(m.group(1)) if m else None, "height": int(m.group(2)) if m else None,
        "mime": "image/png",
    }
    if body and body.include_base64:
        out["base64"] = base64.b64encode(data).decode("ascii")
    return out


@app.post("/device/tap", dependencies=[Depends(verify_key)])
def tap(body: TapBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    if body.duration_ms > 0:
        cmd = ["input", "swipe", str(body.x), str(body.y), str(body.x), str(body.y), str(body.duration_ms)]
    else:
        cmd = ["input", "tap", str(body.x), str(body.y)]
    r = sh(cmd, d["serial"], timeout=15)
    return {"ok": r["ok"], "x": body.x, "y": body.y, "duration_ms": body.duration_ms,
            "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:500]}


@app.post("/device/swipe", dependencies=[Depends(verify_key)])
def swipe(body: SwipeBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    r = sh(["input", "swipe", str(body.x1), str(body.y1), str(body.x2), str(body.y2), str(body.duration_ms)],
           d["serial"], timeout=20)
    return {"ok": r["ok"], "from": {"x": body.x1, "y": body.y1}, "to": {"x": body.x2, "y": body.y2},
            "duration_ms": body.duration_ms, "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:500]}


@app.post("/device/text", dependencies=[Depends(verify_key)])
def text_input(body: TextBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    value = str(body.text or "")
    encoded = value.replace(" ", "%s")
    r = sh(f"input text {shlex.quote(encoded)}", d["serial"], timeout=20)
    submitted = False
    if body.submit:
        submitted = sh(["input", "keyevent", "66"], d["serial"], timeout=10)["ok"]
    note = None
    if any(ord(ch) > 127 for ch in value):
        note = "Android input text is ASCII-only; Arabic needs the ADBKeyboard IME."
    return {"ok": r["ok"], "chars": len(value), "submit": submitted, "note": note,
            "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:500]}


@app.post("/device/key", dependencies=[Depends(verify_key)])
def key_named(body: NamedKeyBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    name = str(body.key or "").lower().strip()
    if name.isdigit():
        keycode = int(name)
    else:
        keycode = KEYCODES.get(name)
    if not keycode:
        return {"ok": False, "error": f"unknown key '{body.key}'", "keys": sorted(KEYCODES)}
    results = [sh(["input", "keyevent", str(keycode)], d["serial"], timeout=10) for _ in range(body.repeat)]
    return {"ok": all(x["ok"] for x in results), "key": name, "keycode": keycode, "repeat": body.repeat}


@app.post("/device/open_app", dependencies=[Depends(verify_key)])
def open_app(body: OpenAppBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    query = (body.package or body.app or "").strip()
    if not query:
        return {"ok": False, "error": "app or package is required"}
    pkg, candidates = _resolve_package(d["serial"], query)
    if not pkg:
        return {"ok": False, "error": f"no installed app matched '{query}'", "candidates": candidates}
    r = sh(["monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"], d["serial"], timeout=20)
    launched = r["ok"] and "No activities found" not in r["stdout"] and "monkey aborted" not in (r["stdout"] + r["stderr"]).lower()
    component = None
    if not launched:
        comp = sh(["cmd", "package", "resolve-activity", "--brief", pkg], d["serial"], timeout=15)
        lines = [ln.strip() for ln in comp["stdout"].splitlines() if "/" in ln]
        component = lines[-1] if lines else None
        if component:
            r = sh(["am", "start", "-n", component], d["serial"], timeout=20)
            launched = r["ok"] and "Error" not in (r["stdout"] + r["stderr"])
    if body.wait_ms:
        time.sleep(body.wait_ms / 1000)
    log(f"open_app {query} -> {pkg}: {'ok' if launched else 'failed'}")
    return {"ok": bool(launched), "app": query, "package": pkg, "component": component,
            "candidates": candidates, "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:800]}


@app.post("/device/packages", dependencies=[Depends(verify_key)])
def packages(query: str = "", include_system: bool = False, limit: int = 300) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    items = _installed_packages(d["serial"], include_system=include_system)
    if query:
        q = query.lower().strip()
        items = [p for p in items if q in p.lower()]
    return {"ok": True, "count": len(items), "packages": items[: int(limit)]}


@app.post("/device/ui", dependencies=[Depends(verify_key)])
def ui(body: UIBody | None = None) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    return _ui_nodes(d["serial"], limit=(body.limit if body else 200))


@app.post("/device/tap_text", dependencies=[Depends(verify_key)])
def tap_text(body: TapTextBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    want = str(body.text or "").strip().lower()
    if not want:
        return {"ok": False, "error": "text is required"}
    report = _ui_nodes(d["serial"], limit=500)
    if not report.get("ok"):
        return report
    matches = []
    for node in report["nodes"]:
        hay = f"{node.get('text') or ''} {node.get('desc') or ''}".lower()
        if (hay.strip() == want) if body.exact else (want in hay):
            matches.append(node)
    if not matches:
        return {"ok": False, "error": f"no on-screen text matched '{body.text}'", "visible": report["nodes"][:40]}
    if body.index >= len(matches):
        return {"ok": False, "error": f"only {len(matches)} matches", "matches": matches}
    node = matches[body.index]
    center = node["center"]
    r = sh(["input", "tap", str(center["x"]), str(center["y"])], d["serial"], timeout=15)
    return {"ok": r["ok"], "tapped": node, "matches": len(matches)}


@app.post("/device/current_app", dependencies=[Depends(verify_key)])
def current_app() -> dict[str, Any]:
    ensure_running()
    d = require_device()
    raw = sh("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'", d["serial"], timeout=20)["stdout"]
    pkg = activity = None
    m = re.search(r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", raw)
    if m:
        pkg, activity = m.group(1), m.group(2)
    return {"ok": True, "package": pkg, "activity": activity, "raw": raw.strip()[:1000]}


@app.post("/device/sms/send", dependencies=[Depends(verify_key)])
def sms_send(body: SmsBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    number = str(body.number or "").strip()
    if not number:
        return {"ok": False, "error": "number is required"}
    command = (
        "am start -a android.intent.action.SENDTO -d "
        + shlex.quote("sms:" + number)
        + " --es sms_body " + shlex.quote(str(body.text or ""))
        + (" --ez exit_on_sent true" if body.exit_on_sent else "")
    )
    r = sh(command, d["serial"], timeout=25)
    log(f"sms draft {number}: {'ok' if r['ok'] else 'failed'}")
    return {"ok": r["ok"], "number": number, "sent": False, "opened": r["ok"],
            "note": "The SMS app opens with the message prefilled; Android does not allow silent SMS from ADB without a helper IME/app.",
            "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:800]}


@app.post("/device/sms/list", dependencies=[Depends(verify_key)])
def sms_list(limit: int = 20) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    raw = sh('content query --uri content://sms/inbox --projection address:date:body --sort "date DESC"',
             d["serial"], timeout=25)
    if not raw["ok"]:
        return {"ok": False, "error": "SMS query failed", "output": (raw["stdout"] + "\n" + raw["stderr"]).strip()[:800]}
    rows = []
    for line in raw["stdout"].splitlines():
        if not line.startswith("Row:"):
            continue
        row = {"raw": line[:800]}
        for key in ("address", "date", "body"):
            m = re.search(rf"\b{key}=([^,]+?)(?:,\s*(?:address|date|body)=|$)", line)
            if m:
                row[key] = m.group(1).strip()
        rows.append(row)
    return {"ok": True, "count": len(rows), "messages": rows[: int(limit)]}


@app.post("/device/calls", dependencies=[Depends(verify_key)])
def calls(limit: int = 20) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    raw = sh('content query --uri content://call_log/calls --projection number:date:duration:type:name --sort "date DESC"',
             d["serial"], timeout=25)
    if not raw["ok"]:
        return {"ok": False, "error": "call log query failed", "output": (raw["stdout"] + "\n" + raw["stderr"]).strip()[:800]}
    rows = [{"raw": ln[:800]} for ln in raw["stdout"].splitlines() if ln.startswith("Row:")]
    return {"ok": True, "count": len(rows), "calls": rows[: int(limit)]}


@app.post("/device/media", dependencies=[Depends(verify_key)])
def media(body: MediaBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    action = str(body.action or "status").lower().strip()
    if action == "status":
        raw = sh("dumpsys media_session", d["serial"], timeout=25)["stdout"]
        lines = [ln.strip() for ln in raw.splitlines()
                 if any(k in ln for k in ("state=PlaybackState", "metadata:", "description=", "active="))]
        return {"ok": True, "lines": lines[:40]}
    keys = {"play": 126, "pause": 127, "play_pause": 85, "next": 87, "previous": 88, "stop": 86}
    if action in keys:
        r = sh(["input", "keyevent", str(keys[action])], d["serial"], timeout=10)
        return {"ok": r["ok"], "action": action}
    if action == "volume" and body.level is not None:
        r = sh(["media", "volume", "--stream", "3", "--set", str(body.level)], d["serial"], timeout=10)
        return {"ok": r["ok"], "action": "volume", "level": body.level,
                "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:300]}
    return {"ok": False, "error": f"unknown media action '{action}'"}


@app.post("/device/dnd", dependencies=[Depends(verify_key)])
def dnd(body: DndBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    mode = str(body.mode or "status").lower().strip()
    mapping = {"off": "off", "on": "on", "none": "none", "priority": "priority", "alarms": "alarms"}
    current = sh("settings get global zen_mode", d["serial"], timeout=10)["stdout"].strip()
    if mode == "status":
        labels = {"0": "off", "1": "priority", "2": "none", "3": "alarms", "4": "all"}
        return {"ok": True, "zen_mode": current, "mode": labels.get(current, current)}
    if mode not in mapping:
        return {"ok": False, "error": "mode must be status, off, priority, none, or alarms"}
    r = sh(f"cmd notification set_dnd {mapping[mode]}", d["serial"], timeout=15)
    if not r["ok"]:
        r = sh(f"settings put global zen_mode { {'off':'0','priority':'1','none':'2','alarms':'3'}[mode] }",
               d["serial"], timeout=15)
    return {"ok": r["ok"], "mode": mode, "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:400]}


@app.post("/device/brightness", dependencies=[Depends(verify_key)])
def brightness(body: BrightnessBody | None = None) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    current = sh("settings get system screen_brightness", d["serial"], timeout=10)["stdout"].strip()
    if body is None or body.percent is None:
        try:
            pct = round(int(current) * 100 / 255)
        except Exception:
            pct = None
        return {"ok": True, "raw": current, "percent": pct}
    raw_value = max(0, min(255, round(body.percent * 255 / 100)))
    r = sh(f"settings put system screen_brightness_mode 0", d["serial"], timeout=10)
    r2 = sh(f"settings put system screen_brightness {raw_value}", d["serial"], timeout=10)
    return {"ok": r["ok"] and r2["ok"], "percent": body.percent, "raw": raw_value}


@app.post("/device/network", dependencies=[Depends(verify_key)])
def network(body: NetworkBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    target = str(body.target or "").lower().strip()
    onoff = "enable" if body.enabled else "disable"
    if target == "wifi":
        r = sh(f"svc wifi {onoff}", d["serial"], timeout=15)
    elif target == "data":
        r = sh(f"svc data {onoff}", d["serial"], timeout=15)
    elif target in ("airplane", "airplane_mode"):
        r = sh(f"cmd connectivity airplane-mode {onoff}", d["serial"], timeout=15)
        if not r["ok"]:
            val = "1" if body.enabled else "0"
            r = sh(f"settings put global airplane_mode_on {val}", d["serial"], timeout=15)
    else:
        return {"ok": False, "error": "target must be wifi, data, or airplane"}
    return {"ok": r["ok"], "target": target, "enabled": body.enabled,
            "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:500]}


@app.post("/device/screen", dependencies=[Depends(verify_key)])
def screen(body: ScreenBody) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    state = str(body.state or "on").lower().strip()
    if state == "status":
        raw = sh("dumpsys power | grep -E 'mWakefulness=|Display Power'", d["serial"], timeout=15)["stdout"]
        return {"ok": True, "raw": raw.strip()[:600]}
    if state in ("on", "wake"):
        r = sh(["input", "keyevent", "224"], d["serial"], timeout=10)
    elif state in ("off", "sleep"):
        r = sh(["input", "keyevent", "223"], d["serial"], timeout=10)
    elif state == "unlock":
        r = sh(["wm", "dismiss-keyguard"], d["serial"], timeout=10)
    elif state == "lock":
        r = sh(["input", "keyevent", "26"], d["serial"], timeout=10)
    else:
        return {"ok": False, "error": "state must be status, on, off, unlock, or lock"}
    return {"ok": r["ok"], "state": state, "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:400]}


@app.post("/device/apps", dependencies=[Depends(verify_key)])
def apps(include_system: bool = False) -> dict[str, Any]:
    ensure_running()
    d = require_device()
    flag = "" if include_system else "-3"
    r = sh(f"pm list packages {flag}".strip(), d["serial"], timeout=25)
    pkgs = sorted({line.strip().replace("package:", "") for line in r["stdout"].splitlines() if line.strip().startswith("package:")})
    return {"ok": r["ok"], "count": len(pkgs), "packages": pkgs}


@app.post("/device/battery", dependencies=[Depends(verify_key)])
def battery() -> dict[str, Any]:
    ensure_running()
    d = require_device()
    raw = sh("dumpsys battery", d["serial"], timeout=15)["stdout"]
    out = {}
    for line in raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return {"ok": True, "serial": d["serial"], "battery": out, "raw": raw[:3000]}


@app.post("/device/storage", dependencies=[Depends(verify_key)])
def storage() -> dict[str, Any]:
    ensure_running()
    d = require_device()
    raw = sh("df -h /data", d["serial"], timeout=15)["stdout"]
    return {"ok": True, "serial": d["serial"], "storage": raw, "free": sh("df /data", d["serial"], timeout=15)["stdout"]}


@app.post("/device/wifi", dependencies=[Depends(verify_key)])
def wifi() -> dict[str, Any]:
    ensure_running()
    d = require_device()
    addr = sh("ip addr show wlan0", d["serial"], timeout=15)["stdout"]
    dump = sh("dumpsys wifi", d["serial"], timeout=30)["stdout"]
    interesting = [ln.strip() for ln in dump.splitlines() if any(x in ln for x in ("SSID:", "mWifiInfo", "Wi-Fi is", "Supplicant state", "IP address"))]
    return {"ok": True, "serial": d["serial"], "ip": addr, "ssid_lines": interesting[:20]}


@app.post("/device/log", dependencies=[Depends(verify_key)])
def connection_log(lines: int = 100) -> dict[str, Any]:
    if not LOG_PATH.exists():
        return {"ok": True, "lines": []}
    content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"ok": True, "lines": content[-int(lines):]}


@app.post("/device/shell", dependencies=[Depends(verify_key)])
def device_shell(body: ShellBody) -> dict[str, Any]:
    """Run one adb shell command on the phone (tap, swipe, text, screencap, etc.)."""
    ensure_running()
    d = require_device()
    r = sh(body.command, d["serial"], timeout=body.timeout)
    log(f"shell {body.command[:80]}: {'ok' if r['ok'] else 'failed'}")
    return {
        "ok": r["ok"],
        "serial": d["serial"],
        "command": body.command,
        "stdout": r["stdout"][:8000],
        "stderr": r["stderr"][:4000],
        "output": (r["stdout"] + "\n" + r["stderr"]).strip()[:8000],
    }


@app.post("/emergency_stop", dependencies=[Depends(verify_key)])
def emergency_stop(body: StopBody | None = None) -> dict[str, Any]:
    active = True if body is None else body.active
    reason = (body.reason if body else "") or ""
    if active:
        STOP_FLAG.write_text(f"{now_iso()} {reason}\n", encoding="utf-8")
        set_state(False, None, "emergency_stop", None)
        log(f"EMERGENCY STOP active: {reason}")
    else:
        STOP_FLAG.unlink(missing_ok=True)
        log("emergency stop cleared")
    return {"ok": True, "active": STOP_FLAG.exists(), "reason": reason}


@app.post("/stop/clear", dependencies=[Depends(verify_key)])
def stop_clear() -> dict[str, Any]:
    STOP_FLAG.unlink(missing_ok=True)
    log("stop flag cleared")
    return {"ok": True, "active": False}


def reconnect_loop() -> None:
    interval = max(15, int(CFG.get("reconnect_interval_seconds") or 60))
    time.sleep(2)
    while True:
        try:
            if not STOP_FLAG.exists():
                d = first_online()
                if d:
                    # lightweight keepalive
                    r = sh("echo ok", d["serial"], timeout=8)
                    if not r["ok"]:
                        log(f"keepalive failed for {d['serial']}: {r['stderr'][:200]}")
                        connect_sequence(force=True)
                else:
                    connect_sequence(force=False)
        except Exception as exc:
            log(f"reconnect loop error: {exc}")
        time.sleep(interval)


@app.on_event("startup")
def startup() -> None:
    log(f"android_agent starting on 127.0.0.1:{PORT} adb={ADB} tailscale={TS}")
    threading.Thread(target=reconnect_loop, daemon=True, name="reconnect-loop").start()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
