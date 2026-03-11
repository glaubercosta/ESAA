# Go/No-Go Checklist - RUN-0003 (Operational Ergonomics)

## Scope
Doctor preflight, incremental task creation, and actionable error UX.

## Checks
- [x] `doctor` command available and executable.
- [x] `task create` command available and performs `task.create` with verify `ok`.
- [x] CLI errors include `next_step` for key error codes.
- [x] Core flow remains deterministic (`verify_status=ok` after submissions).

## Decision
GO
