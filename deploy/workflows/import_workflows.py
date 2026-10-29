#!/usr/bin/env python3
"""Recreate the OTHVM workflow mirror on a fresh n8n instance.

The mirror in ``current/`` is sanitized. Credential IDs and sub-workflow IDs are
placeholders or source-instance IDs, so this tool resolves credentials on the
target instance and remaps ``executeWorkflow`` references as workflows are
created.

Examples (from the repository root):

    # See what would happen; writes nothing.
    python deploy/workflows/import_workflows.py --dry-run

    # Create an offline credential template to fill in.
    python deploy/workflows/import_workflows.py --write-credential-template credentials.json

    # Create workflows in dependency order.
    python deploy/workflows/import_workflows.py --apply --credential-map credentials.json

    # Update workflows that already exist, then activate the ones active in the mirror.
    python deploy/workflows/import_workflows.py --apply --update-existing --activate

The target n8n must be reachable and the public API key must have workflow and
credential read/write access. This tool never prints secret values.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = Path(__file__).resolve().parent
CURRENT_DIR = WORKFLOW_DIR / "current"
MANIFEST_FILE = WORKFLOW_DIR / "manifest.json"
DEFAULT_CONFIG = ROOT / "mcp_server" / "config.json"
DEFAULT_REPORT = WORKFLOW_DIR / "import_report.json"
CREDENTIAL_PLACEHOLDER = "REPLACE_WITH_CREDENTIAL_ID"


class ApiError(RuntimeError):
    def __init__(self, status: int, path: str, detail: str):
        super().__init__(f"n8n API {status} for {path}: {detail[:1000]}")
        self.status = status
        self.path = path
        self.detail = detail


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


class N8nClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={
                "X-N8N-API-KEY": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ApiError(exc.code, path, detail) from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"Cannot reach n8n at {self.base_url}: {exc.reason}") from exc

    def workflows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            path = "/api/v1/workflows?limit=250"
            if cursor:
                path += "&cursor=" + urllib.parse.quote(cursor)
            payload = self.request("GET", path)
            rows.extend(payload.get("data") or [])
            cursor = payload.get("nextCursor")
            if not cursor:
                break
        return rows

    def credentials(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/api/v1/credentials?limit=250")
        return payload.get("data") or []

    def create_workflow(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v1/workflows", body)

    def update_workflow(self, workflow_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", f"/api/v1/workflows/{urllib.parse.quote(workflow_id)}", body)

    def activate(self, workflow_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/workflows/{urllib.parse.quote(workflow_id)}/activate", {})

    def deactivate(self, workflow_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/workflows/{urllib.parse.quote(workflow_id)}/deactivate", {})


def load_mirror() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = load_json(MANIFEST_FILE)
    if not isinstance(manifest, dict) or not manifest.get("workflows"):
        raise SystemExit(f"Missing or invalid {MANIFEST_FILE}; run sync_workflows.py manifest")
    workflows: dict[str, dict[str, Any]] = {}
    for entry in manifest["workflows"]:
        path = CURRENT_DIR / entry["file"]
        data = load_json(path)
        if not isinstance(data, dict) or data.get("id") != entry["id"]:
            raise SystemExit(f"Invalid workflow file: {path}")
        workflows[entry["id"]] = data
    return manifest, workflows


def credential_key(credential_type: str, name: str) -> str:
    return f"{credential_type}::{name}"


def credential_lookup(
    target_credentials: list[dict[str, Any]],
    supplied: dict[str, str],
) -> tuple[dict[tuple[str, str], str], dict[str, list[str]]]:
    by_type_name: dict[tuple[str, str], str] = {}
    by_name: dict[str, list[str]] = {}
    for credential in target_credentials:
        credential_id = credential.get("id")
        name = credential.get("name") or ""
        credential_type = credential.get("type") or ""
        if not credential_id:
            continue
        by_type_name[(credential_type, name)] = credential_id
        by_name.setdefault(name, []).append(credential_id)
    for key, credential_id in supplied.items():
        if not credential_id or key.startswith("_"):
            continue
        if "::" in key:
            credential_type, name = key.split("::", 1)
            by_type_name[(credential_type, name)] = credential_id
        else:
            by_name.setdefault(key, []).append(credential_id)
    return by_type_name, by_name


def resolve_credential(
    credential_type: str,
    name: str,
    by_type_name: dict[tuple[str, str], str],
    by_name: dict[str, list[str]],
) -> str | None:
    if (credential_type, name) in by_type_name:
        return by_type_name[(credential_type, name)]
    candidates = by_name.get(name) or []
    unique = sorted(set(candidates))
    if len(unique) == 1:
        return unique[0]
    return None


def collect_credential_requirements(
    workflows: dict[str, dict[str, Any]],
    selected_ids: set[str],
) -> dict[str, set[str]]:
    requirements: dict[str, set[str]] = {}
    for workflow_id in selected_ids:
        for node in workflows[workflow_id].get("nodes") or []:
            for credential_type, credential in (node.get("credentials") or {}).items():
                name = (credential or {}).get("name") or ""
                requirements.setdefault(credential_key(credential_type, name), set()).add(workflow_id)
    return requirements


def select_workflows(
    manifest: dict[str, Any],
    workflows: dict[str, dict[str, Any]],
    only: list[str],
) -> set[str]:
    if not only:
        return set(workflows)
    selected: set[str] = set()
    names = {workflow.get("name"): workflow_id for workflow_id, workflow in workflows.items()}
    for token in only:
        matches = [workflow_id for workflow_id, workflow in workflows.items()
                   if workflow_id == token or workflow.get("name") == token]
        if not matches:
            matches = [workflow_id for workflow_id, workflow in workflows.items()
                       if token.lower() in (workflow.get("name") or "").lower()]
        if not matches:
            raise SystemExit(f"No workflow matches --only {token!r}")
        selected.update(matches)

    # Pull in every dependency so a selected workflow is never broken.
    entries = {entry["id"]: entry for entry in manifest.get("workflows") or []}
    changed = True
    while changed:
        changed = False
        for workflow_id in list(selected):
            for dependency in entries.get(workflow_id, {}).get("dependencies") or []:
                target = dependency.get("id")
                if target in workflows and target not in selected:
                    selected.add(target)
                    changed = True
    return selected


def ordered_ids(manifest: dict[str, Any], selected: set[str]) -> list[str]:
    order = [workflow_id for workflow_id in manifest.get("importOrder") or [] if workflow_id in selected]
    remaining = sorted(selected - set(order), key=lambda item: item.lower())
    return order + remaining


def build_workflow_body(
    workflow: dict[str, Any],
    id_map: dict[str, str],
    name_map: dict[str, str],
    by_type_name: dict[tuple[str, str], str],
    by_name: dict[str, list[str]],
    *,
    allow_missing_credentials: bool,
    remap: bool,
) -> tuple[dict[str, Any], list[str], list[str]]:
    body = copy.deepcopy(workflow)
    for key in ("id", "active", "createdAt", "updatedAt", "tags"):
        body.pop(key, None)

    missing_credentials: list[str] = []
    unresolved_dependencies: list[str] = []

    for node in body.get("nodes") or []:
        credentials = node.get("credentials")
        if isinstance(credentials, dict):
            for credential_type, credential in list(credentials.items()):
                name = (credential or {}).get("name") or ""
                resolved = resolve_credential(credential_type, name, by_type_name, by_name)
                if resolved:
                    credentials[credential_type] = {"id": resolved, "name": name}
                elif allow_missing_credentials:
                    missing_credentials.append(credential_key(credential_type, name))
                else:
                    missing_credentials.append(credential_key(credential_type, name))

        if not remap or not (node.get("type") or "").endswith("executeWorkflow"):
            continue
        parameters = node.get("parameters") or {}
        raw = parameters.get("workflowId")
        cached_name = parameters.get("workflowName")
        if isinstance(raw, dict):
            cached_name = raw.get("cachedResultName") or cached_name
            value = raw.get("value")
        else:
            value = raw
        if not isinstance(value, str) or value.startswith("=") or "{{" in value:
            continue
        new_id = id_map.get(value)
        if not new_id and cached_name:
            new_id = name_map.get(cached_name)
        if not new_id:
            unresolved_dependencies.append(f"{node.get('name')} -> {cached_name or value}")
            continue
        if isinstance(raw, dict):
            raw["value"] = new_id
            if "cachedResultName" in raw and cached_name:
                raw["cachedResultName"] = cached_name
        else:
            parameters["workflowId"] = new_id

    return body, sorted(set(missing_credentials)), unresolved_dependencies


def cmd_template(args: argparse.Namespace) -> int:
    manifest, workflows = load_mirror()
    requirements = collect_credential_requirements(workflows, set(workflows))
    template: dict[str, Any] = {
        "_comment": "Fill each value with the credential ID from the target n8n instance.",
        "_format": "type::name or name -> credential id",
        "_source": "deploy/workflows/manifest.json",
    }
    for key in sorted(requirements):
        template[key] = ""
    write_json(args.write_credential_template, template)
    print(f"wrote {args.write_credential_template} ({len(requirements)} credential slots)")
    for key in sorted(requirements):
        print(f"  {key}")
    return 0


def cmd_list_credentials(args: argparse.Namespace) -> int:
    client = N8nClient(args.base_url, args.api_key)
    rows = client.credentials()
    for credential in sorted(rows, key=lambda item: ((item.get("type") or ""), (item.get("name") or ""))):
        print(f"{credential.get('id')}  {credential.get('type')}  {credential.get('name')}")
    print(f"{len(rows)} credential(s)")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    manifest, workflows = load_mirror()
    selected = select_workflows(manifest, workflows, args.only)
    order = ordered_ids(manifest, selected)
    entry_by_id = {entry["id"]: entry for entry in manifest["workflows"]}

    client = N8nClient(args.base_url, args.api_key)
    target_workflows = client.workflows()
    target_credentials = client.credentials()
    target_by_name: dict[str, list[dict[str, Any]]] = {}
    for workflow in target_workflows:
        target_by_name.setdefault(workflow.get("name") or "", []).append(workflow)

    supplied: dict[str, str] = {}
    if args.credential_map:
        data = load_json(args.credential_map)
        if not isinstance(data, dict):
            raise SystemExit(f"Invalid credential map: {args.credential_map}")
        supplied = {str(key): str(value) for key, value in data.items() if value}

    by_type_name, by_name = credential_lookup(target_credentials, supplied)
    requirements = collect_credential_requirements(workflows, selected)
    missing = []
    for key, workflow_ids in sorted(requirements.items()):
        credential_type, name = key.split("::", 1)
        if not resolve_credential(credential_type, name, by_type_name, by_name):
            missing.append({"credential": key, "workflows": sorted(workflow_ids)})

    # Existing names are mapped before creation so dependencies can point at them.
    id_map: dict[str, str] = {}
    name_map: dict[str, str] = {}
    plan: list[dict[str, Any]] = []
    for workflow_id in order:
        workflow = workflows[workflow_id]
        entry = entry_by_id[workflow_id]
        existing = target_by_name.get(workflow.get("name") or "") or []
        action = "create"
        target_id = None
        if existing:
            target_id = existing[0]["id"]
            action = "update" if args.update_existing else "skip"
        id_map[workflow_id] = target_id or ""
        name_map[workflow.get("name") or ""] = target_id or ""
        plan.append({
            "id": workflow_id,
            "name": workflow.get("name"),
            "action": action,
            "targetId": target_id,
            "active": entry.get("active"),
            "file": entry.get("file"),
        })

    # Validate every sub-workflow reference before writing anything.
    unresolved: list[dict[str, Any]] = []
    for workflow_id in order:
        workflow = workflows[workflow_id]
        for node in workflow.get("nodes") or []:
            if not (node.get("type") or "").endswith("executeWorkflow"):
                continue
            parameters = node.get("parameters") or {}
            raw = parameters.get("workflowId")
            cached_name = parameters.get("workflowName")
            if isinstance(raw, dict):
                cached_name = raw.get("cachedResultName") or cached_name
                value = raw.get("value")
            else:
                value = raw
            if not isinstance(value, str) or value.startswith("=") or "{{" in value:
                continue
            # Every mirror workflow is a valid target even before it is created;
            # import order guarantees dependencies exist by the time they are used.
            if value not in id_map and not (cached_name and cached_name in name_map):
                unresolved.append({
                    "workflow": workflow.get("name"),
                    "node": node.get("name"),
                    "target": cached_name or value,
                })

    print(f"target          : {client.base_url}")
    print(f"mirror          : {manifest['workflowCount']} workflows ({manifest['activeCount']} active)")
    print(f"selected        : {len(selected)}")
    print(f"credentials     : {len(target_credentials)} on target; {len(requirements)} required")
    print(f"missing creds   : {len(missing)}")
    for item in missing[:50]:
        print(f"  - {item['credential']}")
    print(f"unresolved deps : {len(unresolved)}")
    for item in unresolved[:50]:
        print(f"  - {item['workflow']} / {item['node']} -> {item['target']}")
    print("plan:")
    for item in plan:
        print(f"  {item['action']:<6} {item['id']} {item['name']}")

    errors = []
    if missing and not args.allow_missing_credentials:
        errors.append("missing credentials; create them in n8n or pass --credential-map")
    if unresolved:
        errors.append("unresolved sub-workflow references; import dependencies first or fix the mirror")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    if args.dry_run:
        print("dry-run: nothing written")
        return 0

    report: dict[str, Any] = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": client.base_url,
        "created": [],
        "updated": [],
        "skipped": [],
        "activated": [],
        "failed": [],
        "missingCredentials": missing,
        "unresolvedDependencies": unresolved,
        "idMap": {},
    }

    for workflow_id in order:
        workflow = workflows[workflow_id]
        entry = entry_by_id[workflow_id]
        body, workflow_missing, workflow_unresolved = build_workflow_body(
            workflow,
            id_map,
            name_map,
            by_type_name,
            by_name,
            allow_missing_credentials=args.allow_missing_credentials,
            remap=not args.no_remap,
        )
        existing = target_by_name.get(workflow.get("name") or "") or []
        try:
            if existing and args.update_existing:
                result = client.update_workflow(existing[0]["id"], body)
                report["updated"].append({"id": workflow_id, "targetId": result["id"], "name": workflow.get("name")})
                new_id = result["id"]
            elif existing:
                report["skipped"].append({"id": workflow_id, "targetId": existing[0]["id"], "name": workflow.get("name")})
                new_id = existing[0]["id"]
            else:
                result = client.create_workflow(body)
                report["created"].append({"id": workflow_id, "targetId": result["id"], "name": workflow.get("name")})
                new_id = result["id"]
            id_map[workflow_id] = new_id
            name_map[workflow.get("name") or ""] = new_id
            report["idMap"][workflow_id] = new_id

            if args.activate and entry.get("active"):
                client.activate(new_id)
                report["activated"].append({"id": workflow_id, "targetId": new_id, "name": workflow.get("name")})
        except ApiError as exc:
            report["failed"].append({
                "id": workflow_id,
                "name": workflow.get("name"),
                "error": str(exc),
            })
            print(f"FAILED {workflow.get('name')}: {exc}")
            break

    if args.report:
        write_json(args.report, report)
        print(f"report: {args.report}")
    print(
        f"created={len(report['created'])} updated={len(report['updated'])} "
        f"skipped={len(report['skipped'])} activated={len(report['activated'])} "
        f"failed={len(report['failed'])}"
    )
    if not args.activate:
        print("note: workflows were left inactive; pass --activate to activate the ones active in the mirror")
    return 1 if report["failed"] else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--credential-map", type=Path, default=None)
    parser.add_argument("--only", action="append", default=[], help="workflow id or name (repeatable; dependencies are added)")
    parser.add_argument("--apply", action="store_true", help="write to n8n (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="explicitly do not write (default)")
    parser.add_argument("--update-existing", action="store_true", help="update workflows whose name already exists")
    parser.add_argument("--activate", action="store_true", help="activate workflows that are active in the mirror")
    parser.add_argument("--allow-missing-credentials", action="store_true", help="import even if a credential is missing")
    parser.add_argument("--no-remap", action="store_true", help="do not rewrite executeWorkflow ids")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--write-credential-template", type=Path, default=None)
    parser.add_argument("--list-credentials", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_json(args.config, {})
    if not isinstance(cfg, dict):
        cfg = {}
    args.base_url = args.base_url or os.environ.get("N8N_BASE_URL") or cfg.get("n8n_base")
    args.api_key = args.api_key or os.environ.get("N8N_API_KEY") or cfg.get("n8n_api_key")

    if args.write_credential_template:
        return args
    if not args.base_url or not args.api_key:
        parser.error("n8n base URL and API key are required (config.json, --base-url/--api-key, or env)")
    args.dry_run = not args.apply
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.write_credential_template:
        return cmd_template(args)
    if args.list_credentials:
        return cmd_list_credentials(args)
    return cmd_import(args)


if __name__ == "__main__":
    raise SystemExit(main())
