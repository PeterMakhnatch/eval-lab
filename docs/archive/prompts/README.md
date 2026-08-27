---
status: living
audience:
  - builder
  - operator
  - analyst
---

# Archived Implementation Briefs & Historical Prompts

This directory preserves completed, superseded, and retired implementation briefs, work orders, and dated mission prompts.

Original filenames and contents are preserved 1:1 via Git history (`git log --follow`) with historical frontmatter and living replacement links added.

## Superseded Brief & Mission Map

| Archived Path | Original Role / Scope | Superseded / Merged By | Living Replacement Contracts |
|---|---|---|---|
| [`01-atif-index.md`](01-atif-index.md) | ANALYST: Harbor ATIF indexing & Parquet projection | Implemented contract (2026-08-14) | `docs/data-architecture.md`, `docs/join-spine.md`, `docs/attach-surface.md` |
| [`02-cohort-compare.md`](02-cohort-compare.md) | ANALYST: Deterministic cohort comparison (`evallab compare`) | Implemented contract (2026-08-14) | `docs/analysis-loop.md` |
| [`03-analysis-pipeline.md`](03-analysis-pipeline.md) | ANALYST: End-to-end analysis pipeline (`evallab analyze`) | Implemented contract (2026-08-14) | `docs/analysis-loop.md`, `docs/analysis-worker.md` |
| [`04-proposal-gate.md`](04-proposal-gate.md) | BUILDER: Finding & proposal state machine | Merged state machine | `docs/analysis-loop.md`, `docs/operations.md` |
| [`05-queue-executor-policy.md`](05-queue-executor-policy.md) | BUILDER: Task queue and runner dispatch policy | Merged operational policy | `docs/operations.md` |
| [`06-headless-doctor-launchd-digest.md`](06-headless-doctor-launchd-digest.md) | BUILDER/OPERATOR: Headless doctor, launchd agent, digest | Merged diagnostic surface | `docs/surfaces.md`, `docs/storm-alarms.md`, `docs/operations.md` |
| [`07-canary-suite-drift.md`](07-canary-suite-drift.md) | BUILDER: Canary test suite & drift detection | Merged canary runner | `docs/canaries.md` |
| [`08-phoenix-trace-shipping.md`](08-phoenix-trace-shipping.md) | OBSERVER: Arize Phoenix OTEL span export | Merged trace collector | `docs/observability.md` |
| [`09-judge-calibration-dspy.md`](09-judge-calibration-dspy.md) | JUDGE: LLM-as-judge calibration & DSPy optimizer | Merged calibration pipeline | `docs/verifier-calibration.md` |
| [`12-bounded-researcher-loop.md`](12-bounded-researcher-loop.md) | OPERATOR/AUTOPILOT: Bounded research execution | Merged fleet automation | `docs/fleet-tracking.md`, `docs/surfaces.md` |
| [`overnight-missions.md`](overnight-missions.md) | Legacy overnight role work orders | Retired by M001 | `agents/missions/ACTIVE.md`, `agents/OWNERS.md`, `agents/WORKFLOW.md` |
| [`wave3-missions.md`](wave3-missions.md) | Legacy wave 3 role work orders | Retired by M001 | `agents/missions/ACTIVE.md`, `agents/OWNERS.md`, `agents/WORKFLOW.md` |
| [`system-cartographer-2026-08-15.md`](system-cartographer-2026-08-15.md) | System cartographer dispatch | Merged in PR #52 | `docs/checkpoints/2026-08-15-system-cartography.md` |
| [`functionalization-missions-2026-08-15.md`](functionalization-missions-2026-08-15.md) | Dated dispatch (M005, M006, M007) | Merged PRs #53–#55 | `docs/run-explorer.md`, `docs/analysis-worker.md`, `docs/task-workbench.md` |
| [`next-functionalization-missions-2026-08-15.md`](next-functionalization-missions-2026-08-15.md) | Dated dispatch (M006-R, M009, M010–M014) | Merged PRs #56, #102+ | `docs/checkpoints/2026-08-16-m009-integration-flight.md`, `agents/missions/ACTIVE.md` |

## Notes

- **Briefs 10 and 11**: These brief numbers were skipped in the historical sequence and never authored.
- **Authority**: For live mission states, active role lanes, and gating decisions, refer directly to `agents/missions/ACTIVE.md`.
