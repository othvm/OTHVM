#!/usr/bin/env python3
"""OTHVM remote wake bot for Termux.

Runs on the always-on Android phone. Listens to a *second* Telegram bot and
sends a Wake-on-LAN magic packet to the laptop when the owner sends /wake.

No third-party packages: only the Python standard library.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path.home() / ".othvm_wake.env"


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

TOKEN = os.getenv("WAKE_BOT_TOKEN", "").strip()
OWNER = os.getenv("OWNER_CHAT_ID", "").strip()
MAC = os.getenv("LAPTOP_MAC", "").strip()
BROADCAST = os.getenv("BROADCAST_IP", "255.255.255.255").strip()
PORT = int(os.getenv("WOL_PORT", "9"))
LAPTOP_IP = os.getenv("LAPTOP_IP", "").strip()
REPEAT = max(1, min(10, int(os.getenv("WOL_REPEAT", "3"))))

if not TOKEN or not OWNER or not MAC:
    raise SystemExit(
        "Set WAKE_BOT_TOKEN, OWNER_CHAT_ID and LAPTOP_MAC in ~/.othvm_wake.env"
    )


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def telegram(method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=40) as response:
        return json.loads(response.read().decode("utf-8"))


def send(chat_id: str | int, text: str) -> None:
    try:
        telegram(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
    except Exception as exc:  # noqa: BLE001 - keep the listener alive
        log(f"send failed: {exc}")


def magic_packet(mac: str) -> int:
    clean = mac.replace(":", "").replace("-", "").replace(".", "").strip()
    if len(clean) != 12:
        raise ValueError("LAPTOP_MAC must contain 12 hex digits")
    mac_bytes = bytes.fromhex(clean)
    packet = b"\xff" * 6 + mac_bytes * 16
    sent = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(REPEAT):
            sock.sendto(packet, (BROADCAST, PORT))
            sent += len(packet)
            time.sleep(0.25)
    finally:
        sock.close()
    return sent


def laptop_online() -> bool | None:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", LAPTOP_IP],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


HELP = (
    "👋 <b>OTHVM Wake Bot</b>\n\n"
    "/wake — ابعت باكت إيقاظ للاب\n"
    "/status — هل اللاب شغال؟\n"
    "/id — رقم المحادثة\n\n"
    "شرط الإيقاظ: الموبايل واللاب على نفس شبكة الواي فاي، "
    "و Wake on Magic Packet مفعّل في اللاب."
)


def handle(chat_id: str, text: str) -> None:
    command = (text.split()[0] if text.split() else "").split("@")[0].lower()
    if command in ("/start", "/help"):
        send(chat_id, HELP)
        return
    if command == "/id":
        send(chat_id, f"Chat ID: <code>{chat_id}</code>")
        return
    if command == "/status":
        state = laptop_online()
        if state is True:
            send(chat_id, "🟢 اللاب شغال ومتصل بالشبكة.")
        elif state is False:
            send(chat_id, "🔴 اللاب مش مستجيب للـ ping. ممكن يكون نايم — جرّب /wake.")
        else:
            send(chat_id, "⚪ مش قادر أتحقق (أمر ping غير متاح). جرّب /wake.")
        return
    if command == "/wake":
        try:
            sent = magic_packet(MAC)
        except Exception as exc:  # noqa: BLE001
            send(chat_id, f"❌ فشل إرسال الباكت: <code>{exc}</code>")
            return
        send(
            chat_id,
            f"📡 بعتت {REPEAT} باكتات إيقاظ ({sent} بايت) إلى <code>{MAC}</code> "
            f"على <code>{BROADCAST}:{PORT}</code>.\nاستنى 10–20 ثانية وجرّب /status.",
        )
        time.sleep(8)
        if laptop_online() is True:
            send(chat_id, "🟢 اللاب صحى.")
        return
    send(chat_id, HELP)


def main() -> None:
    if not TOKEN:
        print("WAKE_BOT_TOKEN is missing. Put it in ~/.othvm_wake.env", file=sys.stderr)
        raise SystemExit(2)
    log(f"wake bot started; laptop={MAC} broadcast={BROADCAST}:{PORT} owner={OWNER}")
    offset = 0
    while True:
        try:
            result = telegram(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": json.dumps(["message"]),
                },
            )
            for update in result.get("result", []):
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                text = (message.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                if OWNER and chat_id != OWNER:
                    send(chat_id, "⛔ مش مسموح. البوت ده لصاحب اللاب فقط.")
                    continue
                handle(chat_id, text)
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception as exc:  # noqa: BLE001 - network hiccups are normal
            log(f"poll error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
