from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
from typing import Any

from jsonschema import ValidationError, validate

from .errors import ESAAError
from .utils import normalize_rel_path


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern.replace("\\", "/")) for pattern in patterns)


def _validate_safe_path(path: str) -> str:
    norm = normalize_rel_path(path)
    if not norm or norm.startswith("/") or norm.startswith(".."):
        raise ESAAError("BOUNDARY_VIOLATION", f"invalid path: {path}")
    parts = PurePosixPath(norm).parts
    if any(part == ".." for part in parts):
        raise ESAAError("BOUNDARY_VIOLATION", f"path traversal forbidden: {path}")
    return norm


def validate_agent_output(
    output: dict[str, Any],
    schema: dict[str, Any],
    contract: dict[str, Any],
    task: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    try:
        validate(output, schema)
    except ValidationError as exc:
        raise ESAAError("SCHEMA_INVALID", str(exc)) from exc

    allowed_root = {"activity_event", "file_updates"}
    unknown_root = set(output.keys()) - allowed_root
    if unknown_root:
        raise ESAAError("SCHEMA_INVALID", f"unknown root keys: {sorted(unknown_root)}")

    event = output["activity_event"]
    action = event["action"]
    task_status = str(task.get("status", ""))
    if action not in contract["vocabulary"]["allowed_agent_actions"]:
        raise ESAAError("UNKNOWN_ACTION", f"unknown action: {action}")

    if event["task_id"] != task["task_id"]:
        raise ESAAError("SCHEMA_INVALID", "activity_event.task_id does not match dispatched task")

    _validate_dispatch_model(action=action, event=event, task_status=task_status, contract=contract)

    forbidden = set(contract["output_contract"]["activity_event"]["forbidden_fields"])
    found_forbidden = sorted([field for field in event.keys() if field in forbidden])
    if found_forbidden:
        raise ESAAError("SCHEMA_INVALID", f"forbidden activity_event fields: {found_forbidden}")

    if action == "complete":
        _validate_verification_gate(event=event, task=task, contract=contract)
        _validate_discovery_gate(event=event, task=task, contract=contract)
        if task.get("is_hotfix"):
            if not event.get("issue_id") or not event.get("fixes"):
                raise ESAAError("WORKFLOW_GATE", "hotfix complete requires issue_id and fixes")

    if action == "review":
        decision = event.get("decision")
        if decision not in {"approve", "request_changes"}:
            raise ESAAError("SCHEMA_INVALID", f"invalid review decision: {decision}")

    updates = list(output.get("file_updates", []))
    if updates and action != "complete":
        reject_code = (
            contract.get("output_contract", {})
            .get("file_updates_contract", {})
            .get("reject_code_on_violation", "MISSING_COMPLETE")
        )
        raise ESAAError(reject_code, "file_updates is only allowed when action=complete")
    _validate_boundaries(updates, contract, task)
    return event, updates


def _validate_dispatch_model(action: str, event: dict[str, Any], task_status: str, contract: dict[str, Any]) -> None:
    model = contract.get("dispatch_model", {})
    by_status = model.get("invocation_by_status", {})
    status_rule = by_status.get(task_status, {})
    allowed_actions = status_rule.get("allowed_actions", [])
    if allowed_actions and action not in allowed_actions:
        reject_code = status_rule.get("reject_code_on_forbidden", "WORKFLOW_GATE")
        raise ESAAError(reject_code, f"action {action} is not allowed for task status {task_status}")

    prior_status = event.get("prior_status")
    if prior_status != task_status:
        reject_code = (
            contract.get("output_contract", {})
            .get("activity_event", {})
            .get("prior_status", {})
            .get("on_mismatch", {})
            .get("reject_code", "PRIOR_STATUS_MISMATCH")
        )
        raise ESAAError(
            reject_code,
            f"prior_status mismatch: expected {task_status}, got {prior_status}",
        )


def _validate_verification_gate(event: dict[str, Any], task: dict[str, Any], contract: dict[str, Any]) -> None:
    verification = event.get("verification", {})
    checks = verification.get("checks", [])
    gate = contract.get("integrity_rules", {}).get("verification_gate", {})
    reject_code = gate.get("reject_code_on_violation", "MISSING_VERIFICATION")

    if task.get("is_hotfix"):
        min_checks = int(gate.get("hotfix_min_checks", 2))
    else:
        per_kind = {
            "spec": int(gate.get("spec_min_checks", 1)),
            "impl": int(gate.get("impl_min_checks", 1)),
            "qa": int(gate.get("qa_min_checks", 1)),
        }
        min_checks = per_kind.get(task["task_kind"], 1)

    if len(checks) < min_checks:
        raise ESAAError(reject_code, f"complete requires at least {min_checks} verification checks")


def _validate_discovery_gate(event: dict[str, Any], task: dict[str, Any], contract: dict[str, Any]) -> None:
    gate = contract.get("integrity_rules", {}).get("discovery_gate", {})
    if not gate.get("enabled", False):
        return

    by_kind = gate.get("by_task_kind", {})
    kind_rule = by_kind.get(task["task_kind"])
    if not kind_rule:
        return

    required_actions = kind_rule.get("require_on_actions", [])
    if event.get("action") not in required_actions:
        return

    reject_code = gate.get("reject_code_on_violation", "MISSING_DISCOVERY")
    min_items = int(kind_rule.get("min_items_per_bucket", 1))
    discovery = event.get("discovery_evidence")
    if not isinstance(discovery, dict):
        raise ESAAError(reject_code, "discovery_evidence is required for this action/task kind")

    for key in ("unknowns", "assumptions", "critical_questions"):
        items = discovery.get(key)
        if not isinstance(items, list) or len(items) < min_items:
            raise ESAAError(
                reject_code,
                f"discovery_evidence.{key} must contain at least {min_items} item(s)",
            )


def _validate_boundaries(updates: list[dict[str, str]], contract: dict[str, Any], task: dict[str, Any]) -> None:
    boundaries = contract["boundaries"]["by_task_kind"][task["task_kind"]]
    allowlist = boundaries["write"]
    denylist = boundaries.get("forbidden_write", [])

    scope_patch_enabled = contract["boundaries"]["patch_scope"]["enabled"]
    scope_patch = task.get("scope_patch", [])

    for item in updates:
        path = _validate_safe_path(item["path"])
        if not _matches_any(path, allowlist):
            raise ESAAError("BOUNDARY_VIOLATION", f"path not allowed for {task['task_kind']}: {path}")
        if denylist and _matches_any(path, denylist):
            raise ESAAError("BOUNDARY_VIOLATION", f"path explicitly forbidden: {path}")

        if scope_patch_enabled and task.get("is_hotfix"):
            if not scope_patch:
                raise ESAAError("BOUNDARY_VIOLATION", "hotfix task missing scope_patch")
            if not any(path.startswith(normalize_rel_path(prefix)) for prefix in scope_patch):
                raise ESAAError("BOUNDARY_VIOLATION", f"path outside scope_patch: {path}")

