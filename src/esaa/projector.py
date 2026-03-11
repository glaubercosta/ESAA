from __future__ import annotations

from copy import deepcopy
from typing import Any

from .compat import normalize_legacy_verify_status
from .constants import ESAA_VERSION, SCHEMA_VERSION
from .errors import ESAAError
from .utils import sha256_hex, utc_now_iso


def _new_task(payload: dict[str, Any]) -> dict[str, Any]:
    description = payload.get("description", payload["title"])
    if not isinstance(description, str) or not description.strip():
        description = payload["title"]

    task = {
        "task_id": payload["task_id"],
        "task_kind": payload["task_kind"],
        "title": payload["title"],
        "description": description,
        "status": "todo",
        "depends_on": list(payload.get("depends_on", [])),
        "targets": list(payload.get("targets", [])),
        "outputs": payload.get("outputs", {"files": []}),
        "immutability": {"done_is_immutable": True},
    }
    if payload.get("is_hotfix"):
        task["is_hotfix"] = True
        for field in ("issue_id", "fixes", "scope_patch", "required_verification", "baseline_id"):
            if field in payload:
                task[field] = deepcopy(payload[field])
    return task


def _index_counts(tasks: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for task in tasks:
        value = str(task.get(key, "unknown"))
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda item: item[0]))


def compute_projection_hash(roadmap: dict[str, Any]) -> str:
    payload = {
        "schema_version": roadmap["meta"]["schema_version"],
        "project": roadmap["project"],
        "tasks": roadmap["tasks"],
        "indexes": roadmap["indexes"],
    }
    return sha256_hex(payload)


def _empty_state(project_name: str) -> dict[str, Any]:
    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "esaa_version": ESAA_VERSION,
            "immutable_done": True,
            "master_correlation_id": None,
            "run": {
                "run_id": None,
                "status": "initialized",
                "last_event_seq": 0,
                "projection_hash_sha256": "",
                "verify_status": "unknown",
            },
            "updated_at": utc_now_iso(),
        },
        "project": {"name": project_name, "audit_scope": ".roadmap/"},
        "tasks": [],
        "indexes": {"by_status": {}, "by_kind": {}},
        "_issues": {},
        "_lessons": [],
    }


def _task_by_id(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in state["tasks"]:
        if task["task_id"] == task_id:
            return task
    raise ESAAError("TASK_NOT_FOUND", f"task_id not found: {task_id}")


def _ensure_owner(task: dict[str, Any], actor: str) -> None:
    owner = task.get("assigned_to")
    if owner != actor:
        raise ESAAError("LOCK_VIOLATION", f"actor {actor} is not lock owner ({owner})")


def _apply_claim(state: dict[str, Any], event: dict[str, Any]) -> None:
    task = _task_by_id(state, event["payload"]["task_id"])
    if task["status"] == "done":
        raise ESAAError("IMMUTABLE_DONE", "cannot claim a done task")
    if task["status"] in {"in_progress", "review"} or task.get("assigned_to"):
        raise ESAAError("LOCKED_TASK", "task is already locked")
    task["status"] = "in_progress"
    task["assigned_to"] = event["actor"]
    task["started_at"] = event["ts"]


def _apply_complete(state: dict[str, Any], event: dict[str, Any]) -> None:
    task = _task_by_id(state, event["payload"]["task_id"])
    if task["status"] == "done":
        raise ESAAError("IMMUTABLE_DONE", "cannot complete a done task")
    if task["status"] != "in_progress":
        raise ESAAError("INVALID_TRANSITION", f"complete invalid for status={task['status']}")
    _ensure_owner(task, event["actor"])
    task["status"] = "review"
    verification = event["payload"].get("verification")
    if verification:
        task["verification"] = deepcopy(verification)
    if "issue_id" in event["payload"]:
        task["issue_id"] = event["payload"]["issue_id"]
    if "fixes" in event["payload"]:
        task["fixes"] = event["payload"]["fixes"]


def _apply_review(state: dict[str, Any], event: dict[str, Any]) -> None:
    task = _task_by_id(state, event["payload"]["task_id"])
    decision = event["payload"].get("decision")
    if task["status"] == "done":
        raise ESAAError("IMMUTABLE_DONE", "cannot review a done task")
    if task["status"] != "review":
        raise ESAAError("INVALID_TRANSITION", f"review invalid for status={task['status']}")
    _ensure_owner(task, event["actor"])
    if decision == "approve":
        task["status"] = "done"
        task["completed_at"] = event["ts"]
    elif decision == "request_changes":
        task["status"] = "in_progress"
    else:
        raise ESAAError("INVALID_TRANSITION", f"review decision invalid: {decision}")


def _apply_issue_report(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    issue_id = payload["issue_id"]
    issue = state["_issues"].setdefault(
        issue_id,
        {
            "issue_id": issue_id,
            "status": "open",
            "severity": payload.get("severity", "medium"),
            "title": payload.get("title", issue_id),
            "baseline_id": payload.get("affected", {}).get("baseline_id"),
            "affected": deepcopy(payload.get("affected", {})),
            "evidence": deepcopy(payload.get("evidence", {})),
            "resolution": None,
            "links": {
                "reported_by_task_id": payload.get("task_id"),
                "fixes_task_id": payload.get("fixes"),
                "hotfix_task_id": None,
            },
            "timeline": {
                "created_event_seq": event["event_seq"],
                "resolved_event_seq": None,
            },
        },
    )
    issue["status"] = "open"
    issue["severity"] = payload.get("severity", issue["severity"])
    issue["title"] = payload.get("title", issue["title"])
    issue["evidence"] = deepcopy(payload.get("evidence", issue.get("evidence", {})))

    if "lesson" in payload and isinstance(payload["lesson"], dict):
        _upsert_lesson_from_issue_report(state=state, event=event)


def _normalize_lesson_status(value: Any) -> str:
    allowed = {"proposed", "active", "superseded"}
    text = str(value or "").strip().lower()
    if text in allowed:
        return text
    if text == "archived":
        return "superseded"
    return "active"


def _lesson_signature(payload: dict[str, Any], lesson_payload: dict[str, Any]) -> dict[str, Any]:
    scope = lesson_payload.get("scope", {})
    enforcement = lesson_payload.get("enforcement", {})
    task_kinds = list(scope.get("task_kinds", []))
    task_kinds_sorted = sorted(str(kind) for kind in task_kinds)
    return {
        "issue_id": payload.get("issue_id"),
        "rule": lesson_payload.get("rule"),
        "scope_task_kinds": task_kinds_sorted,
        "enforcement_mode": enforcement.get("mode"),
        "enforcement_applies_to": enforcement.get("applies_to"),
    }


def _derive_lesson_id(payload: dict[str, Any], lesson_payload: dict[str, Any]) -> str:
    explicit_id = lesson_payload.get("lesson_id")
    if isinstance(explicit_id, str) and explicit_id.strip():
        return explicit_id.strip()
    digest = sha256_hex(_lesson_signature(payload, lesson_payload))[:12]
    return f"LES-{digest}"


def _upsert_lesson_from_issue_report(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    lesson_payload = deepcopy(payload["lesson"])
    lesson_id = _derive_lesson_id(payload, lesson_payload)

    existing = None
    for lesson in state["_lessons"]:
        if lesson.get("lesson_id") == lesson_id:
            existing = lesson
            break

    source_ref = {
        "event_id": event["event_id"],
        "event_seq": event["event_seq"],
        "issue_id": payload.get("issue_id"),
        "task_id": payload.get("task_id"),
    }

    lesson_title = payload.get("title") or lesson_payload.get("rule") or lesson_id
    normalized_scope = deepcopy(lesson_payload.get("scope", {}))
    normalized_scope["task_kinds"] = sorted(str(kind) for kind in normalized_scope.get("task_kinds", []))

    if existing is None:
        state["_lessons"].append(
            {
                "lesson_id": lesson_id,
                "status": _normalize_lesson_status(lesson_payload.get("status", "active")),
                "created_at": event["ts"],
                "updated_at": event["ts"],
                "title": lesson_title,
                "mistake": lesson_payload.get("mistake", ""),
                "rule": lesson_payload.get("rule", ""),
                "scope": normalized_scope,
                "enforcement": deepcopy(lesson_payload.get("enforcement", {})),
                "source_refs": [source_ref],
            }
        )
        return

    existing["status"] = _normalize_lesson_status(lesson_payload.get("status", existing.get("status")))
    existing["updated_at"] = event["ts"]
    existing["title"] = lesson_title
    existing["mistake"] = lesson_payload.get("mistake", existing.get("mistake", ""))
    existing["rule"] = lesson_payload.get("rule", existing.get("rule", ""))
    existing["scope"] = normalized_scope
    existing["enforcement"] = deepcopy(lesson_payload.get("enforcement", existing.get("enforcement", {})))
    refs = list(existing.get("source_refs", []))
    if not any(ref.get("event_id") == source_ref["event_id"] for ref in refs):
        refs.append(source_ref)
    existing["source_refs"] = refs


def _apply_hotfix_create(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    task_id = payload["task_id"]
    for task in state["tasks"]:
        if task["task_id"] == task_id:
            raise ESAAError("DUPLICATE_TASK", f"task already exists: {task_id}")
    state["tasks"].append(_new_task(payload))
    issue_id = payload.get("issue_id")
    if issue_id and issue_id in state["_issues"]:
        state["_issues"][issue_id]["links"]["hotfix_task_id"] = task_id


def _apply_issue_resolve(state: dict[str, Any], event: dict[str, Any]) -> None:
    payload = event["payload"]
    issue_id = payload["issue_id"]
    issue = state["_issues"].get(issue_id)
    if not issue:
        raise ESAAError("ISSUE_NOT_FOUND", f"issue not found: {issue_id}")
    issue["status"] = "resolved"
    issue["resolution"] = deepcopy(payload.get("resolution", {}))
    issue["timeline"]["resolved_event_seq"] = event["event_seq"]


def _apply_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    action = event["action"]
    payload = event["payload"]

    if action == "run.start":
        state["meta"]["master_correlation_id"] = payload.get("master_correlation_id")
        state["meta"]["run"]["run_id"] = payload.get("run_id", state["meta"]["run"]["run_id"])
        state["meta"]["run"]["status"] = payload.get("status", "initialized")
    elif action == "run.end":
        state["meta"]["run"]["status"] = payload.get("status", "success")
    elif action == "task.create":
        state["tasks"].append(_new_task(payload))
    elif action == "claim":
        _apply_claim(state, event)
    elif action == "complete":
        _apply_complete(state, event)
    elif action == "review":
        _apply_review(state, event)
    elif action == "issue.report":
        _apply_issue_report(state, event)
    elif action == "hotfix.create":
        _apply_hotfix_create(state, event)
    elif action == "issue.resolve":
        _apply_issue_resolve(state, event)
    elif action == "verify.ok":
        state["meta"]["run"]["verify_status"] = "ok"
    elif action == "verify.fail":
        state["meta"]["run"]["verify_status"] = payload.get("verify_status", "mismatch")
    elif action in {"output.rejected", "orchestrator.file.write", "orchestrator.view.mutate", "verify.start"}:
        pass
    else:
        raise ESAAError("UNKNOWN_ACTION", f"unknown action: {action}")

    state["meta"]["run"]["last_event_seq"] = event["event_seq"]
    state["meta"]["updated_at"] = event["ts"]


def materialize(events: list[dict[str, Any]], project_name: str = "esaa-core") -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    state = _empty_state(project_name=project_name)
    for event in events:
        _apply_event(state, event)

    state["indexes"]["by_status"] = _index_counts(state["tasks"], "status")
    state["indexes"]["by_kind"] = _index_counts(state["tasks"], "task_kind")

    roadmap = {
        "meta": deepcopy(state["meta"]),
        "project": deepcopy(state["project"]),
        "tasks": deepcopy(state["tasks"]),
        "indexes": deepcopy(state["indexes"]),
    }
    roadmap["meta"]["run"]["verify_status"] = normalize_legacy_verify_status(roadmap["meta"]["run"]["verify_status"])
    roadmap["meta"]["run"]["projection_hash_sha256"] = compute_projection_hash(roadmap)

    issues = sorted(state["_issues"].values(), key=lambda issue: issue["issue_id"])
    open_by_baseline: dict[str, list[str]] = {}
    for issue in issues:
        if issue["status"] != "open":
            continue
        baseline = issue.get("baseline_id") or "unknown"
        open_by_baseline.setdefault(baseline, []).append(issue["issue_id"])

    issues_view = {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "esaa_version": ESAA_VERSION,
            "generated_by": "esaa.project",
            "source_event_store": ".roadmap/activity.jsonl",
            "last_event_seq": roadmap["meta"]["run"]["last_event_seq"],
            "updated_at": roadmap["meta"]["updated_at"],
        },
        "issues": issues,
        "indexes": {"open_by_baseline": dict(sorted(open_by_baseline.items(), key=lambda item: item[0]))},
    }

    by_task_kind: dict[str, list[str]] = {}
    by_enforcement: dict[str, list[str]] = {}
    by_status: dict[str, list[str]] = {}
    lessons = sorted(deepcopy(state["_lessons"]), key=lambda item: item["lesson_id"])
    for lesson in lessons:
        by_status.setdefault(lesson["status"], []).append(lesson["lesson_id"])
        for kind in lesson["scope"].get("task_kinds", []):
            by_task_kind.setdefault(kind, []).append(lesson["lesson_id"])
        applies_to = lesson["enforcement"]["applies_to"]
        by_enforcement.setdefault(applies_to, []).append(lesson["lesson_id"])

    lessons_view = {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "esaa_version": ESAA_VERSION,
            "generated_by": "esaa.project",
            "source_event_store": ".roadmap/activity.jsonl",
            "updated_at": roadmap["meta"]["updated_at"],
        },
        "lessons": lessons,
        "indexes": {
            "by_task_kind": dict(sorted(by_task_kind.items(), key=lambda item: item[0])),
            "by_enforcement_applies_to": dict(sorted(by_enforcement.items(), key=lambda item: item[0])),
            "by_status": dict(sorted(by_status.items(), key=lambda item: item[0])),
        },
    }
    return roadmap, issues_view, lessons_view
