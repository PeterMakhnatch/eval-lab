# Mission board

The sole live mission board. Only the integrator edits this file. States: `ready` -> `active` -> `review` -> `merged`, with `blocked` possible anywhere.

## Now

### Active Protected Role Lanes (ADR-031)

Per ADR-031, long-lived role lanes isolate parallel workstreams. Current branch state, dirty handoffs, and working notes are recorded in `research/analysis/git-estate-handoffs-2026-08-27.md`:

| Lane | Branch / Worktree | Role Scope | Current State & Handoff Authority |
|---|---|---|---|
| Architect | `lane/architect` / `.worktrees/lane-architect` | Controller, ledger maintenance, review authority, stabilization | `research/analysis/git-estate-handoffs-2026-08-27.md` |
| Execution | `lane/execution` / `.worktrees/lane-execution` | Runner/queue immutable DTO contracts, execution watchdog | `research/analysis/git-estate-handoffs-2026-08-27.md` |
| Storage | `lane/storage` / `.worktrees/lane-storage` | CAS/database management, partition discovery, backfill surface | `research/analysis/git-estate-handoffs-2026-08-27.md` |

### Active Hygiene Sequence & Module Stability Contract

- `Current module locations are stable for now; new work uses the current authoritative paths.`
- `No further physical package migration is authorized without new explicit Peter approval. This does not freeze repository or feature work.`

## Missions

- **Feature Work Status**: Feature development, task ingestion, analysis queries, verifier calibration, and bug fixes remain completely unblocked across all active lanes.
- **Context-Supply Intake Queue**: `research/inbox/QUEUE.md` (authoritative intake queue for HARVEST / STANDARDS / VERIFIER loops).
- **Overnight Program & Stabilization Ledger**: `research/analysis/automated-trajectory-overnight-ledger.md` (authoritative tracking for ADR-001 through ADR-035, model budget directives, and Track A–E milestone settlements).
- **Estate & Lane Handoffs**: `research/analysis/git-estate-handoffs-2026-08-27.md` and `docs/git-estate-inventory.md` (authoritative records of branch estate, dirty working trees, and lane reconciliation status).

## Archive

Historical completed mission logs and narrative records:
- `agents/archive/2026-08-15-missions-m001-m004-a001.md` (Wave 1: M001–M004, A001)
- `agents/archive/2026-08-15-missions-m005-m008.md` (Wave 1: M005–M008)
- `agents/archive/2026-08-27-mission-board-pre-hygiene.md` (Pre-hygiene verbatim board snapshot)
