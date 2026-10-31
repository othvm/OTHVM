#!/usr/bin/env python3
"""Back up the live OTHVM memory tables before a schema migration.

The script is read-only against Supabase.  It writes a timestamped JSON
snapshot plus a SHA-256 manifest under ``backups/memory/`` (git-ignored) and
verifies the snapshot counts against the live tables.

Usage:
    python scripts/backup_memory.py [--out backups/memory]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "mcp_server" / "config.json"
TABLES = ("memory_items", "memory_events", "othman_memory", "othvm_memory")


def _config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8-sig"))


def _headers(cfg: dict) -> dict:
    key = cfg.get("supabase_service_key") or cfg.get("supabase_anon_key")
    if not key:
        raise SystemExit("supabase_service_key is missing from mcp_server/config.json")
    return {"apikey": key, "Authorization": "Bearer " + key}


def _fetch(base: str, headers: dict, table: str) -> list[dict]:
    rows: list[dict] = []
    page = 1000
    offset = 0
    with httpx.Client(timeout=90) as client:
        while True:
            r = client.get(
                f"{base}/rest/v1/{table}",
                headers={**headers, "Range-Unit": "items", "Range": f"{offset}-{offset + page - 1}"},
                params={"select": "*"},
            )
            r.raise_for_status()
            batch = r.json()
            if not isinstance(batch, list):
                raise SystemExit(f"{table}: unexpected response")
            rows.extend(batch)
            if len(batch) < page:
                break
            offset += page
    return rows


def _digest(rows: list[dict]) -> str:
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "backups" / "memory")
    args = parser.parse_args()

    cfg = _config()
    base = str(cfg["supabase_url"]).rstrip("/")
    headers = _headers(cfg)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out if args.out.is_absolute() else ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"memory_{stamp}.json"

    snapshot = {"created_at": datetime.now(timezone.utc).isoformat(), "tables": {}}
    manifest = {"created_at": snapshot["created_at"], "tables": {}}
    for table in TABLES:
        rows = _fetch(base, headers, table)
        snapshot["tables"][table] = rows
        manifest["tables"][table] = {
            "rows": len(rows),
            "sha256": _digest(rows),
        }
        print(f"{table}: {len(rows)} rows")

    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest_path = target.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Verify by re-reading the file and re-counting the live tables.
    reread = json.loads(target.read_text(encoding="utf-8"))
    problems = []
    for table in TABLES:
        file_rows = reread["tables"].get(table) or []
        if _digest(file_rows) != manifest["tables"][table]["sha256"]:
            problems.append(f"{table}: file hash mismatch")
        live = _fetch(base, headers, table)
        if len(live) != len(file_rows):
            problems.append(f"{table}: live {len(live)} != backup {len(file_rows)}")
    if problems:
        print("BACKUP VERIFY FAILED: " + "; ".join(problems), file=sys.stderr)
        return 1
    print(f"backup: {target}")
    print(f"manifest: {manifest_path}")
    print("verified: row counts and file hashes match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
