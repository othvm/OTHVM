"""
Interactive Telethon login for OTHVM Telegram Personal.

Run this once as the operator, in a normal terminal (not as the service):

    .venv/Scripts/python.exe login.py

It asks for the phone code (and 2FA password if enabled) and writes the
configured session file next to this file. The Windows service then reuses that
session without needing the phone again.

Credentials: fill api_id/api_hash/phone in config.json, or set
TG_API_ID / TG_API_HASH / TG_PHONE environment variables. Get them from
https://my.telegram.org -> API development tools.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"


def load_config() -> dict:
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig")) if CONFIG_FILE.exists() else {}
    data["api_id"] = int(os.getenv("TG_API_ID") or data.get("api_id") or 0)
    data["api_hash"] = str(os.getenv("TG_API_HASH") or data.get("api_hash") or "")
    data["phone"] = str(os.getenv("TG_PHONE") or data.get("phone") or "")
    return data


async def main() -> int:
    cfg = load_config()
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        print("ERROR: api_id/api_hash are missing.")
        print("Open https://my.telegram.org -> API development tools, create an app,")
        print(f"then put api_id and api_hash in {CONFIG_FILE} and run this script again.")
        return 2
    phone = cfg.get("phone") or input("Phone (international, e.g. +2010...): ").strip()
    if not phone:
        print("ERROR: phone is required.")
        return 2
    if not cfg.get("phone"):
        cfg["phone"] = phone
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved phone to {CONFIG_FILE}")

    session = BASE_DIR / f"{cfg.get('session_name') or 'telegram_personal'}.session"
    client = TelegramClient(str(session), int(cfg["api_id"]), str(cfg["api_hash"]),
                            device_model="OTHVM Personal", system_version="Windows 10",
                            app_version="1.0", lang_code="ar", system_lang_code="ar")
    print(f"Connecting (session: {session})...")
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorized as {getattr(me, 'first_name', '')} @{getattr(me, 'username', '')} (id {me.id}).")
        print("Nothing to do. The service can start.")
        await client.disconnect()
        return 0

    print("Sending login code to Telegram...")
    try:
        await client.send_code_request(phone)
    except Exception as e:
        print(f"ERROR sending code: {type(e).__name__}: {e}")
        await client.disconnect()
        return 1

    code = input("Enter the Telegram login code: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        password = input("Two-step verification password: ").strip()
        await client.sign_in(password=password)
    except Exception as e:
        print(f"ERROR signing in: {type(e).__name__}: {e}")
        await client.disconnect()
        return 1

    me = await client.get_me()
    print(f"SUCCESS: authorized as {getattr(me, 'first_name', '')} @{getattr(me, 'username', '')} (id {me.id}).")
    print(f"Session saved to {session}. Keep this file private.")
    await client.disconnect()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
