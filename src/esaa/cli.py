from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import ESAAError
from .service import ESAAService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="esaa", description="ESAA deterministic orchestrator core")
    parser.add_argument("--root", default=".", help="project root path")

    sub = parser.add_subparsers(dest="command", required=True)

    cmd_init = sub.add_parser("init", help="initialize canonical clean-state")
    cmd_init.add_argument("--run-id", default="RUN-0001")
    cmd_init.add_argument("--master-correlation-id", default="CID-ESAA-INIT")
    cmd_init.add_argument("--force", action="store_true")

    cmd_run = sub.add_parser("run", help="execute orchestration steps (mock adapter)")
    cmd_run.add_argument("--steps", type=int, default=1)
    cmd_run.add_argument("--dry-run", action="store_true")

    cmd_submit = sub.add_parser("submit", help="validate and apply an agent.result JSON")
    cmd_submit.add_argument("file", nargs="?", default="-", help="path to agent.result JSON file (default: stdin)")
    cmd_submit.add_argument("--actor", required=True, help="agent identity (e.g. agent-spec, claude-code)")
    cmd_submit.add_argument("--dry-run", action="store_true", help="validate without persisting")

    cmd_process = sub.add_parser("process", help="process all pending files from .roadmap/inbox/")
    cmd_process.add_argument("--dry-run", action="store_true", help="validate without persisting or moving files")

    sub.add_parser("doctor", help="run environment and project preflight checks")

    cmd_task = sub.add_parser("task", help="task planning utilities")
    task_sub = cmd_task.add_subparsers(dest="task_command", required=True)
    cmd_task_create = task_sub.add_parser("create", help="create a new task incrementally")
    cmd_task_create.add_argument("--task-id", required=True, help="task id (e.g., T-2000)")
    cmd_task_create.add_argument("--kind", required=True, choices=["spec", "impl", "qa"], help="task kind")
    cmd_task_create.add_argument("--title", required=True, help="task title")
    cmd_task_create.add_argument("--description", required=True, help="task description")
    cmd_task_create.add_argument("--depends-on", action="append", default=[], help="dependency task id (repeatable)")
    cmd_task_create.add_argument("--target", action="append", default=[], help="task target label (repeatable)")
    cmd_task_create.add_argument("--output-file", action="append", default=[], help="expected output file path (repeatable)")
    cmd_task_create.add_argument("--dry-run", action="store_true", help="validate and project without persisting")

    cmd_lessons = sub.add_parser("lessons", help="lesson suggestion and promotion tools")
    lessons_sub = cmd_lessons.add_subparsers(dest="lessons_command", required=True)
    lessons_sub.add_parser("suggest", help="generate lesson suggestions from recent failures")
    cmd_lessons_promote = lessons_sub.add_parser("promote", help="promote suggested lessons to active lessons")
    cmd_lessons_promote.add_argument("--all", action="store_true", help="promote all available suggestions")
    cmd_lessons_promote.add_argument("--limit", type=int, default=1, help="number of suggestions to promote when --all is not set")
    cmd_lessons_promote.add_argument("--dry-run", action="store_true", help="compute promotion without persisting")

    cmd_runtime = sub.add_parser("runtime", help="runtime profile helpers for canonical stack commands")
    runtime_sub = cmd_runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_sub.add_parser("profiles", help="list available runtime profiles")
    cmd_runtime_command = runtime_sub.add_parser("command", help="get canonical command for stack/action")
    cmd_runtime_command.add_argument("--stack", required=True, help="runtime stack profile")
    cmd_runtime_command.add_argument("--action", required=True, choices=["start", "build", "test"], help="runtime action")

    cmd_github = sub.add_parser("github", help="GitHub integration helpers")
    github_sub = cmd_github.add_subparsers(dest="github_command", required=True)
    github_sub.add_parser("check", help="check git/github prerequisites")
    cmd_github_publish = github_sub.add_parser("publish", help="assisted publish workflow")
    cmd_github_publish.add_argument("--repo", default=None, help="GitHub repo slug (owner/name) or full remote URL")
    cmd_github_publish.add_argument("--remote", default="origin", help="git remote name")
    cmd_github_publish.add_argument("--branch", default=None, help="branch to push (default: current branch)")
    cmd_github_publish.add_argument("--visibility", default="private", choices=["private", "public"], help="repo visibility when creating via gh")
    cmd_github_publish.add_argument("--no-gh", action="store_true", help="force fallback flow without gh CLI")
    cmd_github_publish.add_argument("--dry-run", action="store_true", help="show planned commands without executing")

    sub.add_parser("project", help="reproject read-models from event store")
    sub.add_parser("verify", help="verify projection consistency")

    cmd_replay = sub.add_parser("replay", help="rebuild state until event id/seq")
    cmd_replay.add_argument("--until", default=None, help="event_seq (number) or event_id")
    cmd_replay.add_argument("--no-write", action="store_true", help="compute replay without writing views")

    cmd_memory = sub.add_parser("memory", help="semantic memory tools")
    ms_sub = cmd_memory.add_subparsers(dest="subcommand", required=True)
    ms_search = ms_sub.add_parser("search", help="search event logs semanticly")
    ms_search.add_argument("query", help="search query")
    ms_search.add_argument("--top", type=int, default=5, help="top results")
    ms_sub.add_parser("sync", help="manually sync semantic index")

    cmd_mutate = sub.add_parser("mutate", help="apply an orchestrator.view.mutate event to the event store")
    cmd_mutate.add_argument("--target", required=True, help="mutation target area (e.g. contracts, schemas, projections)")
    cmd_mutate.add_argument("--change", required=True, help="change type (e.g. new, upgrade, deprecate)")
    cmd_mutate.add_argument("--summary", required=True, help="human-readable description of the mutation")
    cmd_mutate.add_argument("--files", nargs="*", help="list of files changed by this mutation")
    cmd_mutate.add_argument("--resolves", default=None, help="issue ID resolved by this mutation (e.g. ISS-0005)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    service = ESAAService(root=root)

    try:
        if args.command == "init":
            result = service.init(
                run_id=args.run_id,
                master_correlation_id=args.master_correlation_id,
                force=args.force,
            )
        elif args.command == "run":
            result = service.run(steps=args.steps, dry_run=args.dry_run)
        elif args.command == "submit":
            if args.file == "-":
                raw = sys.stdin.read()
            else:
                raw = Path(args.file).read_text(encoding="utf-8")
            agent_output = json.loads(raw)
            result = service.submit(agent_output, actor=args.actor, dry_run=args.dry_run)
        elif args.command == "process":
            result = service.process(dry_run=args.dry_run)
        elif args.command == "doctor":
            result = service.doctor()
        elif args.command == "task":
            if args.task_command == "create":
                result = service.create_task(
                    task_id=args.task_id,
                    task_kind=args.kind,
                    title=args.title,
                    description=args.description,
                    depends_on=list(args.depends_on or []),
                    targets=list(args.target or []),
                    output_files=list(args.output_file or []),
                    dry_run=args.dry_run,
                )
            else:
                raise ESAAError("UNKNOWN_COMMAND", f"unknown task subcommand: {args.task_command}")
        elif args.command == "lessons":
            if args.lessons_command == "suggest":
                result = service.lessons_suggest()
            elif args.lessons_command == "promote":
                result = service.lessons_promote(
                    all_suggestions=args.all,
                    limit=args.limit,
                    dry_run=args.dry_run,
                )
            else:
                raise ESAAError("UNKNOWN_COMMAND", f"unknown lessons subcommand: {args.lessons_command}")
        elif args.command == "runtime":
            if args.runtime_command == "profiles":
                result = service.runtime_profiles()
            elif args.runtime_command == "command":
                result = service.runtime_command(stack=args.stack, action=args.action)
            else:
                raise ESAAError("UNKNOWN_COMMAND", f"unknown runtime subcommand: {args.runtime_command}")
        elif args.command == "github":
            if args.github_command == "check":
                result = service.github_check()
            elif args.github_command == "publish":
                result = service.github_publish(
                    repo=args.repo,
                    remote=args.remote,
                    branch=args.branch,
                    visibility=args.visibility,
                    use_gh=not args.no_gh,
                    dry_run=args.dry_run,
                )
            else:
                raise ESAAError("UNKNOWN_COMMAND", f"unknown github subcommand: {args.github_command}")
        elif args.command == "project":
            result = service.project()
        elif args.command == "verify":
            result = service.verify()
        elif args.command == "replay":
            result = service.replay(until=args.until, write_views=not args.no_write)
        elif args.command == "memory":
            if args.subcommand == "search":
                result = service.memory_search(args.query, top_k=args.top)
            elif args.subcommand == "sync":
                result = service.project()
        elif args.command == "mutate":
            result = service.mutate(
                target=args.target,
                change=args.change,
                summary=args.summary,
                files=args.files,
                resolves=args.resolves,
            )
        else:
            raise ESAAError("UNKNOWN_COMMAND", f"unknown command: {args.command}")

        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict):
            verify_status = result.get("verify_status")
            if verify_status in {"mismatch", "corrupted"}:
                return 2
        return 0
    except ESAAError as exc:
        next_step = _next_step_for_error(exc.code)
        print(
            json.dumps(
                {"error_code": exc.code, "error_message": exc.message, "next_step": next_step},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


def _next_step_for_error(code: str) -> str:
    mapping = {
        "TASK_NOT_FOUND": "Run `esaa project` and check task_id before submitting.",
        "MISSING_CLAIM": "Submit `claim` first while task is in `todo` state.",
        "MISSING_COMPLETE": "Use action=complete when sending file_updates.",
        "LOCK_VIOLATION": "Submit from the same actor that claimed the task.",
        "PRIOR_STATUS_MISMATCH": "Refresh roadmap state and resend with the current task status.",
        "INIT_BLOCKED": "Use `esaa init --force` only if you intend to reset the current run.",
        "TASK_ALREADY_EXISTS": "Choose a new task_id or inspect existing tasks with `esaa project`.",
        "DEPENDENCY_NOT_FOUND": "Create missing dependency tasks first or fix `--depends-on` values.",
        "RUNTIME_STACK_NOT_FOUND": "Run `esaa runtime profiles` and choose a supported stack.",
        "RUNTIME_ACTION_NOT_FOUND": "Use one of the supported actions for that stack (start/build/test).",
        "GITHUB_NOT_REPO": "Run this command inside a git repository root.",
        "GITHUB_REMOTE_REQUIRED": "Provide --repo to configure a remote or add one manually with git remote add.",
        "GITHUB_COMMAND_FAILED": "Review command output, fix auth/remote issues, and retry with --dry-run first.",
    }
    return mapping.get(code, "Inspect the error_code/message and run `esaa project` for current state context.")


if __name__ == "__main__":
    raise SystemExit(main())
