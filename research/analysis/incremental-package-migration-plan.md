---
status: plan-only
owner: Architect
date: 2026-08-26
authority: TRAJECTORY-WORK-ORDERS-2026-08-26.md
---

# Eval Lab Incremental Package Migration Plan

## Decision

No mass rename, move, deletion, archive, or package split is authorized. This plan converts the reconciled repository audit at head `72daeea` and native consumer/root/compatibility audits into five independently reviewable, behavior-preserving packages. Implementation requires a separate Architect assignment after every dependency gate clears.

Dynamic loading, operator SQL, console scripts, public imports, test-only research utilities, generated products, and active worktrees count as consumers. Zero static imports, old age, a large file, or a similar name is never sufficient deletion evidence.

## Target boundaries

| Boundary | Responsibility | Authorities preserved | Plan |
|---|---|---|---|
| `execution` | requests, queue leases, provider/network staging, lifecycle, quota | runner/queue/network/automation/quota and dynamic Harbor adapters | facade-first pure-contract extraction only |
| `evidence` | raw/ATIF/state/facts/quality/IR/hydration/pack/CAS | Harbor/ATIF/CAS raw authority and Agent Data-owned trajectory files | break cycles after PR #199; no alternate producer |
| `interpretation` | recipes, judgment, acceptance, reports | Platform runtime/judgment/acceptance and Analyst recipes | auto-accept remains disabled |
| `storage` | PostgreSQL, DuckDB attach, Parquet discovery/compaction | database/attach/compaction and SQL operator surfaces | pure partition discovery first; no unverified SQL removal |
| `experiments` | specs, cohorts, ladders, matrices | spec/cohort/ladder/screen/manifests | desired boundary only this wave |
| `benchmarks` | adapters, materializers, controls | `library/benchmarks`, `library/adapters`, registry/workbench | no movement this wave |
| `synthetic` | transforms, certification, projections | synthetic modules and `library/synthetic` | no movement before selected-family interfaces freeze |
| `cli` | parser, handlers, entrypoint/compatibility | `cli.py`, primary command, permanent legacy CLI alias, golden surface | last code package |

## M0 — documentation truth and compatibility inventory

- **Owner:** Architect/documentation integrator.
- **Files:** `README.md`, `docs/agent-profiles.md`, `docs/engineering.md`, generated `docs/STATUS.md`, `docs/repo-map.md`, `docs/INDEX.md`; no source.
- **Change:** remove stale hard-coded test counts or replace with commands; correct invocation examples; document the legacy CLI alias as retained; regenerate status/map/index.
- **Gate:** reconciled audit merged.
- **Acceptance:** docindex, repomap, governance, status-generator focused test; generated files only via generators.
- **Non-goal:** entrypoint removal, CLI rename, code movement.

## M1 — storage partition discovery leaf

- **Owner:** Platform.
- **Files:** `src/evallab/paths.py`, `attach.py`, `parquet_compaction.py`, focused tests; `facts.py` untouched.
- **Change:** extract pure hot/cold/standalone Parquet path enumeration and table normalization into `paths.py`; preserve old call facades and all layouts/schemas.
- **Gate:** PR #199 merged; Platform audit done; compaction/hardening owners clear files.
- **Acceptance:** focused path/attach/compaction tests; real Z2+Z3+Z4 smoke; identical discovered sets; zero SQL removals.

## M2 — execution pure-contract facade

- **Owner:** Platform with Ops review.
- **Files:** `runner.py`, `queue.py`, `harbor_network.py`, `quota.py`, `automation.py`; proposed `execution/contracts.py`, `execution/__init__.py`; old modules remain facades.
- **Change:** move immutable value contracts and side-effect-free validation only. Keep supervisor, `O_EXCL` leasing, auth/env propagation, Docker staging, signals and cleanup in place.
- **Gate:** all runner/network/auth hardening branches resolved with owner confirmation; Ops work done.
- **Acceptance:** serialized request equality; queue atomicity; focused tests; unchanged command/environment snapshots and public imports.

## M3 — evidence layering and cycle break

- **Owner:** Agent Data contracts/IR; Platform integration; one writer.
- **Files:** `atif.py`, `facts.py`, `event_mart.py`, `schemas.py`; proposed `evidence_contracts.py`; focused tests. Data-owned `trajectory_ir.py`, `evidence_pack.py`, `trajectory_hydration.py`, `trajectory_alignment.py`, `canary_pipeline.py` frozen unless assigned.
- **Change:** extract existing shared DTOs; re-export old symbols; invert callbacks so parsing/fact extraction does not invoke projection. Preserve raw→IR→pack authority, digests and Parquet schemas.
- **Gate:** PR #199/follow-ups merged; Analyst/Platform audits complete; event/family/coverage contracts frozen.
- **Acceptance:** byte-identical five-TB3 ATIF/IR/pack identities; exact schemas/rows; focused tests; import-cycle removal with unchanged outputs.
- **Non-goal:** replace raw ATIF with IR, remove mechanical facts, rename trajectory modules, alter semantics.

## M4 — CLI domain-handler facade

- **Owner:** Platform/CLI maintainer.
- **Files:** `cli.py`; proposed `cli_commands/{analyze,db,run,synthetic,registry}.py`; `pyproject.toml`; CLI golden/registry/audit tests; generated map/index.
- **Change:** move handler bodies while `cli.py` retains parser, entrypoint and facades. No command/argument/default/help/exit/script change.
- **Gate:** every active CLI-adding track merged; surface frozen; AST/golden strategy reviewed; legacy CLI alias retained.
- **Acceptance:** byte-identical golden surface; focused tests on 3.12/3.14 CI; top-level command smoke; unchanged both entrypoints.

## M5 is intentionally deferred

A central `schemas.py` split, registry/workbench split, synthetic namespace move, ladder/screen cycle break, SQL archival, and compatibility removal are not authorized packages. They require new evidence after M0–M4 and explicit approval.

## Deletion and compatibility criteria

1. Deletion requires zero static, dynamic/config, CLI, operator and external consumers; merged replacement; completed deprecation; green focused/generated checks.
2. Archival requires historical immutable non-authority status, current index links, and explicit retention/regeneration policy.
3. Merge requires identical semantics, fields, lifecycle and consumers—not names. `BehaviorEpisode` and `BehaviorEpisodeRecord` stay distinct.
4. Compatibility removal requires every source/test/external caller migrated and separately approved. `credentials.py`, `CitationTarget`, `compile_context_pack`, legacy manifest keys, backup layouts and the legacy CLI alias remain.
5. Generated products are changed through authorities and generators only: repo map, doc index, status and lessons.
6. Active worktrees require owner confirmation. This plan schedules no prune or cleanup.

## Stop state

M0 completed via PR #203 (`5bd2ba3`). The focused jobs-Parquet discovery repair merged via PR #206 (`f36ec8a`) without starting M1 or changing schemas. M1–M4 remain plan-only and require separate assignments; no implementation package is authorized by this document.
