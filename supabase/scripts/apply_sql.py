"""Apply a SQL file to the OTHVM Supabase project through the exec_sql RPC.

Usage (from the repository root):
    mcp_server\\.venv\\Scripts\\python.exe supabase\\scripts\\apply_sql.py supabase\\migrations\\011_unified_memory.sql
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent
CFG = json.loads((ROOT / "mcp_server" / "config.json").read_text(encoding="utf-8-sig"))
KEY = CFG["supabase_service_key"]
URL = CFG["supabase_url"].rstrip("/") + "/rest/v1/rpc/exec_sql"


def run(query: str) -> dict:
    r = httpx.post(
        URL,
        headers={"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
        json={"query": query},
        timeout=120,
    )
    if r.status_code != 200:
        raise SystemExit(f"HTTP {r.status_code}: {r.text[:2000]}")
    return r.json()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: apply_sql.py <file.sql> [--check \"select ...\"]")
        return 2
    sql = Path(sys.argv[1]).read_text(encoding="utf-8")
    out = run(sql)
    print(json.dumps(out, ensure_ascii=False)[:2000])
    if len(sys.argv) >= 4 and sys.argv[2] == "--check":
        print(json.dumps(run(sys.argv[3]), ensure_ascii=False)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
