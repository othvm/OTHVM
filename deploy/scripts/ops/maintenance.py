"""OTHVM weekly maintenance: prune n8n executions + export Supabase.

Runs as SYSTEM from Task Scheduler (see install_maintenance.ps1). Safe to run
by hand:

    python deploy/scripts/ops/maintenance.py --dry-run
    python deploy/scripts/ops/maintenance.py

* n8n: keeps the newest ``--keep`` executions and anything younger than
  ``--days`` days; deletes the rest through the public API (never touches the
  SQLite file while n8n is running).
* Supabase: exports every public table to
  ``$OTHVM_BACKUP_DIR/<YYYY-MM-DD>/supabase/`` (vector columns are skipped).
  The default backup root is ``../OTHVM-backups`` next to the repository.
* Memory v1: runs ``memory.consolidate`` (near-duplicate merge at 0.97,
  dry-run when ``--dry-run``). ``--memory-prune`` also archives stale
  low-importance items.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CFG = json.loads((ROOT / "mcp_server" / "config.json").read_text(encoding="utf-8"))
N8N_KEY = str(CFG.get("n8n_api_key") or os.environ.get("N8N_API_KEY") or "")
N8N_BASE = str(CFG.get("n8n_base") or os.environ.get("N8N_BASE") or "http://127.0.0.1:5678").rstrip("/")
N8N_API = N8N_BASE + "/api/v1"
BACKUP_ROOT = Path(
    os.environ.get("OTHVM_BACKUP_DIR") or (ROOT.parent / "OTHVM-backups")
).resolve()
N8N_HEADERS = {
    "X-N8N-API-KEY": N8N_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
}
SB_BASE = CFG["supabase_url"].rstrip("/")
SB_HEADERS = {
    "apikey": CFG["supabase_service_key"],
    "Authorization": "Bearer " + CFG["supabase_service_key"],
    "Content-Type": "application/json",
    "Accept": "application/json",
}
LOG = ROOT / "mcp_server" / "logs" / "maintenance.log"
VECTOR_TABLES = {"knowledge_base", "memory_items"}
MCP_BASE = f"http://127.0.0.1:{CFG.get('service_port', 8765)}"
MCP_HEADERS = {
    "X-API-Key": CFG["api_key"],
    "Content-Type": "application/json",
    "Accept": "application/json",
}

if not N8N_KEY:
    raise SystemExit("n8n_api_key is missing from mcp_server/config.json (or set N8N_API_KEY)")
if not SB_BASE or not CFG.get("supabase_service_key"):
    raise SystemExit("supabase_url / supabase_service_key are missing from mcp_server/config.json")


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        if LOG.exists() and LOG.stat().st_size > 2_000_000:
            LOG.replace(LOG.with_suffix(".log.1"))
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def api(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(N8N_API + path, data=data, headers=N8N_HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:800]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def prune_executions(days: int, keep: int, max_delete: int, dry: bool) -> dict:
    status, data = api("GET", "/executions?limit=250")
    if status != 200:
        return {"ok": False, "error": f"list executions failed ({status}): {data}"}
    items = list(data.get("data") or [])
    cursor = data.get("nextCursor")
    while cursor and len(items) < 10000:
        status, page = api("GET", f"/executions?limit=250&cursor={urllib.parse.quote(str(cursor))}")
        if status != 200:
            break
        items.extend(page.get("data") or [])
        cursor = page.get("nextCursor")
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    candidates = []
    for index, ex in enumerate(items):
        if index < max(1, keep):
            continue
        started = parse_dt(ex.get("startedAt") or ex.get("createdAt"))
        if started and started < cutoff and ex.get("status") not in ("running", "waiting"):
            candidates.append(ex.get("id"))
    candidates = [c for c in candidates if c is not None][: max(0, max_delete)]
    deleted = 0
    errors = []
    if not dry:
        for eid in candidates:
            st, res = api("DELETE", f"/executions/{eid}")
            if st in (200, 204):
                deleted += 1
            else:
                errors.append(f"{eid}: {st} {str(res)[:120]}")
    return {
        "ok": not errors,
        "scanned": len(items),
        "candidates": len(candidates),
        "deleted": deleted,
        "dry_run": dry,
        "errors": errors[:10],
    }


def sb_rpc(sql: str) -> dict:
    req = urllib.request.Request(
        SB_BASE + "/rest/v1/rpc/exec_sql",
        data=json.dumps({"query": sql}).encode(),
        headers=SB_HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def sb_page(table: str, offset: int, limit: int, select: str = "*") -> list:
    q = urllib.parse.urlencode({"select": select, "offset": offset, "limit": limit})
    req = urllib.request.Request(f"{SB_BASE}/rest/v1/{urllib.parse.quote(table)}?{q}", headers=SB_HEADERS)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def mcp_tool(path: str, payload: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        MCP_BASE + path,
        data=json.dumps(payload).encode(),
        headers=MCP_HEADERS,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:600]}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def consolidate_memory(dry: bool, threshold: float, prune: bool, prune_days: int) -> dict:
    payload = {
        "operation": "memory.consolidate",
        "dry_run": dry,
        "similarity_threshold": threshold,
        "prune": prune,
        "prune_days": prune_days,
    }
    res = mcp_tool("/tools/db", payload, timeout=240)
    if not isinstance(res, dict) or res.get("ok") is not True:
        return {"ok": False, "error": str((res or {}).get("error") or res)[:800]}
    return {
        "ok": True,
        "dry_run": dry,
        "threshold": threshold,
        "candidate_pairs": res.get("candidate_pairs", 0),
        "merged": res.get("merged", 0),
        "archived": res.get("archived", 0),
    }


def backup_supabase(dry: bool, out_dir: str | None = None) -> dict:
    day = datetime.now().strftime("%Y-%m-%d")
    out = Path(out_dir) if out_dir else BACKUP_ROOT / day / "supabase"
    if dry:
        return {"ok": True, "dry_run": True, "out": str(out)}
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "tables": {}}
    tables = [r["table_name"] for r in (sb_rpc(
        "select table_name from information_schema.tables "
        "where table_schema='public' and table_type='BASE TABLE' order by table_name"
    ).get("rows") or [])]
    total = 0
    for table in tables:
        try:
            cols = sb_rpc(
                "select column_name, data_type from information_schema.columns "
                f"where table_schema='public' and table_name='{table}' order by ordinal_position"
            ).get("rows") or []
            names = [c["column_name"] for c in cols]
            skip = {"embedding", "embedding_half"} if table in VECTOR_TABLES else set()
            select = ",".join(c for c in names if c not in skip)
            rows: list = []
            offset = 0
            while True:
                page = sb_page(table, offset, 1000, select)
                rows.extend(page)
                if len(page) < 1000:
                    break
                offset += 1000
            (out / f"{table}.json").write_text(json.dumps({
                "table": table, "columns": cols, "row_count": len(rows), "rows": rows,
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            manifest["tables"][table] = {"rows": len(rows), "columns": len(cols), "file": f"{table}.json"}
            total += len(rows)
        except Exception as e:
            manifest["tables"][table] = {"error": f"{type(e).__name__}: {e}"}
    manifest["total_rows"] = total
    manifest["duration_sec"] = round(time.time() - started, 1)
    (out / "_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "tables": len(tables), "rows": total, "out": str(out), "sec": manifest["duration_sec"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--keep", type=int, default=1500)
    ap.add_argument("--max-delete", type=int, default=2000)
    ap.add_argument("--skip-prune", action="store_true")
    ap.add_argument("--skip-backup", action="store_true")
    ap.add_argument("--skip-memory", action="store_true")
    ap.add_argument("--memory-threshold", type=float, default=0.97)
    ap.add_argument("--memory-prune-days", type=int, default=365)
    ap.add_argument("--memory-prune", action="store_true")
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result: dict = {"started_at": datetime.now(timezone.utc).isoformat()}
    if not args.skip_prune:
        result["prune"] = prune_executions(args.days, args.keep, args.max_delete, args.dry_run)
        log("prune " + json.dumps(result["prune"], ensure_ascii=False))
    if not args.skip_backup:
        result["backup"] = backup_supabase(args.dry_run, args.backup_dir or None)
        log("backup " + json.dumps(result["backup"], ensure_ascii=False))
    if not args.skip_memory:
        result["memory"] = consolidate_memory(
            args.dry_run, args.memory_threshold, args.memory_prune, args.memory_prune_days)
        log("memory " + json.dumps(result["memory"], ensure_ascii=False))
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    log("done " + json.dumps({"prune": result.get("prune", {}).get("deleted"),
                              "backup_rows": result.get("backup", {}).get("rows"),
                              "memory_merged": result.get("memory", {}).get("merged")}))
    return 0 if all(v.get("ok") for k, v in result.items() if isinstance(v, dict)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
