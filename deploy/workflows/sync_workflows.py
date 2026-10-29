#!/usr/bin/env python3
"""Export, sanitize, check, and index the OTHVM n8n workflow mirror.

The live n8n instance is authoritative. This tool never writes to n8n.

Usage (from the repository root):

    python deploy/workflows/sync_workflows.py export
    python deploy/workflows/sync_workflows.py check
    python deploy/workflows/sync_workflows.py manifest
    python deploy/workflows/sync_workflows.py scan

Configuration is read from ``mcp_server/config.json`` by default, or from
``--config``. ``N8N_BASE_URL`` and ``N8N_API_KEY`` override the file.

Sanitization removes live secrets, personal identifiers, credential IDs,
runtime state, and instance-only metadata. The mirror is for review, versioning,
and disaster recovery; use ``import_workflows.py`` to recreate workflows on a
fresh instance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = Path(__file__).resolve().parent
CURRENT_DIR = WORKFLOW_DIR / "current"
MANIFEST_FILE = WORKFLOW_DIR / "manifest.json"
DEFAULT_CONFIG = ROOT / "mcp_server" / "config.json"

SANITIZER_VERSION = 2
CREDENTIAL_PLACEHOLDER = "REPLACE_WITH_CREDENTIAL_ID"
TELEGRAM_CHAT_PLACEHOLDER = "REPLACE_WITH_TELEGRAM_CHAT_ID"

# Exact values are replaced before pattern rules. Placeholders are deliberately
# stable so a fresh checkout can be imported after credentials are reconnected.
CONFIG_REDACTIONS: dict[str, dict[str, str]] = {
    "mcp_server/config.json": {
        "api_key": "REPLACE_WITH_MCP_API_KEY",
        "telegram_bot_token": "REPLACE_WITH_TELEGRAM_BOT_TOKEN",
        "telegram_chat_id": TELEGRAM_CHAT_PLACEHOLDER,
        "gemini_api_key": "REPLACE_WITH_GEMINI_API_KEY",
        "supabase_url": "https://YOUR_PROJECT.supabase.co",
        "supabase_service_key": "REPLACE_WITH_SUPABASE_SERVICE_ROLE_KEY",
        "n8n_api_key": "REPLACE_WITH_N8N_PUBLIC_API_KEY",
        "n8n_mcp_key": "REPLACE_WITH_N8N_MCP_TOKEN",
        "agent_id": "REPLACE_WITH_AGENT_ID",
        "main_telegram_thread_id": "REPLACE_WITH_MAIN_THREAD_ID",
        "public_base_url": "https://YOUR_PUBLIC_WEBHOOK_HOST",
        "brief_telegram_credential_id": "REPLACE_WITH_TELEGRAM_CREDENTIAL_ID",
        "n8n_db_path": "REPLACE_WITH_N8N_DB_PATH",
    },
    "deploy/webhook_fastpath/config.json": {
        "mcp_api_key": "REPLACE_WITH_MCP_API_KEY",
        "telegram_bot_token": "REPLACE_WITH_TELEGRAM_BOT_TOKEN",
        "telegram_secret": "REPLACE_WITH_TELEGRAM_WEBHOOK_SECRET",
        "allowed_user_id": "REPLACE_WITH_TELEGRAM_USER_ID",
        "allowed_chat_id": TELEGRAM_CHAT_PLACEHOLDER,
        "desktop_dir": "REPLACE_WITH_DESKTOP_DIR",
    },
    "guard/config.json": {
        "telegram_bot_token": "REPLACE_WITH_TELEGRAM_BOT_TOKEN",
        "telegram_secret": "REPLACE_WITH_TELEGRAM_WEBHOOK_SECRET",
        "telegram_chat_id": TELEGRAM_CHAT_PLACEHOLDER,
        "thread_like": "REPLACE_WITH_AGENT_ID:%",
    },
    "android_agent/config.json": {
        "api_key": "REPLACE_WITH_ANDROID_API_KEY",
        "tailscale_phone_ip": "REPLACE_WITH_PHONE_IP",
        "local_phone_ip": "REPLACE_WITH_PHONE_IP",
    },
    "telegram_personal/config.json": {
        "api_hash": "REPLACE_WITH_TELEGRAM_API_HASH",
        "api_id": "REPLACE_WITH_TELEGRAM_API_ID",
        "phone": "REPLACE_WITH_PHONE_NUMBER",
        "api_key": "REPLACE_WITH_TELEGRAM_PERSONAL_API_KEY",
        "telegram_bot_token": "REPLACE_WITH_TELEGRAM_BOT_TOKEN",
        "telegram_chat_id": TELEGRAM_CHAT_PLACEHOLDER,
    },
}

PATTERN_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"othvm_cmd_[A-Za-z0-9]{8,}"), "REPLACE_WITH_COMMAND_SECRET"),
    (re.compile(r"othvm_[A-Za-z0-9_]{20,}"), "REPLACE_WITH_MCP_API_KEY"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "REPLACE_WITH_TELEGRAM_BOT_TOKEN"),
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "REPLACE_WITH_GROQ_API_KEY"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "REPLACE_WITH_GOOGLE_API_KEY"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "REPLACE_WITH_JWT"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"), "REPLACE_WITH_GITHUB_TOKEN"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "REPLACE_WITH_OPENAI_API_KEY"),
    (re.compile(r"https://[a-z0-9]+\.supabase\.co"), "https://YOUR_PROJECT.supabase.co"),
    (re.compile(r"https://[a-z0-9-]+\.ngrok[a-z0-9.-]*"), "https://YOUR_PUBLIC_WEBHOOK_HOST"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "REPLACE_WITH_EMAIL"),
]

CHAT_KEYS = {
    "chatId",
    "chat_id",
    "telegram_chat_id",
    "allowed_chat_id",
    "allowed_user_id",
    "OWNER_CHAT_ID",
    "TELEGRAM_CHAT_ID",
}

TRIGGER_KINDS = {
    "n8n-nodes-base.scheduleTrigger": "schedule",
    "n8n-nodes-base.webhook": "webhook",
    "n8n-nodes-base.executeWorkflowTrigger": "executeWorkflow",
    "n8n-nodes-base.manualTrigger": "manual",
    "n8n-nodes-base.telegramTrigger": "telegram",
    "n8n-nodes-base.chatTrigger": "chat",
}


def path_variants(path: Path) -> list[str]:
    """Return the escaped path forms that occur in JSON and JS strings."""
    raw = str(path)
    return [raw.replace("\\", "\\\\"), raw, raw.replace("\\", "/")]


def operator_names() -> list[tuple[str, str]]:
    """Optional name redactions from the environment, never hardcoded."""
    pairs: list[tuple[str, str]] = []
    english = os.environ.get("OTHVM_OPERATOR_NAME", "").strip()
    arabic = os.environ.get("OTHVM_OPERATOR_NAME_AR", "").strip()
    if english:
        pairs.append((rf"\b{re.escape(english)}\b", "Operator"))
    if arabic:
        pairs.append((re.escape(arabic), "المستخدم"))
    return pairs


NAME_REPLACEMENTS = operator_names()

PATH_REPLACEMENTS = [
    (re.escape(value), "REPLACE_WITH_OTHVM_ROOT")
    for value in path_variants(ROOT)
] + [
    (re.escape(value), "REPLACE_WITH_USER_HOME")
    for value in path_variants(Path.home())
]

GITHUB_OWNER_RE = re.compile(
    r'(\$json\.owner\s*\|\|\s*")(?!REPLACE_WITH_GITHUB_OWNER)([^"]+)(")'
)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def config_values() -> dict[str, str]:
    """Return exact live values that must never appear in the mirror."""
    values: dict[str, str] = {}
    for rel, keys in CONFIG_REDACTIONS.items():
        cfg = load_json(ROOT / rel, {})
        if not isinstance(cfg, dict):
            continue
        for key, placeholder in keys.items():
            value = cfg.get(key)
            if isinstance(value, bool) or value is None:
                continue
            text = str(value).strip()
            if len(text) < 4:
                continue
            values[text] = placeholder
    return values


def build_redactor() -> tuple[list[tuple[str, str]], list[tuple[re.Pattern[str], str]]]:
    exact = config_values()
    # Longest first so a token is not partially replaced by a shorter prefix.
    exact_items = sorted(exact.items(), key=lambda kv: len(kv[0]), reverse=True)
    return exact_items, PATTERN_REDACTIONS


def sanitize_string(value: str, exact: list[tuple[str, str]]) -> str:
    text = value
    for needle, replacement in exact:
        if needle in text:
            text = text.replace(needle, replacement)
    for pattern, replacement in NAME_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    for pattern, replacement in PATH_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    text = GITHUB_OWNER_RE.sub(r"\1REPLACE_WITH_GITHUB_OWNER\3", text)
    for pattern, replacement in PATTERN_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_obj(value: Any, exact: list[tuple[str, str]], *, key: str = "") -> Any:
    if isinstance(value, str):
        if key in CHAT_KEYS and not value.startswith("="):
            return TELEGRAM_CHAT_PLACEHOLDER
        return sanitize_string(value, exact)
    if isinstance(value, list):
        return [sanitize_obj(item, exact) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for child_key, child_value in value.items():
            out[child_key] = sanitize_obj(child_value, exact, key=child_key)
        return out
    return value


def sanitize_node(node: dict[str, Any], exact: list[tuple[str, str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "credentials" and isinstance(value, dict):
            credentials: dict[str, Any] = {}
            for credential_type, credential in value.items():
                item = dict(credential) if isinstance(credential, dict) else {}
                item["id"] = CREDENTIAL_PLACEHOLDER
                credentials[credential_type] = sanitize_obj(item, exact)
            out[key] = credentials
        elif key == "pinData":
            # Test data can contain live payloads and secrets.
            continue
        else:
            out[key] = sanitize_obj(value, exact)
    return out


def sanitize_workflow(raw: dict[str, Any], exact: list[tuple[str, str]]) -> dict[str, Any]:
    """Project a live workflow to the stable, sanitized mirror shape."""
    tags = raw.get("tags") or []
    tag_names = sorted(
        tag.get("name") if isinstance(tag, dict) else str(tag)
        for tag in tags
        if (tag.get("name") if isinstance(tag, dict) else tag)
    )
    workflow: dict[str, Any] = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "active": bool(raw.get("active")),
        "createdAt": raw.get("createdAt"),
        "updatedAt": raw.get("updatedAt"),
        "settings": sanitize_obj(raw.get("settings") or {}, exact),
        "tags": tag_names,
        "connections": sanitize_obj(raw.get("connections") or {}, exact),
        "nodes": [sanitize_node(node, exact) for node in (raw.get("nodes") or [])],
    }
    description = raw.get("description")
    if description:
        workflow["description"] = sanitize_obj(description, exact)
    return workflow


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "workflow").lower()).strip("-")
    return (slug or "workflow")[:60]


def workflow_filename(workflow: dict[str, Any]) -> str:
    return f"{workflow['id']}_{slugify(workflow.get('name') or 'workflow')}.json"


def canonical(workflow: dict[str, Any]) -> str:
    return json.dumps(workflow, ensure_ascii=False, sort_keys=True, indent=1)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def api_request(base: str, key: str, path: str) -> Any:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        headers={"X-N8N-API-KEY": key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise SystemExit(f"n8n API {exc.code} for {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach n8n at {base}: {exc.reason}") from exc


def fetch_live_workflows(base: str, key: str) -> list[dict[str, Any]]:
    workflows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        path = "/api/v1/workflows?limit=250"
        if cursor:
            path += "&cursor=" + urllib.parse.quote(cursor)
        payload = api_request(base, key, path)
        workflows.extend(payload.get("data") or [])
        cursor = payload.get("nextCursor")
        if not cursor:
            break
    return workflows


def load_local_workflows() -> dict[str, tuple[Path, dict[str, Any]]]:
    local: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted(CURRENT_DIR.glob("*.json")):
        data = load_json(path)
        if isinstance(data, dict) and data.get("id"):
            local[data["id"]] = (path, data)
    return local


def workflow_ref(node: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (workflow id, cached name, dynamic expression) for a sub-workflow call."""
    parameters = node.get("parameters") or {}
    raw = parameters.get("workflowId")
    cached = parameters.get("workflowName")
    if isinstance(raw, dict):
        cached = raw.get("cachedResultName") or cached
        raw = raw.get("value")
    if raw is None or raw == "":
        return None, cached, None
    if not isinstance(raw, str):
        return None, cached, str(raw)
    if raw.startswith("=") or "{{" in raw or "$" in raw:
        return None, cached, raw
    return raw, cached, None


def trigger_info(node: dict[str, Any]) -> dict[str, Any] | None:
    node_type = node.get("type") or ""
    kind = TRIGGER_KINDS.get(node_type)
    if kind is None:
        if "trigger" not in node_type.lower():
            return None
        kind = node_type.rsplit(".", 1)[-1]
    info: dict[str, Any] = {
        "node": node.get("name"),
        "type": node_type,
        "kind": kind,
    }
    parameters = node.get("parameters") or {}
    if kind == "webhook":
        info["path"] = parameters.get("path")
        info["method"] = str(parameters.get("httpMethod") or "GET").upper()
        if node.get("webhookId"):
            info["webhookId"] = node.get("webhookId")
    if kind == "schedule":
        rule = parameters.get("rule")
        if rule is not None:
            info["rule"] = rule
    return info


def build_manifest(workflows: list[dict[str, Any]], generated_at: str | None = None) -> dict[str, Any]:
    by_id = {workflow["id"]: workflow for workflow in workflows}
    by_name = {workflow.get("name"): workflow for workflow in workflows}
    dependencies: dict[str, list[str]] = {workflow["id"]: [] for workflow in workflows}
    entries: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    credential_counts: dict[str, dict[str, Any]] = {}
    webhook_paths: list[dict[str, Any]] = []

    for workflow in sorted(workflows, key=lambda item: (item.get("name") or "").lower()):
        workflow_id = workflow["id"]
        triggers: list[dict[str, Any]] = []
        credentials: dict[tuple[str, str], int] = {}
        workflow_dependencies: list[dict[str, Any]] = []

        for node in workflow.get("nodes") or []:
            trigger = trigger_info(node)
            if trigger:
                triggers.append(trigger)
                if trigger["kind"] == "webhook":
                    webhook_paths.append({
                        "workflowId": workflow_id,
                        "workflowName": workflow.get("name"),
                        "node": trigger.get("node"),
                        "path": trigger.get("path"),
                        "method": trigger.get("method"),
                    })

            for credential_type, credential in (node.get("credentials") or {}).items():
                name = (credential or {}).get("name") or ""
                key = (credential_type, name)
                credentials[key] = credentials.get(key, 0) + 1
                bucket = credential_counts.setdefault(
                    credential_type,
                    {"type": credential_type, "references": 0, "names": set(), "workflowIds": set()},
                )
                bucket["references"] += 1
                bucket["names"].add(name)
                bucket["workflowIds"].add(workflow_id)

            if (node.get("type") or "").endswith("executeWorkflow"):
                target_id, cached_name, dynamic = workflow_ref(node)
                if dynamic:
                    warnings.append({
                        "workflowId": workflow_id,
                        "workflowName": workflow.get("name"),
                        "node": node.get("name"),
                        "reason": "dynamic workflowId",
                        "expression": dynamic,
                    })
                    workflow_dependencies.append({
                        "node": node.get("name"),
                        "dynamic": True,
                        "expression": dynamic,
                    })
                    continue
                if target_id is None:
                    warnings.append({
                        "workflowId": workflow_id,
                        "workflowName": workflow.get("name"),
                        "node": node.get("name"),
                        "reason": "missing workflowId",
                    })
                    workflow_dependencies.append({"node": node.get("name"), "missing": True})
                    continue
                target = by_id.get(target_id)
                if target is None and cached_name:
                    target = by_name.get(cached_name)
                if target is None:
                    warnings.append({
                        "workflowId": workflow_id,
                        "workflowName": workflow.get("name"),
                        "node": node.get("name"),
                        "reason": "unresolved workflowId",
                        "workflowIdTarget": target_id,
                        "cachedName": cached_name,
                    })
                    workflow_dependencies.append({
                        "node": node.get("name"),
                        "id": target_id,
                        "name": cached_name,
                        "unresolved": True,
                    })
                    continue
                dependencies[workflow_id].append(target["id"])
                workflow_dependencies.append({
                    "node": node.get("name"),
                    "id": target["id"],
                    "name": target.get("name"),
                })

        entries.append({
            "id": workflow_id,
            "name": workflow.get("name"),
            "file": workflow_filename(workflow),
            "active": bool(workflow.get("active")),
            "createdAt": workflow.get("createdAt"),
            "updatedAt": workflow.get("updatedAt"),
            "tags": workflow.get("tags") or [],
            "entryPoint": any(item["kind"] != "executeWorkflow" for item in triggers),
            "triggers": triggers,
            "webhooks": [item for item in triggers if item["kind"] == "webhook"],
            "credentials": [
                {"type": credential_type, "name": name, "references": count}
                for (credential_type, name), count in sorted(credentials.items())
            ],
            "dependencies": workflow_dependencies,
        })

    # Dependency-first import order. Cycles are reported but still emitted.
    order: list[str] = []
    state: dict[str, int] = {}
    cycle_nodes: set[str] = set()

    def visit(node_id: str, stack: list[str]) -> None:
        if state.get(node_id) == 2:
            return
        if state.get(node_id) == 1:
            cycle_nodes.update(stack[stack.index(node_id):] if node_id in stack else [node_id])
            return
        state[node_id] = 1
        stack.append(node_id)
        for dependency in sorted(set(dependencies.get(node_id, []))):
            visit(dependency, stack)
        stack.pop()
        state[node_id] = 2
        order.append(node_id)

    for workflow_id in sorted(by_id, key=lambda item: (by_id[item].get("name") or "").lower()):
        visit(workflow_id, [])

    if cycle_nodes:
        warnings.append({
            "reason": "dependency cycle",
            "workflowIds": sorted(cycle_nodes),
            "workflowNames": sorted(by_id[item].get("name") for item in cycle_nodes),
        })

    index = {workflow_id: position for position, workflow_id in enumerate(order, start=1)}
    for entry in entries:
        entry["importIndex"] = index.get(entry["id"])

    credential_types = []
    for credential_type, bucket in sorted(credential_counts.items()):
        credential_types.append({
            "type": credential_type,
            "references": bucket["references"],
            "workflowCount": len(bucket["workflowIds"]),
            "names": sorted(bucket["names"]),
        })

    return {
        "generatedAt": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "live n8n instance",
        "sanitized": True,
        "sanitizerVersion": SANITIZER_VERSION,
        "workflowCount": len(workflows),
        "activeCount": sum(1 for workflow in workflows if workflow.get("active")),
        "inactiveCount": sum(1 for workflow in workflows if not workflow.get("active")),
        "importOrder": order,
        "credentialTypes": credential_types,
        "webhookPaths": sorted(webhook_paths, key=lambda item: (item.get("path") or "", item.get("workflowName") or "")),
        "warnings": warnings,
        "workflows": entries,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


def cmd_export(args: argparse.Namespace) -> int:
    exact, _ = build_redactor()
    live = fetch_live_workflows(args.base_url, args.api_key)
    sanitized = [sanitize_workflow(workflow, exact) for workflow in live]
    sanitized.sort(key=lambda item: (item.get("name") or "").lower())

    existing = {path.name for path in CURRENT_DIR.glob("*.json")}
    planned: set[str] = set()
    changed: list[str] = []
    for workflow in sanitized:
        filename = workflow_filename(workflow)
        planned.add(filename)
        path = CURRENT_DIR / filename
        text = canonical(workflow) + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            changed.append(filename)
        if not args.dry_run:
            path.write_text(text, encoding="utf-8")

    stale = sorted(existing - planned)
    if stale and not args.no_prune and not args.dry_run:
        for filename in stale:
            (CURRENT_DIR / filename).unlink()

    manifest = build_manifest(sanitized)
    if not args.dry_run:
        write_json(MANIFEST_FILE, manifest)

    print(f"live workflows : {len(live)}")
    print(f"active         : {manifest['activeCount']}")
    print(f"inactive       : {manifest['inactiveCount']}")
    print(f"written        : {0 if args.dry_run else len(sanitized)}")
    print(f"changed        : {len(changed)}")
    if stale:
        print(f"stale removed  : {0 if args.dry_run else len(stale)}")
        for filename in stale:
            print(f"  - {filename}")
    if manifest["warnings"]:
        print(f"warnings       : {len(manifest['warnings'])}")
        for warning in manifest["warnings"]:
            print(f"  - {warning}")
    if args.dry_run:
        print("dry-run: nothing written")
    return 0


def diff_paths(left: Any, right: Any, path: str = "") -> Iterable[str]:
    if type(left) is not type(right):
        yield path or "<root>"
        return
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else key
            if key not in left:
                yield f"{child} (missing on live)"
            elif key not in right:
                yield f"{child} (extra on live)"
            else:
                yield from diff_paths(left[key], right[key], child)
        return
    if isinstance(left, list):
        if len(left) != len(right):
            yield f"{path} (length {len(left)} != {len(right)})"
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            yield from diff_paths(left_item, right_item, f"{path}[{index}]")
        return
    if left != right:
        yield path or "<root>"


def cmd_check(args: argparse.Namespace) -> int:
    exact, _ = build_redactor()
    live_raw = fetch_live_workflows(args.base_url, args.api_key)
    live = {workflow["id"]: sanitize_workflow(workflow, exact) for workflow in live_raw}
    local = load_local_workflows()

    missing = sorted(set(live) - set(local))
    extra = sorted(set(local) - set(live))
    changed: list[tuple[str, list[str]]] = []
    for workflow_id in sorted(set(live) & set(local)):
        left = canonical(live[workflow_id])
        right = canonical(local[workflow_id][1])
        if left != right:
            changed.append((workflow_id, list(diff_paths(live[workflow_id], local[workflow_id][1]))[:20]))

    print(f"live={len(live)} local={len(local)} missing={len(missing)} extra={len(extra)} changed={len(changed)}")
    for workflow_id in missing:
        print(f"MISSING {workflow_id} {live[workflow_id].get('name')}")
    for workflow_id in extra:
        print(f"EXTRA   {workflow_id} {local[workflow_id][1].get('name')}")
    for workflow_id, paths in changed:
        print(f"CHANGED {workflow_id} {live[workflow_id].get('name')}")
        for path in paths:
            print(f"  {path}")

    return 0 if not (missing or extra or changed) else 1


def cmd_manifest(args: argparse.Namespace) -> int:
    local = load_local_workflows()
    workflows = [workflow for _, workflow in local.values()]
    manifest = build_manifest(workflows)
    write_json(MANIFEST_FILE, manifest)
    print(f"workflows={manifest['workflowCount']} active={manifest['activeCount']} importOrder={len(manifest['importOrder'])}")
    if manifest["warnings"]:
        for warning in manifest["warnings"]:
            print(f"warning: {warning}")
    return 0


LEAK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(re.escape(value), re.I), "absolute repository path")
    for value in path_variants(ROOT)
] + [
    (re.compile(re.escape(value), re.I), "absolute user path")
    for value in path_variants(Path.home())
] + [
    (GITHUB_OWNER_RE, "GitHub owner literal"),
] + [
    (re.compile(pattern, re.I), "personal name")
    for pattern, _ in NAME_REPLACEMENTS
]


def cmd_scan(args: argparse.Namespace) -> int:
    exact, patterns = build_redactor()
    findings: list[tuple[Path, str, str]] = []
    for path in sorted(CURRENT_DIR.glob("*.json")) + [MANIFEST_FILE]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for needle, _ in exact:
            if needle in text:
                findings.append((path, "live config value", needle[:80]))
        for pattern, _ in patterns:
            match = pattern.search(text)
            if match:
                findings.append((path, "secret pattern", match.group(0)[:80]))
        for pattern, label in LEAK_PATTERNS:
            match = pattern.search(text)
            if match:
                findings.append((path, label, match.group(0)[:80]))
    if findings:
        for path, kind, sample in findings:
            print(f"{path.relative_to(ROOT)}: {kind}: {sample}")
        print(f"FAIL: {len(findings)} finding(s)")
        return 1
    print("OK: no live secrets or secret patterns in the workflow mirror")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="export, sanitize, and index live workflows")
    export.add_argument("--dry-run", action="store_true")
    export.add_argument("--no-prune", action="store_true", help="keep local files that no longer exist live")
    export.set_defaults(func=cmd_export)

    check = sub.add_parser("check", help="compare the sanitized local mirror with live n8n")
    check.set_defaults(func=cmd_check)

    manifest = sub.add_parser("manifest", help="rebuild manifest.json from current/")
    manifest.set_defaults(func=cmd_manifest)

    scan = sub.add_parser("scan", help="scan the mirror for live secrets")
    scan.set_defaults(func=cmd_scan)

    args = parser.parse_args(argv)
    cfg = load_json(args.config, {})
    if not isinstance(cfg, dict):
        cfg = {}
    args.base_url = args.base_url or os.environ.get("N8N_BASE_URL") or cfg.get("n8n_base")
    args.api_key = args.api_key or os.environ.get("N8N_API_KEY") or cfg.get("n8n_api_key")
    if args.command in {"export", "check"} and (not args.base_url or not args.api_key):
        parser.error("n8n base URL and API key are required (config.json, --base-url/--api-key, or env)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
