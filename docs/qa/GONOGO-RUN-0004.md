# Go/No-Go Checklist - RUN-0004 (Lessons Automation)

## Scope
Automatic suggestions and explicit promotion to active lessons.

## Checks
- [x] CLI exposes `lessons suggest` and `lessons promote`.
- [x] Promotion appends events and preserves `verify_status=ok`.
- [x] `.roadmap/lessons.json` updated with active lesson and valid indexes.
- [x] No non-JSON persistence introduced in this phase (as requested).

## Decision
GO
