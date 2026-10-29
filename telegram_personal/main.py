"""
OTHVM — Telegram Personal (Telethon)
====================================
REST bridge that gives OTHVM access to the founder's *personal* Telegram
account through Telethon. It is intentionally small, local-only and guarded
by hard safety rules:

  * max 10 outgoing messages per rolling hour
  * every send needs explicit founder confirmation (`confirmed=true`)
  * unknown contacts need an extra `confirm_unknown=true`
  * every send attempt is appended to send_log.json
  * FloodWait / SlowMode pauses all sending and notifies the founder on the
    OTHVM bot.

Run interactively once to create the session:
    python login.py

Then run the API (port 8768 — 8766 is used by the Windows desktop helper):
    python main.py

Windows service: OTHVM-TelegramPersonal (NSSM, auto-start), see
_install_service.cmd.

Auth: X-API-Key header for /tools/telegram_personal and /status.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
import uvicorn

try:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError, SlowModeWaitError
    from telethon.tl.types import Channel, Chat, User
except Exception:  # pragma: no cover - surfaced in /health
    TelegramClient = None  # type: ignore
    FloodWaitError = SlowModeWaitError = Exception  # type: ignore
    Channel = Chat = User = object  # type: ignore

# --------------------------------------------------------------------------- paths
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
SEND_LOG = BASE_DIR / "send_log.json"
STATE_FILE = BASE_DIR / "state.json"
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "api_id": 0,
    "api_hash": "",
    "phone": "",
    "session_name": "telegram_personal",
    "port": 8768,
    "api_key": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "rate_limit_per_hour": 10,
    "max_file_mb": 200,
    "allowed_contacts": [],
    "note": "Fill api_id/api_hash/phone from https://my.telegram.org, run login.py, then start the service. Port 8768 because 8766 is the desktop helper.",
}

SEND_ACTIONS = {"send_message", "send_file", "forward_message"}


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
CFG["api_id"] = int(os.getenv("TG_API_ID") or CFG.get("api_id") or 0)
CFG["api_hash"] = str(os.getenv("TG_API_HASH") or CFG.get("api_hash") or "")
CFG["phone"] = str(os.getenv("TG_PHONE") or CFG.get("phone") or "")
CFG["port"] = int(os.getenv("TG_PORT") or CFG.get("port") or 8768)
CFG["api_key"] = str(os.getenv("OTHVM_TG_API_KEY") or CFG.get("api_key") or "")
CFG["telegram_bot_token"] = str(os.getenv("OTHVM_BOT_TOKEN") or CFG.get("telegram_bot_token") or "")
CFG["telegram_chat_id"] = str(os.getenv("OTHVM_CHAT_ID") or CFG.get("telegram_chat_id") or "")

# Keep a usable key even on a fresh install; n8n reads it from this file.
if not CFG.get("api_key"):
    CFG["api_key"] = "othvm_tg_" + secrets.token_urlsafe(24)
if not CONFIG_FILE.exists() or load_json(CONFIG_FILE, {}).get("api_key") != CFG["api_key"]:
    save_json(CONFIG_FILE, CFG)

API_KEY = str(CFG["api_key"])
SESSION_PATH = BASE_DIR / f"{CFG.get('session_name') or 'telegram_personal'}.session"

# --------------------------------------------------------------------------- state
CLIENT = None
CLIENT_READY = False
CLIENT_ERROR = ""
SEND_LOCK = asyncio.Lock()
CONTACT_CACHE: dict[str, Any] = {"at": 0.0, "contacts": []}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _display_name(entity: Any) -> str:
    try:
        name = " ".join(x for x in [getattr(entity, "first_name", ""), getattr(entity, "last_name", "")] if x).strip()
        if name:
            return name
        title = getattr(entity, "title", "")
        if title:
            return str(title)
        username = getattr(entity, "username", "")
        if username:
            return "@" + str(username)
        return str(getattr(entity, "id", ""))
    except Exception:
        return ""


def _entity_type(entity: Any) -> str:
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
    if isinstance(entity, Channel):
        return "channel" if getattr(entity, "broadcast", False) else "group"
    if isinstance(entity, Chat):
        return "group"
    return "unknown"


def _read_send_log() -> list[dict]:
    data = load_json(SEND_LOG, [])
    return data if isinstance(data, list) else []


def _append_send_log(entry: dict) -> None:
    rows = _read_send_log()
    rows.append(entry)
    # Keep the file bounded; last 2000 attempts is plenty for audit + rate limit.
    save_json(SEND_LOG, rows[-2000:])


def _state() -> dict:
    return load_json(STATE_FILE, {})


def _save_state(data: dict) -> None:
    save_json(STATE_FILE, data)


def _hour_count() -> int:
    cutoff = time.time() - 3600
    count = 0
    for row in _read_send_log():
        if row.get("action") not in SEND_ACTIONS or not row.get("ok"):
            continue
        try:
            ts = float(row.get("ts_epoch") or 0)
        except Exception:
            ts = 0
        if ts >= cutoff:
            count += 1
    return count


def rate_limit_status() -> dict:
    max_per_hour = int(CFG.get("rate_limit_per_hour") or 10)
    used = _hour_count()
    paused_until = float(_state().get("paused_until") or 0)
    remaining_pause = max(0, int(paused_until - time.time()))
    return {
        "limit": max_per_hour,
        "used_last_hour": used,
        "remaining": max(0, max_per_hour - used),
        "paused_until": datetime.fromtimestamp(paused_until, timezone.utc).isoformat() if paused_until else None,
        "pause_seconds_left": remaining_pause,
        "blocked": remaining_pause > 0 or used >= max_per_hour,
    }


def _rate_block() -> dict | None:
    status = rate_limit_status()
    if status["pause_seconds_left"] > 0:
        return {
            "ok": False,
            "error": "Telegram flood wait is active — sending is paused.",
            "flood_wait": True,
            "retry_after_seconds": status["pause_seconds_left"],
            "paused_until": status["paused_until"],
            "rate_limit": status,
        }
    if status["remaining"] <= 0:
        return {
            "ok": False,
            "error": "Hourly send limit reached (10 messages/hour). Try again later.",
            "rate_limited": True,
            "retry_after_seconds": 3600,
            "rate_limit": status,
        }
    return None


# --------------------------------------------------------------------------- telegram helpers
async def notify_founder(text: str) -> bool:
    token = _clean(CFG.get("telegram_bot_token"))
    chat = _clean(CFG.get("telegram_chat_id"))
    if not token or not chat:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            )
            return r.status_code == 200
    except Exception:
        return False


async def ensure_client() -> tuple[bool, str]:
    """Return (ready, error). Never raises; endpoints turn this into JSON."""
    global CLIENT, CLIENT_READY, CLIENT_ERROR
    if TelegramClient is None:
        CLIENT_ERROR = "telethon is not installed"
        CLIENT_READY = False
        return False, CLIENT_ERROR
    if not CFG.get("api_id") or not CFG.get("api_hash"):
        CLIENT_ERROR = (
            "Telegram API credentials are missing. Fill api_id/api_hash/phone in "
            "telegram_personal/config.json (from https://my.telegram.org) and run login.py."
        )
        CLIENT_READY = False
        return False, CLIENT_ERROR
    if CLIENT is None:
        try:
            CLIENT = TelegramClient(
                str(SESSION_PATH),
                int(CFG["api_id"]),
                str(CFG["api_hash"]),
                device_model="OTHVM Personal",
                system_version="Windows 10",
                app_version="1.0",
                lang_code="ar",
                system_lang_code="ar",
                auto_reconnect=True,
                connection_retries=5,
            )
        except Exception as e:
            CLIENT_ERROR = f"could not create Telethon client: {e}"
            CLIENT_READY = False
            return False, CLIENT_ERROR
    try:
        if not CLIENT.is_connected():
            await CLIENT.connect()
        if not await CLIENT.is_user_authorized():
            CLIENT_ERROR = (
                "Telegram session is not authorized. Run `python login.py` once in an "
                "interactive terminal (it asks for the phone code / 2FA password)."
            )
            CLIENT_READY = False
            return False, CLIENT_ERROR
        me = await CLIENT.get_me()
        CLIENT_ERROR = ""
        CLIENT_READY = True
        return True, f"authorized as @{getattr(me, 'username', '') or _display_name(me)} (id {getattr(me, 'id', '?')})"
    except Exception as e:
        CLIENT_ERROR = f"could not connect to Telegram: {type(e).__name__}: {e}"
        CLIENT_READY = False
        return False, CLIENT_ERROR


async def get_contacts_cached(force: bool = False) -> list[Any]:
    global CONTACT_CACHE
    if not force and CONTACT_CACHE["contacts"] and time.time() - CONTACT_CACHE["at"] < 300:
        return CONTACT_CACHE["contacts"]
    ready, error = await ensure_client()
    if not ready:
        raise RuntimeError(error)
    try:
        contacts = await CLIENT.get_contacts()  # type: ignore[union-attr]
    except AttributeError:
        # Telethon 1.45 exposes contacts through the raw API, not get_contacts().
        from telethon.tl.functions.contacts import GetContactsRequest
        result = await CLIENT(GetContactsRequest(hash=0))  # type: ignore[operator]
        contacts = list(getattr(result, "users", []) or [])
    CONTACT_CACHE = {"at": time.time(), "contacts": list(contacts or [])}
    return CONTACT_CACHE["contacts"]


def _norm_name(value: str) -> str:
    value = value.casefold().strip()
    value = re.sub(r"[\u064B-\u0652]", "", value)
    value = re.sub(r"[^\w\u0600-\u06FF@+]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


async def resolve_contact(contact: str, allow_unknown: bool = False) -> tuple[Any | None, dict]:
    """Resolve by @username, phone, numeric id, or exact/partial contact name.

    Returns (entity, meta). meta always contains `known`; when the entity can
    only be reached by a raw @username/phone that is *not* in the address book
    it returns `unknown=True` and the caller must require confirm_unknown.
    """
    ready, error = await ensure_client()
    if not ready:
        return None, {"ok": False, "error": error}

    raw = _clean(contact)
    if not raw:
        return None, {"ok": False, "error": "contact is required"}

    entity = None
    lookup = raw
    if lookup.startswith("https://t.me/"):
        lookup = "@" + lookup.rsplit("/", 1)[-1]

    # 1) direct lookup: numeric id, @username, or international phone
    direct = bool(re.fullmatch(r"-?\d+", lookup)) or lookup.startswith("@") or bool(re.fullmatch(r"\+?\d{7,15}", lookup))
    if direct:
        try:
            entity = await CLIENT.get_entity(int(lookup) if re.fullmatch(r"-?\d+", lookup) else lookup)  # type: ignore[union-attr]
        except Exception:
            entity = None

    contacts: list[Any] = []
    try:
        contacts = await get_contacts_cached()
    except Exception:
        contacts = []

    # 2) name search in contacts (exact, then prefix, then contains)
    if entity is None and not direct:
        needle = _norm_name(raw)
        scored: list[tuple[int, Any]] = []
        for c in contacts:
            name = _norm_name(_display_name(c))
            username = _norm_name("@" + str(getattr(c, "username", "") or ""))
            phone = _norm_name(str(getattr(c, "phone", "") or ""))
            if needle == name or needle == username or needle == phone:
                scored.append((0, c))
            elif name.startswith(needle) or username.startswith(needle):
                scored.append((1, c))
            elif needle in name or needle in username:
                scored.append((2, c))
        if scored:
            scored.sort(key=lambda x: (x[0], _display_name(x[1])))
            entity = scored[0][1]

    # 3) recent dialogs by name (covers non-contacts you already chat with)
    dialog_ids: set[int] = set()
    if entity is None:
        try:
            dialogs = await CLIENT.get_dialogs(limit=200)  # type: ignore[union-attr]
        except Exception:
            dialogs = []
        needle = _norm_name(raw)
        for d in dialogs:
            try:
                dialog_ids.add(int(d.entity.id))
            except Exception:
                pass
            name = _norm_name(d.name or _display_name(d.entity))
            if entity is None and needle and (needle == name or name.startswith(needle) or needle in name):
                entity = d.entity

    if entity is None:
        return None, {
            "ok": False,
            "error": f'No Telegram contact, username or chat found for "{raw}".',
            "not_found": True,
        }

    try:
        eid = int(entity.id)
    except Exception:
        eid = None
    contact_ids = {int(c.id) for c in contacts if getattr(c, "id", None) is not None}
    allowed = {str(x).lstrip("@").casefold() for x in (CFG.get("allowed_contacts") or [])}
    username = str(getattr(entity, "username", "") or "")
    known = (eid in contact_ids) or (eid in dialog_ids) or (username.casefold() in allowed)
    meta = {
        "ok": True,
        "known": known,
        "unknown": not known,
        "id": eid,
        "name": _display_name(entity),
        "username": username or None,
        "type": _entity_type(entity),
    }
    if not known and not allow_unknown:
        meta.update({
            "ok": False,
            "needs_confirmation": True,
            "confirm_unknown_required": True,
            "error": (
                f'"{raw}" is not in the founder\'s contacts or recent chats. '
                "Ask the founder explicitly and retry with confirm_unknown=true."
            ),
        })
        return None, meta
    return entity, meta


def _text_value(node: Any) -> str:
    """Recursively flatten Telegram rich-text nodes (TextPlain/TextBold/TextConcat)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            return node["text"]
        if node.get("text") is not None:
            return _text_value(node["text"])
        texts = node.get("texts")
        if isinstance(texts, list):
            return "".join(_text_value(x) for x in texts)
        return ""
    text = getattr(node, "text", None)
    if isinstance(text, str):
        return text
    if text is not None:
        return _text_value(text)
    texts = getattr(node, "texts", None)
    if texts:
        return "".join(_text_value(x) for x in texts)
    return ""


def _rich_text(message: Any) -> str:
    """Extract text from Telegram's RichMessage blocks (new bot API format).

    Telethon leaves ``message.message`` empty for rich messages, so a normal
    read would show a blank reply. Flatten PageBlockParagraph blocks, including
    nested TextConcat/TextBold nodes, into plain text.
    """
    rich = getattr(message, "rich_message", None)
    if rich is None:
        return ""
    blocks = getattr(rich, "blocks", None)
    if blocks is None and isinstance(rich, dict):
        blocks = rich.get("blocks")
    parts: list[str] = []
    for block in blocks or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        value = _text_value(text)
        if value:
            parts.append(value)
    return "\n".join(parts).strip()


def _message_to_dict(message: Any) -> dict:
    text = getattr(message, "message", None) or getattr(message, "text", None) or _rich_text(message) or ""
    media = getattr(message, "media", None)
    media_type = None
    if media is not None:
        media_type = type(media).__name__
    sender = getattr(message, "sender", None)
    return {
        "message_id": getattr(message, "id", None),
        "date": getattr(message, "date", None).isoformat() if getattr(message, "date", None) else None,
        "text": str(text),
        "sender_id": getattr(sender, "id", None) if sender is not None else None,
        "sender_name": _display_name(sender) if sender is not None else None,
        "outgoing": bool(getattr(message, "out", False)),
        "media_type": media_type,
        "reply_to_message_id": getattr(message, "reply_to_msg_id", None),
    }


# --------------------------------------------------------------------------- operations
async def op_send_message(body: dict) -> dict:
    if body.get("confirmed") is not True:
        return {
            "ok": False,
            "needs_confirmation": True,
            "error": "Sending to the founder's personal Telegram requires explicit founder confirmation. Ask first, then call again with confirmed=true.",
        }
    contact = _clean(body.get("contact") or body.get("to") or body.get("chat"))
    message = str(body.get("message") or body.get("text") or "")
    if not contact or not message:
        return {"ok": False, "error": "contact and message are required"}
    if len(message) > 4096:
        return {"ok": False, "error": "message is longer than Telegram's 4096-character limit"}

    blocked = _rate_block()
    if blocked:
        return blocked

    entity, meta = await resolve_contact(contact, allow_unknown=body.get("confirm_unknown") is True)
    if entity is None:
        return meta

    async with SEND_LOCK:
        blocked = _rate_block()
        if blocked:
            return blocked
        started = time.time()
        try:
            sent = await CLIENT.send_message(entity, message, reply_to=int(body["reply_to_message_id"]) if body.get("reply_to_message_id") else None)  # type: ignore[union-attr]
            result = {
                "ok": True,
                "action": "personal.send_message",
                "message_id": getattr(sent, "id", None),
                "timestamp": getattr(sent, "date", None).isoformat() if getattr(sent, "date", None) else now_iso(),
                "contact": meta,
                "text_preview": message[:160],
                "rate_limit": rate_limit_status(),
            }
            _append_send_log({
                "ts": now_iso(), "ts_epoch": time.time(), "action": "send_message",
                "contact_input": contact, "contact": meta, "message_preview": message[:500],
                "message_id": result["message_id"], "ok": True, "duration_ms": int((time.time() - started) * 1000),
            })
            return result
        except (FloodWaitError, SlowModeWaitError) as e:
            seconds = int(getattr(e, "seconds", 0) or 0) or 60
            paused_until = time.time() + seconds
            state = _state()
            state["paused_until"] = paused_until
            state["flood_reason"] = f"send_message {type(e).__name__}: {seconds}s"
            _save_state(state)
            await notify_founder(
                "⚠️ OTHVM Telegram Personal\n"
                f"تلجرام رجّع {type(e).__name__} — الإرسال اتوقف لمدة {seconds} ثانية.\n"
                f"الحساب الشخصي مش هيبعت أي رسالة لحد {datetime.fromtimestamp(paused_until, timezone.utc).strftime('%H:%M:%S')} UTC."
            )
            _append_send_log({
                "ts": now_iso(), "ts_epoch": time.time(), "action": "send_message",
                "contact_input": contact, "contact": meta, "message_preview": message[:500],
                "ok": False, "error": f"{type(e).__name__}: {seconds}s flood wait", "paused_until": paused_until,
            })
            return {"ok": False, "error": "Telegram flood wait — sending paused.", "flood_wait": True,
                    "retry_after_seconds": seconds, "paused_until": datetime.fromtimestamp(paused_until, timezone.utc).isoformat()}
        except Exception as e:
            _append_send_log({
                "ts": now_iso(), "ts_epoch": time.time(), "action": "send_message",
                "contact_input": contact, "contact": meta, "message_preview": message[:500],
                "ok": False, "error": f"{type(e).__name__}: {e}",
            })
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "contact": meta}


async def op_send_file(body: dict) -> dict:
    if body.get("confirmed") is not True:
        return {
            "ok": False,
            "needs_confirmation": True,
            "error": "Sending a file to the founder's personal Telegram requires explicit founder confirmation. Ask first, then call again with confirmed=true.",
        }
    contact = _clean(body.get("contact") or body.get("to") or body.get("chat"))
    file_path = _clean(body.get("file_path") or body.get("path"))
    file_url = _clean(body.get("file_url") or body.get("url"))
    if not contact or (not file_path and not file_url):
        return {"ok": False, "error": "contact and file_path (or file_url) are required"}

    blocked = _rate_block()
    if blocked:
        return blocked

    entity, meta = await resolve_contact(contact, allow_unknown=body.get("confirm_unknown") is True)
    if entity is None:
        return meta

    local_path: Path | None = None
    downloaded = False
    if file_url:
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
                r = await c.get(file_url)
            if r.status_code != 200:
                return {"ok": False, "error": f"file_url download failed ({r.status_code})"}
            max_mb = float(CFG.get("max_file_mb") or 200)
            if len(r.content) > max_mb * 1024 * 1024:
                return {"ok": False, "error": f"file is larger than {max_mb:g} MB"}
            name = Path(str(file_url.split("?")[0])).name or "file.bin"
            local_path = TEMP_DIR / f"url_{int(time.time())}_{secrets.token_hex(3)}_{name}"
            local_path.write_bytes(r.content)
            downloaded = True
        except Exception as e:
            return {"ok": False, "error": f"could not download file_url: {e}"}
    else:
        local_path = Path(file_path).expanduser()
        if not local_path.is_file():
            return {"ok": False, "error": f"file not found: {local_path}"}
        max_mb = float(CFG.get("max_file_mb") or 200)
        if local_path.stat().st_size > max_mb * 1024 * 1024:
            return {"ok": False, "error": f"file is larger than {max_mb:g} MB"}

    async with SEND_LOCK:
        blocked = _rate_block()
        if blocked:
            return blocked
        started = time.time()
        try:
            sent = await CLIENT.send_file(  # type: ignore[union-attr]
                entity,
                str(local_path),
                caption=str(body.get("caption") or "")[:1024],
                force_document=bool(body.get("force_document")),
            )
            result = {
                "ok": True,
                "action": "personal.send_file",
                "message_id": getattr(sent, "id", None),
                "timestamp": getattr(sent, "date", None).isoformat() if getattr(sent, "date", None) else now_iso(),
                "contact": meta,
                "file": str(local_path.name),
                "size_bytes": local_path.stat().st_size,
                "rate_limit": rate_limit_status(),
            }
            _append_send_log({
                "ts": now_iso(), "ts_epoch": time.time(), "action": "send_file",
                "contact_input": contact, "contact": meta, "file": str(local_path),
                "message_id": result["message_id"], "ok": True, "duration_ms": int((time.time() - started) * 1000),
            })
            return result
        except (FloodWaitError, SlowModeWaitError) as e:
            seconds = int(getattr(e, "seconds", 0) or 0) or 60
            paused_until = time.time() + seconds
            state = _state()
            state["paused_until"] = paused_until
            state["flood_reason"] = f"send_file {type(e).__name__}: {seconds}s"
            _save_state(state)
            await notify_founder(
                "⚠️ OTHVM Telegram Personal\n"
                f"تلجرام رجّع {type(e).__name__} أثناء إرسال ملف — الإرسال اتوقف {seconds} ثانية."
            )
            _append_send_log({
                "ts": now_iso(), "ts_epoch": time.time(), "action": "send_file",
                "contact_input": contact, "contact": meta, "file": str(local_path),
                "ok": False, "error": f"{type(e).__name__}: {seconds}s flood wait", "paused_until": paused_until,
            })
            return {"ok": False, "error": "Telegram flood wait — sending paused.", "flood_wait": True,
                    "retry_after_seconds": seconds, "paused_until": datetime.fromtimestamp(paused_until, timezone.utc).isoformat()}
        except Exception as e:
            _append_send_log({
                "ts": now_iso(), "ts_epoch": time.time(), "action": "send_file",
                "contact_input": contact, "contact": meta, "file": str(local_path),
                "ok": False, "error": f"{type(e).__name__}: {e}",
            })
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "contact": meta}
        finally:
            if downloaded and local_path and local_path.exists():
                try:
                    local_path.unlink()
                except Exception:
                    pass


async def op_get_dialogs(body: dict) -> dict:
    ready, error = await ensure_client()
    if not ready:
        return {"ok": False, "error": error}
    limit = max(1, min(int(body.get("limit", 30) or 30), 200))
    dialogs = await CLIENT.get_dialogs(limit=limit)  # type: ignore[union-attr]
    out = []
    for d in dialogs:
        last = getattr(d, "message", None)
        out.append({
            "id": getattr(d.entity, "id", None),
            "name": d.name,
            "username": getattr(d.entity, "username", None),
            "type": _entity_type(d.entity),
            "unread_count": int(getattr(d, "unread_count", 0) or 0),
            "unread_mentions": int(getattr(d, "unread_mentions_count", 0) or 0),
            "pinned": bool(getattr(d, "pinned", False)),
            "last_message": (getattr(last, "message", None) or "")[:500] if last else None,
            "last_date": getattr(last, "date", None).isoformat() if last and getattr(last, "date", None) else None,
            "last_outgoing": bool(getattr(last, "out", False)) if last else None,
        })
    return {"ok": True, "action": "personal.get_dialogs", "count": len(out), "dialogs": out,
            "note": "read-only"}


async def op_get_messages(body: dict) -> dict:
    contact = _clean(body.get("contact") or body.get("chat") or body.get("name"))
    if not contact:
        return {"ok": False, "error": "contact is required"}
    limit = max(1, min(int(body.get("limit", 20) or 20), 200))
    entity, meta = await resolve_contact(contact, allow_unknown=body.get("confirm_unknown") is True)
    if entity is None:
        return meta
    messages = await CLIENT.get_messages(entity, limit=limit)  # type: ignore[union-attr]
    out = [_message_to_dict(m) for m in reversed(messages or [])]
    return {"ok": True, "action": "personal.get_messages", "contact": meta, "count": len(out),
            "messages": out, "note": "read-only"}


async def op_search_messages(body: dict) -> dict:
    query = _clean(body.get("query") or body.get("keyword") or body.get("q"))
    if not query:
        return {"ok": False, "error": "query is required"}
    limit = max(1, min(int(body.get("limit", 50) or 50), 200))
    chats_limit = max(1, min(int(body.get("chats_limit", 40) or 40), 100))
    ready, error = await ensure_client()
    if not ready:
        return {"ok": False, "error": error}
    dialogs = await CLIENT.get_dialogs(limit=chats_limit)  # type: ignore[union-attr]
    results: list[dict] = []
    scanned = 0
    errors = 0
    for d in dialogs:
        if len(results) >= limit:
            break
        try:
            scanned += 1
            per = max(1, min(limit - len(results), 10))
            msgs = await CLIENT.get_messages(d.entity, search=query, limit=per)  # type: ignore[union-attr]
            for m in msgs or []:
                row = _message_to_dict(m)
                row["chat_id"] = getattr(d.entity, "id", None)
                row["chat_name"] = d.name
                row["chat_type"] = _entity_type(d.entity)
                results.append(row)
        except Exception:
            errors += 1
            continue
    results.sort(key=lambda r: r.get("date") or "", reverse=True)
    return {"ok": True, "action": "personal.search_messages", "query": query,
            "chats_scanned": scanned, "errors": errors, "count": len(results),
            "results": results[:limit], "note": "read-only"}


async def op_get_contacts(body: dict) -> dict:
    ready, error = await ensure_client()
    if not ready:
        return {"ok": False, "error": error}
    contacts = await get_contacts_cached(force=bool(body.get("refresh")))
    out = []
    for c in contacts:
        out.append({
            "id": getattr(c, "id", None),
            "name": _display_name(c),
            "first_name": getattr(c, "first_name", None),
            "last_name": getattr(c, "last_name", None),
            "username": getattr(c, "username", None),
            "phone": getattr(c, "phone", None),
            "mutual": bool(getattr(c, "mutual_contact", False)),
            "bot": bool(getattr(c, "bot", False)),
        })
    out.sort(key=lambda x: (x.get("name") or "").casefold())
    return {"ok": True, "action": "personal.get_contacts", "count": len(out),
            "contacts": out, "note": "read-only"}


async def op_forward_message(body: dict) -> dict:
    if body.get("confirmed") is not True:
        return {
            "ok": False,
            "needs_confirmation": True,
            "error": "Forwarding from the founder's personal Telegram requires explicit founder confirmation. Ask first, then call again with confirmed=true.",
        }
    source = _clean(body.get("from_contact") or body.get("source") or body.get("from"))
    target = _clean(body.get("to_contact") or body.get("target") or body.get("to"))
    message_id = body.get("message_id")
    if not source or not target or message_id in (None, ""):
        return {"ok": False, "error": "from_contact, to_contact and message_id are required"}

    blocked = _rate_block()
    if blocked:
        return blocked

    src_entity, src_meta = await resolve_contact(source)
    if src_entity is None:
        return src_meta
    dst_entity, dst_meta = await resolve_contact(target, allow_unknown=body.get("confirm_unknown") is True)
    if dst_entity is None:
        return dst_meta

    async with SEND_LOCK:
        blocked = _rate_block()
        if blocked:
            return blocked
        started = time.time()
        try:
            sent = await CLIENT.forward_messages(dst_entity, int(message_id), src_entity)  # type: ignore[union-attr]
            single = sent[0] if isinstance(sent, list) and sent else sent
            result = {
                "ok": True,
                "action": "personal.forward_message",
                "message_id": getattr(single, "id", None),
                "timestamp": getattr(single, "date", None).isoformat() if getattr(single, "date", None) else now_iso(),
                "from_contact": src_meta,
                "to_contact": dst_meta,
                "rate_limit": rate_limit_status(),
            }
            _append_send_log({
                "ts": now_iso(), "ts_epoch": time.time(), "action": "forward_message",
                "from_contact": src_meta, "to_contact": dst_meta, "source_message_id": message_id,
                "message_id": result["message_id"], "ok": True, "duration_ms": int((time.time() - started) * 1000),
            })
            return result
        except (FloodWaitError, SlowModeWaitError) as e:
            seconds = int(getattr(e, "seconds", 0) or 0) or 60
            paused_until = time.time() + seconds
            state = _state()
            state["paused_until"] = paused_until
            state["flood_reason"] = f"forward {type(e).__name__}: {seconds}s"
            _save_state(state)
            await notify_founder(
                "⚠️ OTHVM Telegram Personal\n"
                f"تلجرام رجّع {type(e).__name__} أثناء التوجيه — الإرسال اتوقف {seconds} ثانية."
            )
            _append_send_log({
                "ts": now_iso(), "ts_epoch": time.time(), "action": "forward_message",
                "from_contact": src_meta, "to_contact": dst_meta, "source_message_id": message_id,
                "ok": False, "error": f"{type(e).__name__}: {seconds}s flood wait", "paused_until": paused_until,
            })
            return {"ok": False, "error": "Telegram flood wait — sending paused.", "flood_wait": True,
                    "retry_after_seconds": seconds, "paused_until": datetime.fromtimestamp(paused_until, timezone.utc).isoformat()}
        except Exception as e:
            _append_send_log({
                "ts": now_iso(), "ts_epoch": time.time(), "action": "forward_message",
                "from_contact": src_meta, "to_contact": dst_meta, "source_message_id": message_id,
                "ok": False, "error": f"{type(e).__name__}: {e}",
            })
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


OPS = {
    "send_message": op_send_message,
    "send_file": op_send_file,
    "get_dialogs": op_get_dialogs,
    "get_messages": op_get_messages,
    "search_messages": op_search_messages,
    "get_contacts": op_get_contacts,
    "forward_message": op_forward_message,
}

READ_ONLY_OPS = {"get_dialogs", "get_messages", "search_messages", "get_contacts"}


# --------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Try to connect once; failures are reported by /health and each operation.
    await ensure_client()
    yield
    global CLIENT
    if CLIENT is not None:
        try:
            await CLIENT.disconnect()
        except Exception:
            pass
        CLIENT = None


app = FastAPI(title="OTHVM Telegram Personal", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    ready = CLIENT_READY and bool(CLIENT is not None)
    me = None
    if ready:
        try:
            user = await CLIENT.get_me()  # type: ignore[union-attr]
            me = {"id": getattr(user, "id", None), "username": getattr(user, "username", None),
                  "name": _display_name(user)}
        except Exception:
            pass
    return {
        "ok": True,
        "service": "othvm-telegram-personal",
        "configured": bool(CFG.get("api_id") and CFG.get("api_hash")),
        "authorized": ready,
        "account": me,
        "error": CLIENT_ERROR or None,
        "session_file": SESSION_PATH.name,
        "session_exists": SESSION_PATH.exists(),
        "port": CFG.get("port"),
        "rate_limit": rate_limit_status(),
        "time": now_iso(),
    }


@app.get("/status")
async def status(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        return JSONResponse({"ok": False, "error": "invalid api key"}, status_code=401)
    return await health()


@app.post("/tools/telegram_personal")
async def tool_endpoint(request: Request, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        return JSONResponse({"ok": False, "error": "invalid api key"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    action = _clean(body.get("action") or body.get("operation"))
    action = action.replace("personal.", "").replace("telegram_personal.", "").lower().strip()
    if not action:
        return {"ok": False, "error": "action is required",
                "operations": sorted(OPS), "read_only": sorted(READ_ONLY_OPS)}
    fn = OPS.get(action)
    if fn is None:
        return {"ok": False, "error": f'unknown action "{action}"', "operations": sorted(OPS)}
    try:
        result = await fn(body)
        result.setdefault("read_only", action in READ_ONLY_OPS)
        if action in SEND_ACTIONS:
            result.setdefault("requires_permission", "confirmed_by_founder")
        return result
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def main():
    port = int(CFG.get("port") or 8768)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
