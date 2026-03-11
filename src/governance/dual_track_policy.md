# Dual-Track Promotion Policy

## Scope
This policy governs promotion from `lab` to `stable` for ESAA evolution.

## Baseline
- Stable baseline tag: `snapshot-2026-03-11-full-freeze`
- Any rollback reference must include this baseline tag or a newer approved stable freeze tag.

## Tracks
- `stable`: production-safe line, critical fixes only.
- `lab`: experimentation and recursive ESAA evolution.

## Promotion Rule
Promotion is allowed only through PR from lab to stable with external approval.

## Mandatory Gates
1. `verify_status` must be `ok`.
2. No open issues with severity `high` or `critical`.
3. External reviewer approval is required.
4. `docs/qa/GONOGO-<run_id>.md` must be present and approved.
5. PR description must include: run_id, master_correlation_id, projection hash, baseline tag.

## Block Conditions
- Missing Go/No-Go artifact.
- Verification mismatch or corrupted state.
- Contract/schema changes without explicit review note.

## Emergency Path
If emergency fix is needed on stable, open dedicated hotfix cycle with explicit issue linkage and post-fix retrospective in QA artifacts.
