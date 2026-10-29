"""Offline self-test for the Telegram Personal bridge (no Telegram login needed).

Run:
    .venv/Scripts/python.exe selftest.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

import main


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))
    if not cond:
        raise SystemExit(1)


def main_test() -> None:
    # Isolate the audit log so the real one is untouched.
    tmp = Path(__file__).with_name("_selftest_send_log.json")
    tmp.write_text("[]", encoding="utf-8")
    original_log = main.SEND_LOG
    original_state = main.STATE_FILE
    main.SEND_LOG = tmp
    main.STATE_FILE = Path(__file__).with_name("_selftest_state.json")
    if main.STATE_FILE.exists():
        main.STATE_FILE.unlink()
    try:
        # 1) empty log -> 10 remaining
        status = main.rate_limit_status()
        check("rate limit starts at 10", status["used_last_hour"] == 0 and status["remaining"] == 10, json.dumps(status))

        # 2) 10 successful sends -> blocked
        rows = []
        for i in range(10):
            rows.append({"ts_epoch": time.time() - i, "action": "send_message", "ok": True, "message_id": i})
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        status = main.rate_limit_status()
        check("rate limit blocks the 11th", status["remaining"] == 0 and status["blocked"], json.dumps(status))
        check("_rate_block returns rate_limited", (main._rate_block() or {}).get("rate_limited") is True)

        # 3) old sends do not count
        rows = [{"ts_epoch": time.time() - 7200, "action": "send_message", "ok": True} for _ in range(10)]
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        check("old sends fall out of the window", main.rate_limit_status()["used_last_hour"] == 0)

        # 4) HTTP contract
        client = TestClient(main.app)
        h = client.get("/health")
        check("health is public", h.status_code == 200 and h.json()["ok"] is True, h.text[:200])
        bad = client.post("/tools/telegram_personal", json={"action": "get_dialogs"})
        check("missing api key is rejected", bad.status_code == 401, bad.text[:120])
        headers = {"X-API-Key": main.API_KEY}
        unknown = client.post("/tools/telegram_personal", json={"action": "nope"}, headers=headers)
        check("unknown action returns error", unknown.json().get("ok") is False, unknown.text[:200])
        unconfirmed = client.post("/tools/telegram_personal",
                                  json={"action": "send_message", "contact": "@someone", "message": "hi"},
                                  headers=headers)
        j = unconfirmed.json()
        check("send without confirmation is refused", j.get("needs_confirmation") is True, unconfirmed.text[:300])

        # 5) unconfigured account reports a clear error on reads
        # Isolate this check from the live config so it never touches a real session.
        original_cfg = main.CFG
        main.CFG = {**original_cfg, "api_id": 0, "api_hash": ""}
        main.CLIENT = None
        main.CLIENT_READY = False
        main.CLIENT_ERROR = ""
        try:
            dialogs = client.post("/tools/telegram_personal", json={"action": "get_dialogs"}, headers=headers)
            dj = dialogs.json()
            check("read operation explains missing credentials",
                  dj.get("ok") is False and ("api_id" in str(dj.get("error", "")).lower()
                                             or "credentials" in str(dj.get("error", "")).lower()))
        finally:
            main.CFG = original_cfg
            main.CLIENT = None
            main.CLIENT_READY = False
            main.CLIENT_ERROR = ""

        # 6) flood pause blocks sends
        main._save_state({"paused_until": time.time() + 120})
        block = main._rate_block() or {}
        check("flood pause blocks sends", block.get("flood_wait") is True, json.dumps(block))
        main.STATE_FILE.unlink(missing_ok=True)

        print("\nALL CHECKS PASSED")
    finally:
        main.SEND_LOG = original_log
        main.STATE_FILE = original_state
        tmp.unlink(missing_ok=True)
        Path(__file__).with_name("_selftest_state.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main_test()
