from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .adapters.base import AgentAdapter
from .adapters.mock import MockAgentAdapter
from .constants import ESAA_VERSION, SCHEMA_VERSION
from .errors import CorruptedStoreError, ESAAError
from .projector import materialize
from .store import (
    append_events,
    ensure_event_store,
    load_agent_contract,
    load_agent_result_schema,
    load_lessons,
    load_roadmap,
    next_event_seq,
    parse_event_store,
    save_issues,
    save_lessons,
    save_roadmap,
)
from .utils import ensure_parent, normalize_rel_path, sha256_hex, utc_now_iso
from .validator import validate_agent_output
from .memory import SemanticMemory
from .lesson_engine import LessonEngine


class ESAAService:
    def __init__(self, root: Path, adapter: AgentAdapter | None = None) -> None:
        self.root = root
        self.adapter = adapter or MockAgentAdapter()

    def init(self, run_id: str = "RUN-0001", master_correlation_id: str = "CID-ESAA-INIT", force: bool = False) -> dict[str, Any]:
        roadmap_dir = self.root / ".roadmap"
        roadmap_dir.mkdir(parents=True, exist_ok=True)

        if not force and (self.root / ".roadmap/activity.jsonl").exists():
            existing = (self.root / ".roadmap/activity.jsonl").read_text(encoding="utf-8").strip()
            if existing:
                raise ESAAError("INIT_BLOCKED", "event store already contains events; use --force to reinitialize")

        for rel in ("docs/spec", "docs/qa", "src", "tests"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)

        events: list[dict[str, Any]] = []
        seq = 1
        events.append(
            make_event(
                seq,
                actor="orchestrator",
                action="run.start",
                payload={
                    "run_id": run_id,
                    "status": "initialized",
                    "master_correlation_id": master_correlation_id,
                    "baseline_id": "B-000",
                },
            )
        )
        seq += 1
        for task in seed_tasks():
            events.append(make_event(seq, actor="orchestrator", action="task.create", payload=task))
            seq += 1

        events.append(
            make_event(
                seq,
                actor="orchestrator",
                action="verify.start",
                payload={"strict": True},
            )
        )
        seq += 1

        roadmap_preview, _, _ = materialize(events)
        events.append(
            make_event(
                seq,
                actor="orchestrator",
                action="verify.ok",
                payload={"projection_hash_sha256": roadmap_preview["meta"]["run"]["projection_hash_sha256"]},
            )
        )

        path = ensure_event_store(self.root)
        path.write_text("", encoding="utf-8")
        append_events(self.root, events)
        roadmap, issues, lessons = materialize(events)
        save_roadmap(self.root, roadmap)
        save_issues(self.root, issues)
        save_lessons(self.root, lessons)
        return {
            "run_id": run_id,
            "events_written": len(events),
            "last_event_seq": roadmap["meta"]["run"]["last_event_seq"],
            "projection_hash_sha256": roadmap["meta"]["run"]["projection_hash_sha256"],
        }

    def project(self) -> dict[str, Any]:
        events = parse_event_store(self.root)
        roadmap, issues, lessons = materialize(events)
        save_roadmap(self.root, roadmap)
        save_issues(self.root, issues)
        save_lessons(self.root, lessons)
        
        # Optional Semantic Memory Sync
        memory = SemanticMemory(self.root)
        mem_synced = memory.sync(events)
        
        # Automated Lesson Proposals
        engine = LessonEngine(self.root)
        suggested = engine.analyze_failures(events, lessons.get("lessons", []))
        
        return {
            "last_event_seq": roadmap["meta"]["run"]["last_event_seq"],
            "projection_hash_sha256": roadmap["meta"]["run"]["projection_hash_sha256"],
            "tasks": len(roadmap["tasks"]),
            "issues": len(issues["issues"]),
            "lessons": len(lessons["lessons"]),
            "suggested_lessons": len(suggested),
            "memory_synced": mem_synced,
            "_suggested": suggested
        }

    def doctor(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def add_check(name: str, status: str, detail: str, required: bool, next_step: str | None = None) -> None:
            entry: dict[str, Any] = {
                "name": name,
                "status": status,
                "required": required,
                "detail": detail,
            }
            if next_step:
                entry["next_step"] = next_step
            checks.append(entry)

        for command in ("python", "git"):
            found = shutil.which(command)
            add_check(
                name=f"bin:{command}",
                status="ok" if found else "fail",
                detail=found or "not found in PATH",
                required=True,
                next_step=f"Install {command} and ensure it is available in PATH.",
            )

        for command in ("node", "npm", "gh"):
            found = shutil.which(command)
            add_check(
                name=f"bin:{command}",
                status="ok" if found else "warn",
                detail=found or "not found in PATH",
                required=False,
                next_step=f"Install {command} for improved workflow support.",
            )

        roadmap_dir = self.root / ".roadmap"
        add_check(
            name="path:.roadmap",
            status="ok" if roadmap_dir.exists() else "fail",
            detail=str(roadmap_dir),
            required=True,
            next_step="Run `esaa init` or bootstrap at the project root.",
        )

        for filename in (
            "AGENT_CONTRACT.yaml",
            "ORCHESTRATOR_CONTRACT.yaml",
            "RUNTIME_POLICY.yaml",
            "STORAGE_POLICY.yaml",
            "agent_result.schema.json",
            "roadmap.schema.json",
            "issues.schema.json",
            "lessons.schema.json",
        ):
            path = roadmap_dir / filename
            add_check(
                name=f"artifact:{filename}",
                status="ok" if path.exists() else "fail",
                detail=str(path),
                required=True,
                next_step=f"Restore missing .roadmap/{filename} from canonical framework files.",
            )

        has_fail = any(item["required"] and item["status"] == "fail" for item in checks)
        has_warn = any(item["status"] == "warn" for item in checks)
        status = "fail" if has_fail else ("warn" if has_warn else "ok")
        next_steps = [item["next_step"] for item in checks if item.get("next_step") and item["status"] in {"fail", "warn"}]
        return {
            "status": status,
            "project_root": str(self.root),
            "checks": checks,
            "next_steps": next_steps,
        }

    def memory_search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        memory = SemanticMemory(self.root)
        return memory.search(query, top_k=top_k)

    def runtime_profiles(self) -> dict[str, Any]:
        catalog = _runtime_profile_catalog()
        return {
            "status": "ok",
            "profiles": sorted(catalog.keys()),
            "actions": ["start", "build", "test"],
        }

    def runtime_command(self, stack: str, action: str) -> dict[str, Any]:
        catalog = _runtime_profile_catalog()
        profile = catalog.get(stack)
        if profile is None:
            raise ESAAError("RUNTIME_STACK_NOT_FOUND", f"runtime stack not found: {stack}")
        command = profile.get(action)
        if command is None:
            raise ESAAError("RUNTIME_ACTION_NOT_FOUND", f"action {action} not available for stack {stack}")
        return {
            "status": "ok",
            "stack": stack,
            "action": action,
            "command": command,
            "notes": profile.get("_notes", ""),
        }

    def github_check(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        git_path = shutil.which("git")
        gh_path = shutil.which("gh")
        checks.append({"name": "bin:git", "status": "ok" if git_path else "fail", "detail": git_path or "not found"})
        checks.append({"name": "bin:gh", "status": "ok" if gh_path else "warn", "detail": gh_path or "not found"})

        in_repo = False
        branch = None
        remote_origin = None
        if git_path:
            probe = _run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.root)
            in_repo = probe["ok"] and probe["stdout"].strip() == "true"
            checks.append(
                {
                    "name": "git:inside_repo",
                    "status": "ok" if in_repo else "fail",
                    "detail": probe["stdout"].strip() or probe["stderr"].strip(),
                }
            )
            if in_repo:
                branch_probe = _run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.root)
                if branch_probe["ok"]:
                    branch = branch_probe["stdout"].strip()
                checks.append(
                    {
                        "name": "git:current_branch",
                        "status": "ok" if branch else "warn",
                        "detail": branch or branch_probe["stderr"].strip() or "unknown",
                    }
                )

                remote_probe = _run_command(["git", "remote", "get-url", "origin"], cwd=self.root)
                if remote_probe["ok"]:
                    remote_origin = remote_probe["stdout"].strip()
                checks.append(
                    {
                        "name": "git:origin_remote",
                        "status": "ok" if remote_origin else "warn",
                        "detail": remote_origin or "origin not configured",
                    }
                )

        gh_auth = None
        if gh_path:
            auth_probe = _run_command(["gh", "auth", "status", "-h", "github.com"], cwd=self.root)
            gh_auth = auth_probe["ok"]
            checks.append(
                {
                    "name": "gh:auth",
                    "status": "ok" if gh_auth else "warn",
                    "detail": "authenticated" if gh_auth else (auth_probe["stderr"].strip() or "not authenticated"),
                }
            )

        has_fail = any(item["status"] == "fail" for item in checks)
        has_warn = any(item["status"] == "warn" for item in checks)
        status = "fail" if has_fail else ("warn" if has_warn else "ok")
        return {
            "status": status,
            "project_root": str(self.root),
            "in_repo": in_repo,
            "branch": branch,
            "origin": remote_origin,
            "checks": checks,
        }

    def github_publish(
        self,
        repo: str | None = None,
        remote: str = "origin",
        branch: str | None = None,
        visibility: str = "private",
        use_gh: bool = True,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        repo_probe = _run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.root)
        if not repo_probe["ok"] or repo_probe["stdout"].strip() != "true":
            raise ESAAError("GITHUB_NOT_REPO", "current root is not a git repository")

        branch_name = branch
        if not branch_name:
            branch_probe = _run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.root)
            if branch_probe["ok"] and branch_probe["stdout"].strip():
                branch_name = branch_probe["stdout"].strip()
            else:
                unborn_probe = _run_command(["git", "symbolic-ref", "--short", "HEAD"], cwd=self.root)
                if unborn_probe["ok"] and unborn_probe["stdout"].strip():
                    branch_name = unborn_probe["stdout"].strip()
                else:
                    raise ESAAError("GITHUB_COMMAND_FAILED", "unable to determine current branch")

        remote_probe = _run_command(["git", "remote", "get-url", remote], cwd=self.root)
        remote_exists = remote_probe["ok"]
        gh_available = shutil.which("gh") is not None and use_gh

        steps: list[list[str]] = []
        mode = "gh" if gh_available else "fallback"
        if not remote_exists:
            if gh_available and repo:
                steps.append(["gh", "repo", "create", repo, f"--{visibility}", "--source", ".", "--remote", remote])
            else:
                if not repo:
                    raise ESAAError("GITHUB_REMOTE_REQUIRED", "remote not configured and --repo was not provided")
                remote_url = repo if repo.startswith("http") or repo.startswith("git@") else f"https://github.com/{repo}.git"
                steps.append(["git", "remote", "add", remote, remote_url])

        steps.append(["git", "push", "-u", remote, branch_name])

        if dry_run:
            return {
                "status": "planned",
                "mode": mode,
                "branch": branch_name,
                "remote": remote,
                "remote_exists": remote_exists,
                "steps": [" ".join(step) for step in steps],
            }

        executed: list[dict[str, Any]] = []
        for step in steps:
            result = _run_command(step, cwd=self.root)
            executed.append({"command": " ".join(step), "ok": result["ok"], "stdout": result["stdout"], "stderr": result["stderr"]})
            if not result["ok"]:
                raise ESAAError("GITHUB_COMMAND_FAILED", f"command failed: {' '.join(step)}")
        return {
            "status": "ok",
            "mode": mode,
            "branch": branch_name,
            "remote": remote,
            "steps_executed": executed,
        }

    def lessons_suggest(self) -> dict[str, Any]:
        events = parse_event_store(self.root)
        _, _, lessons_view = materialize(events)
        engine = LessonEngine(self.root)
        suggestions = engine.analyze_failures(events, lessons_view.get("lessons", []))
        return {
            "status": "ok",
            "suggested_lessons": len(suggestions),
            "suggestions": suggestions,
        }

    def lessons_promote(self, all_suggestions: bool = False, limit: int = 1, dry_run: bool = False) -> dict[str, Any]:
        events = parse_event_store(self.root)
        roadmap, issues_view, lessons_view = materialize(events)
        engine = LessonEngine(self.root)
        suggestions = engine.analyze_failures(events, lessons_view.get("lessons", []))
        if not suggestions:
            return {
                "status": "noop",
                "promoted": 0,
                "reason": "no_suggestions",
                "last_event_seq": roadmap["meta"]["run"]["last_event_seq"],
                "verify_status": roadmap["meta"]["run"]["verify_status"],
            }

        if all_suggestions:
            to_promote = suggestions
        else:
            safe_limit = max(1, int(limit))
            to_promote = suggestions[:safe_limit]

        existing_issue_ids = {item.get("issue_id") for item in issues_view.get("issues", [])}
        new_events: list[dict[str, Any]] = []
        base_events = events

        for suggestion in to_promote:
            lesson_id = suggestion["lesson_id"]
            source_ref = (suggestion.get("source_refs") or [{}])[0]
            task_id = source_ref.get("task_id", "T-UNKNOWN")
            issue_id = source_ref.get("issue_id") or f"ISS-SUG-{lesson_id[-6:].upper()}"
            if issue_id in existing_issue_ids:
                issue_id = f"{issue_id}-{len(existing_issue_ids) + 1}"
            existing_issue_ids.add(issue_id)

            report_payload = {
                "action": "issue.report",
                "task_id": task_id,
                "issue_id": issue_id,
                "severity": "low",
                "title": f"Lesson Promotion {lesson_id}",
                "affected": {"baseline_id": "B-000", "environment": "lessons-promotion", "paths": []},
                "evidence": {
                    "symptom": suggestion.get("mistake", "lesson promotion"),
                    "repro_steps": [suggestion.get("rule", "review event trail")],
                },
                "lesson": {
                    "lesson_id": lesson_id,
                    "status": "active",
                    "mistake": suggestion.get("mistake", ""),
                    "rule": suggestion.get("rule", ""),
                    "scope": suggestion.get("scope", {"task_kinds": ["spec", "impl", "qa"]}),
                    "enforcement": suggestion.get("enforcement", {"mode": "reject", "applies_to": "workflow_gate"}),
                },
            }
            report_event = make_event(
                next_event_seq(base_events + new_events),
                actor="orchestrator",
                action="issue.report",
                payload=report_payload,
            )
            new_events.append(report_event)

            resolve_event = make_event(
                next_event_seq(base_events + new_events),
                actor="orchestrator",
                action="issue.resolve",
                payload={
                    "issue_id": issue_id,
                    "resolution": {
                        "status": "resolved",
                        "summary": f"Lesson {lesson_id} promoted to active.",
                    },
                },
            )
            new_events.append(resolve_event)

        verify_start = make_event(
            next_event_seq(base_events + new_events),
            actor="orchestrator",
            action="verify.start",
            payload={"strict": True},
        )
        new_events.append(verify_start)

        preview_roadmap, _, _ = materialize(base_events + new_events)
        verify_ok = make_event(
            next_event_seq(base_events + new_events),
            actor="orchestrator",
            action="verify.ok",
            payload={"projection_hash_sha256": preview_roadmap["meta"]["run"]["projection_hash_sha256"]},
        )
        new_events.append(verify_ok)

        final_roadmap, final_issues, final_lessons = materialize(base_events + new_events)
        if not dry_run:
            append_events(self.root, new_events)
            save_roadmap(self.root, final_roadmap)
            save_issues(self.root, final_issues)
            save_lessons(self.root, final_lessons)

        return {
            "status": "accepted",
            "promoted": len(to_promote),
            "events_appended": len(new_events),
            "last_event_seq": final_roadmap["meta"]["run"]["last_event_seq"],
            "verify_status": final_roadmap["meta"]["run"]["verify_status"],
            "projection_hash_sha256": final_roadmap["meta"]["run"]["projection_hash_sha256"],
        }

    def mutate(self, target: str, change: str, summary: str, files: list[str] | None = None, resolves: str | None = None) -> dict[str, Any]:
        events = parse_event_store(self.root)
        seq = next_event_seq(events)
        
        payload = {
            "target": target,
            "change": change,
            "summary": summary
        }
        if files:
            payload["files_changed"] = files
        if resolves:
            payload["resolves"] = resolves
            
        mutate_event = make_event(
            seq,
            actor="orchestrator",
            action="orchestrator.view.mutate",
            payload=payload
        )
        
        append_events(self.root, [mutate_event])
        return self.project()

    def verify(self) -> dict[str, Any]:
        try:
            events = parse_event_store(self.root)
            projected, _, projected_lessons = materialize(events)
        except CorruptedStoreError as exc:
            return {
                "verify_status": "corrupted",
                "error_code": exc.code,
                "error_message": exc.message,
                "last_event_seq": None,
                "projection_hash_sha256": None,
            }

        stored = load_roadmap(self.root)
        if stored is None:
            return {
                "verify_status": "mismatch",
                "reason": "roadmap_missing",
                "last_event_seq": projected["meta"]["run"]["last_event_seq"],
                "projection_hash_sha256": projected["meta"]["run"]["projection_hash_sha256"],
            }

        computed_hash = projected["meta"]["run"]["projection_hash_sha256"]
        stored_hash = stored.get("meta", {}).get("run", {}).get("projection_hash_sha256")
        computed_seq = projected["meta"]["run"]["last_event_seq"]
        stored_seq = stored.get("meta", {}).get("run", {}).get("last_event_seq")
        projected_lessons_hash = sha256_hex(projected_lessons)
        stored_lessons = load_lessons(self.root)
        if stored_lessons is None:
            return {
                "verify_status": "mismatch",
                "reason": "lessons_missing",
                "last_event_seq": computed_seq,
                "projection_hash_sha256": computed_hash,
            }
        stored_lessons_hash = sha256_hex(stored_lessons)

        if (
            computed_hash == stored_hash
            and computed_seq == stored_seq
            and projected_lessons_hash == stored_lessons_hash
        ):
            return {
                "verify_status": "ok",
                "last_event_seq": computed_seq,
                "projection_hash_sha256": computed_hash,
                "lessons_hash_sha256": projected_lessons_hash,
            }
        return {
            "verify_status": "mismatch",
            "last_event_seq": computed_seq,
            "projection_hash_sha256": computed_hash,
            "stored_last_event_seq": stored_seq,
            "stored_projection_hash_sha256": stored_hash,
            "lessons_hash_sha256": projected_lessons_hash,
            "stored_lessons_hash_sha256": stored_lessons_hash,
        }

    def replay(self, until: str | None = None, write_views: bool = True) -> dict[str, Any]:
        events = parse_event_store(self.root)
        selected = events
        if until:
            if until.isdigit():
                seq_limit = int(until)
                selected = [ev for ev in events if int(ev["event_seq"]) <= seq_limit]
            else:
                out: list[dict[str, Any]] = []
                for event in events:
                    out.append(event)
                    if event["event_id"] == until:
                        break
                selected = out
        roadmap, issues, lessons = materialize(selected)
        if write_views:
            save_roadmap(self.root, roadmap)
            save_issues(self.root, issues)
            save_lessons(self.root, lessons)
        return {
            "events_replayed": len(selected),
            "last_event_seq": roadmap["meta"]["run"]["last_event_seq"],
            "projection_hash_sha256": roadmap["meta"]["run"]["projection_hash_sha256"],
            "verify_status": "ok",
        }

    def create_task(
        self,
        task_id: str,
        task_kind: str,
        title: str,
        description: str,
        depends_on: list[str] | None = None,
        targets: list[str] | None = None,
        output_files: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        clean_task_id = task_id.strip()
        if not clean_task_id:
            raise ESAAError("INVALID_ARGUMENT", "task_id must not be empty")
        if task_kind not in {"spec", "impl", "qa"}:
            raise ESAAError("INVALID_ARGUMENT", f"invalid task kind: {task_kind}")

        events = parse_event_store(self.root)
        roadmap, _, _ = materialize(events)
        existing = {task["task_id"] for task in roadmap["tasks"]}
        if clean_task_id in existing:
            raise ESAAError("TASK_ALREADY_EXISTS", f"task already exists: {clean_task_id}")

        depends = [item for item in (depends_on or []) if item]
        for dep in depends:
            if dep not in existing:
                raise ESAAError("DEPENDENCY_NOT_FOUND", f"depends_on task not found: {dep}")

        normalized_outputs = [normalize_rel_path(path) for path in (output_files or []) if path]
        if not normalized_outputs:
            defaults = {
                "spec": f"docs/spec/{clean_task_id}.md",
                "impl": f"src/{clean_task_id}.txt",
                "qa": f"docs/qa/{clean_task_id}.md",
            }
            normalized_outputs = [defaults[task_kind]]

        task_targets = [item for item in (targets or []) if item] or [f"{task_kind}-incremental"]
        payload = {
            "task_id": clean_task_id,
            "task_kind": task_kind,
            "title": title,
            "description": description,
            "depends_on": depends,
            "targets": task_targets,
            "outputs": {"files": normalized_outputs},
        }

        new_events: list[dict[str, Any]] = []
        new_events.append(
            make_event(
                next_event_seq(events),
                actor="orchestrator",
                action="task.create",
                payload=payload,
            )
        )
        new_events.append(
            make_event(
                next_event_seq(events + new_events),
                actor="orchestrator",
                action="verify.start",
                payload={"strict": True},
            )
        )
        preview_roadmap, _, _ = materialize(events + new_events)
        new_events.append(
            make_event(
                next_event_seq(events + new_events),
                actor="orchestrator",
                action="verify.ok",
                payload={"projection_hash_sha256": preview_roadmap["meta"]["run"]["projection_hash_sha256"]},
            )
        )

        final_roadmap, final_issues, final_lessons = materialize(events + new_events)
        if not dry_run:
            append_events(self.root, new_events)
            save_roadmap(self.root, final_roadmap)
            save_issues(self.root, final_issues)
            save_lessons(self.root, final_lessons)

        return {
            "status": "accepted",
            "action": "task.create",
            "task_id": clean_task_id,
            "events_appended": len(new_events),
            "last_event_seq": final_roadmap["meta"]["run"]["last_event_seq"],
            "verify_status": final_roadmap["meta"]["run"]["verify_status"],
            "projection_hash_sha256": final_roadmap["meta"]["run"]["projection_hash_sha256"],
        }

    def submit(self, agent_output: dict[str, Any], actor: str, dry_run: bool = False) -> dict[str, Any]:
        """Validate and apply a single agent.result submitted externally.

        This is the primary interface for real LLM agents (Claude Code,
        Codex, Gemini Code, etc.) that read .roadmap/ and produce a
        structured JSON output following agent_result.schema.json.
        """
        events = parse_event_store(self.root)
        contract = load_agent_contract(self.root)
        schema = load_agent_result_schema(self.root)
        roadmap, _, _ = materialize(events)

        activity_event = agent_output.get("activity_event", {})
        task_id = activity_event.get("task_id")
        if not task_id:
            raise ESAAError("SCHEMA_INVALID", "activity_event.task_id is required")

        task = None
        for t in roadmap["tasks"]:
            if t["task_id"] == task_id:
                task = t
                break
        if not task:
            raise ESAAError("TASK_NOT_FOUND", f"task_id not found: {task_id}")

        current_seq = next_event_seq(events)
        new_events: list[dict[str, Any]] = []
        files_written = 0

        try:
            validated_event, file_updates = validate_agent_output(agent_output, schema, contract, task)
            agent_event = make_event(
                current_seq,
                actor=actor,
                action=validated_event["action"],
                payload=validated_event,
            )
            candidate_events = [agent_event]
            _ = materialize(events + candidate_events)

            if file_updates:
                write_event = make_event(
                    current_seq + 1,
                    actor="orchestrator",
                    action="orchestrator.file.write",
                    payload={
                        "task_id": task_id,
                        "files": [normalize_rel_path(item["path"]) for item in file_updates],
                    },
                )
                candidate_events.append(write_event)
                _ = materialize(events + candidate_events)

                if not dry_run:
                    for item in file_updates:
                        path = self.root / normalize_rel_path(item["path"])
                        ensure_parent(path)
                        path.write_text(item["content"], encoding="utf-8")
                        files_written += 1

            if validated_event["action"] == "issue.report":
                hotfix_event = build_hotfix_event(events + candidate_events, validated_event)
                if hotfix_event:
                    candidate_events.append(hotfix_event)
                    _ = materialize(events + candidate_events)

            new_events.extend(candidate_events)
        except ESAAError:
            raise

        # Verify after applying
        all_events = events + new_events
        verify_start = make_event(
            next_event_seq(all_events),
            actor="orchestrator",
            action="verify.start",
            payload={"strict": True},
        )
        all_events.append(verify_start)
        new_events.append(verify_start)

        final_roadmap, final_issues, final_lessons = materialize(all_events)

        # Check if all tasks done -> run.end
        if all_tasks_done(final_roadmap["tasks"]) and final_roadmap["meta"]["run"]["status"] != "success":
            run_end = make_event(
                next_event_seq(all_events),
                actor="orchestrator",
                action="run.end",
                payload={"status": "success"},
            )
            all_events.append(run_end)
            new_events.append(run_end)
            final_roadmap, final_issues, final_lessons = materialize(all_events)

        verify_ok = make_event(
            next_event_seq(all_events),
            actor="orchestrator",
            action="verify.ok",
            payload={"projection_hash_sha256": final_roadmap["meta"]["run"]["projection_hash_sha256"]},
        )
        all_events.append(verify_ok)
        new_events.append(verify_ok)
        final_roadmap, final_issues, final_lessons = materialize(all_events)

        if not dry_run:
            append_events(self.root, new_events)
            save_roadmap(self.root, final_roadmap)
            save_issues(self.root, final_issues)
            save_lessons(self.root, final_lessons)

        return {
            "status": "accepted",
            "actor": actor,
            "task_id": task_id,
            "action": validated_event["action"],
            "events_appended": len(new_events),
            "files_written": files_written,
            "last_event_seq": final_roadmap["meta"]["run"]["last_event_seq"],
            "verify_status": final_roadmap["meta"]["run"]["verify_status"],
            "projection_hash_sha256": final_roadmap["meta"]["run"]["projection_hash_sha256"],
        }

    def process(self, dry_run: bool = False) -> dict[str, Any]:
        """Process all pending agent.result files from .roadmap/inbox/.

        Each file must be named {task_id}.json or {actor}__{task_id}.json
        and contain a valid agent_result.schema.json payload.
        Accepted files are moved to .roadmap/inbox/done/.
        Rejected files are moved to .roadmap/inbox/rejected/.
        """
        inbox = self.root / ".roadmap" / "inbox"
        if not inbox.exists():
            return {"processed": 0, "accepted": 0, "rejected": 0, "results": []}

        done_dir = inbox / "done"
        rejected_dir = inbox / "rejected"
        done_dir.mkdir(parents=True, exist_ok=True)
        rejected_dir.mkdir(parents=True, exist_ok=True)

        files = sorted(inbox.glob("*.json"))
        results: list[dict[str, Any]] = []
        accepted = 0
        rejected = 0

        for filepath in files:
            name = filepath.stem  # e.g., "T-1000" or "agent-spec__T-1000"
            if "__" in name:
                actor, _task_id = name.split("__", 1)
            else:
                actor = "agent-external"

            try:
                agent_output = json.loads(filepath.read_text(encoding="utf-8"))
                result = self.submit(agent_output, actor=actor, dry_run=dry_run)
                results.append(result)
                accepted += 1
                if not dry_run:
                    filepath.rename(done_dir / filepath.name)
            except (ESAAError, json.JSONDecodeError) as exc:
                error_info = {
                    "status": "rejected",
                    "file": filepath.name,
                    "error": str(exc),
                }
                if isinstance(exc, ESAAError):
                    error_info["error_code"] = exc.code
                    error_info["error"] = exc.message
                results.append(error_info)
                rejected += 1
                if not dry_run:
                    filepath.rename(rejected_dir / filepath.name)

        return {
            "processed": len(files),
            "accepted": accepted,
            "rejected": rejected,
            "results": results,
        }

    def run(self, steps: int = 1, dry_run: bool = False) -> dict[str, Any]:
        if steps < 1:
            raise ESAAError("INVALID_ARGUMENT", "steps must be >= 1")

        events = parse_event_store(self.root)
        contract = load_agent_contract(self.root)
        schema = load_agent_result_schema(self.root)
        new_events: list[dict[str, Any]] = []
        files_written = 0
        rejected = 0
        executed = 0

        for _ in range(steps):
            roadmap, _, _ = materialize(events + new_events)
            task = select_next_task(roadmap["tasks"])
            if not task:
                break
            executed += 1
            context = build_dispatch_context(roadmap, task, contract, root=self.root)
            current_seq = next_event_seq(events + new_events)

            output: dict[str, Any] | None = None
            try:
                output = self.adapter.execute(context)
                activity_event, file_updates = validate_agent_output(output, schema, contract, task)
                agent_event = make_event(
                    current_seq,
                    actor=self.adapter.agent_id,
                    action=activity_event["action"],
                    payload=activity_event,
                )
                candidate_events = [agent_event]
                _ = materialize(events + new_events + candidate_events)

                if file_updates:
                    write_event = make_event(
                        current_seq + 1,
                        actor="orchestrator",
                        action="orchestrator.file.write",
                        payload={
                            "task_id": task["task_id"],
                            "files": [normalize_rel_path(item["path"]) for item in file_updates],
                        },
                    )
                    candidate_events.append(write_event)
                    _ = materialize(events + new_events + candidate_events)

                    if not dry_run:
                        for item in file_updates:
                            path = self.root / normalize_rel_path(item["path"])
                            ensure_parent(path)
                            path.write_text(item["content"], encoding="utf-8")
                            files_written += 1

                if activity_event["action"] == "issue.report":
                    hotfix_event = build_hotfix_event(events + new_events + candidate_events, activity_event)
                    if hotfix_event:
                        candidate_events.append(hotfix_event)
                        _ = materialize(events + new_events + candidate_events)

                new_events.extend(candidate_events)
            except ESAAError as exc:
                rejected += 1
                reject_event = make_event(
                    current_seq,
                    actor="orchestrator",
                    action="output.rejected",
                    payload={
                        "task_id": task["task_id"],
                        "error_code": exc.code,
                        "message": exc.message,
                        "source_action": output.get("activity_event", {}).get("action", "unknown") if isinstance(output, dict) else "unknown",
                    },
                )
                new_events.append(reject_event)
            except ValueError as exc:
                rejected += 1
                reject_event = make_event(
                    current_seq,
                    actor="orchestrator",
                    action="output.rejected",
                    payload={
                        "task_id": task["task_id"],
                        "error_code": "LLM_PARSE_FAILED",
                        "message": str(exc),
                        "source_action": "unknown",
                    },
                )
                new_events.append(reject_event)

        final_events = events + new_events
        final_roadmap, final_issues, final_lessons = materialize(final_events)
        if all_tasks_done(final_roadmap["tasks"]) and final_roadmap["meta"]["run"]["status"] != "success":
            run_end = make_event(
                next_event_seq(final_events),
                actor="orchestrator",
                action="run.end",
                payload={"status": "success"},
            )
            final_events.append(run_end)
            new_events.append(run_end)
            final_roadmap, final_issues, final_lessons = materialize(final_events)

        verify_start = make_event(
            next_event_seq(final_events),
            actor="orchestrator",
            action="verify.start",
            payload={"strict": True},
        )
        final_events.append(verify_start)
        new_events.append(verify_start)

        final_roadmap, final_issues, final_lessons = materialize(final_events)
        verify_ok = make_event(
            next_event_seq(final_events),
            actor="orchestrator",
            action="verify.ok",
            payload={"projection_hash_sha256": final_roadmap["meta"]["run"]["projection_hash_sha256"]},
        )
        final_events.append(verify_ok)
        new_events.append(verify_ok)
        final_roadmap, final_issues, final_lessons = materialize(final_events)

        if not dry_run:
            append_events(self.root, new_events)
            save_roadmap(self.root, final_roadmap)
            save_issues(self.root, final_issues)
            save_lessons(self.root, final_lessons)

        return {
            "steps_requested": steps,
            "steps_executed": executed,
            "events_appended": len(new_events),
            "rejected": rejected,
            "files_written": files_written,
            "last_event_seq": final_roadmap["meta"]["run"]["last_event_seq"],
            "verify_status": final_roadmap["meta"]["run"]["verify_status"],
            "projection_hash_sha256": final_roadmap["meta"]["run"]["projection_hash_sha256"],
        }


def make_event(event_seq: int, actor: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"EV-{event_seq:08d}",
        "event_seq": event_seq,
        "ts": utc_now_iso(),
        "actor": actor,
        "action": action,
        "payload": payload,
    }


def seed_tasks() -> list[dict[str, Any]]:
    return [
        {
            "task_id": "T-1000",
            "task_kind": "spec",
            "title": "Create initial ESAA spec document",
            "description": "Produce the initial specification artifact for the ESAA core baseline.",
            "depends_on": [],
            "targets": ["spec-core"],
            "outputs": {"files": ["docs/spec/T-1000.md"]},
        },
        {
            "task_id": "T-1010",
            "task_kind": "impl",
            "title": "Create initial implementation artifact",
            "description": "Produce the initial implementation artifact that follows the approved specification.",
            "depends_on": ["T-1000"],
            "targets": ["impl-core"],
            "outputs": {"files": ["src/T-1010.txt"]},
        },
        {
            "task_id": "T-1020",
            "task_kind": "qa",
            "title": "Create initial QA report",
            "description": "Produce the initial QA evidence artifact validating the implementation baseline.",
            "depends_on": ["T-1010"],
            "targets": ["qa-core"],
            "outputs": {"files": ["docs/qa/T-1020.md"]},
        },
    ]


def all_tasks_done(tasks: list[dict[str, Any]]) -> bool:
    return bool(tasks) and all(task["status"] == "done" for task in tasks)


def select_next_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_id = {task["task_id"]: task for task in tasks}

    for status in ("review", "in_progress"):
        candidates = sorted([task for task in tasks if task["status"] == status], key=lambda item: item["task_id"])
        if candidates:
            return candidates[0]

    todo = sorted([task for task in tasks if task["status"] == "todo"], key=lambda item: item["task_id"])
    for task in todo:
        deps = task.get("depends_on", [])
        if all(by_id[dep]["status"] == "done" for dep in deps if dep in by_id):
            return task
    return None


def build_dispatch_context(roadmap: dict[str, Any], task: dict[str, Any], contract: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    boundaries = contract["boundaries"]["by_task_kind"][task["task_kind"]]
    context = {
        "task": task,
        "task_status": task.get("status"),
        "boundaries": {
            "read": boundaries.get("read", []),
            "write": boundaries.get("write", []),
        },
        "context_pack": {
            "run": roadmap["meta"]["run"],
            "project": roadmap["project"],
        },
        "correlation": {
            "master_correlation_id": roadmap["meta"].get("master_correlation_id"),
            "task_id": task["task_id"],
        },
    }
    
    lessons_injection = contract.get("active_lessons_injection", {})
    if root and lessons_injection.get("enabled"):
        lessons_path = root / ".roadmap" / "lessons.json"
        if lessons_path.exists():
            try:
                lessons_payload = json.loads(lessons_path.read_text(encoding="utf-8"))
                lessons = lessons_payload.get("lessons", [])
                active_lessons = []
                for lesson in lessons:
                    if lesson.get("status") != "active":
                        continue
                    task_kinds = lesson.get("scope", {}).get("task_kinds", [])
                    if task["task_kind"] not in task_kinds:
                        continue
                    active_lessons.append(
                        {
                            "lesson_id": lesson.get("lesson_id"),
                            "rule": lesson.get("rule"),
                            "enforcement": lesson.get("enforcement", {}),
                        }
                    )
                if active_lessons:
                    context["active_lessons"] = active_lessons
            except (OSError, json.JSONDecodeError):
                # Keep dispatch deterministic and fail-soft for context enrichment.
                pass

    # Optional Semantic Injection
    if root:
        memory = SemanticMemory(root)
        # Search for context relevant to the task title and description
        search_query = f"{task['title']} {task.get('description', '')}"
        relevant = memory.search(search_query, top_k=3)
        if relevant:
            context["semantic_memory"] = relevant
            
    return context


def build_hotfix_event(current_events: list[dict[str, Any]], issue_payload: dict[str, Any]) -> dict[str, Any] | None:
    issue_id = issue_payload.get("issue_id")
    fixes = issue_payload.get("fixes")
    if not issue_id or not fixes:
        return None

    hotfix_task_id = f"HF-{issue_id}"
    for event in current_events:
        if event["action"] == "hotfix.create" and event["payload"].get("task_id") == hotfix_task_id:
            return None

    seq = next_event_seq(current_events)
    return make_event(
        seq,
        actor="orchestrator",
        action="hotfix.create",
        payload={
            "task_id": hotfix_task_id,
            "task_kind": "impl",
            "title": f"Hotfix for {issue_id}",
            "description": f"Apply a minimal hotfix to resolve issue {issue_id} without regressing immutable done tasks.",
            "depends_on": [],
            "targets": [issue_id],
            "outputs": {"files": [f"src/hotfix/{hotfix_task_id}.txt"]},
            "is_hotfix": True,
            "issue_id": issue_id,
            "fixes": fixes,
            "scope_patch": issue_payload.get("scope_patch", ["src/hotfix/"]),
            "required_verification": issue_payload.get("required_verification", ["unit", "regression"]),
            "baseline_id": issue_payload.get("affected", {}).get("baseline_id", "B-000"),
        },
    )


def _runtime_profile_catalog() -> dict[str, dict[str, str]]:
    return {
        "react-vite": {
            "start": "npm --prefix src/frontend run dev -- --host 127.0.0.1 --port 5173",
            "build": "npm --prefix src/frontend run build",
            "test": "npm --prefix src/frontend run test",
            "_notes": "React + Vite frontend profile.",
        },
        "fastapi": {
            "start": "python -m uvicorn src.backend.main:app --host 127.0.0.1 --port 8000",
            "build": "python -m compileall src/backend",
            "test": "pytest tests/test_tasks_api.py",
            "_notes": "FastAPI backend profile.",
        },
        "fullstack-fastapi-react": {
            "start": "python -m uvicorn src.backend.main:app --host 127.0.0.1 --port 8000",
            "build": "npm --prefix src/frontend run build",
            "test": "pytest tests/test_tasks_api.py",
            "_notes": "Use with frontend command from react-vite profile for dual terminal setup.",
        },
    }


def _run_command(command: list[str], cwd: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": str(exc)}
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def dumps_pretty(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
