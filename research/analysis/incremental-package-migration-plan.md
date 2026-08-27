---
status: in-progress
owner: Architect
date: 2026-08-27
authority: Peter assignment (2026-08-27)
---

# Eval Lab Incremental Package Migration Plan

## Decision

The 2026-08-27 Architect assignment authorizes a behavior-preserving physical
move of the flat `src/evallab/` module tree into
`src/evallab/{schemas,evidence,execution,cli}/` packages. Explicit
`__init__.py` facades preserve public imports. The move is sequential:
schemas → evidence → execution → cli.

This authority supersedes the prior plan-only stop state for physical packaging
only. It does not authorize behavior changes, compatibility removal, generated
product edits outside their generators, policy relaxation, or deletion based
only on apparent lack of static imports. Dynamic loading, operator SQL, console
scripts, public imports, test-only research utilities, generated products, and
active worktrees remain consumers.

## Target boundaries

| Boundary | Responsibility | Integration boundary |
|---|---|---|
| `schemas` | core typed models, immutable DTOs, trial specs, validation schemas | first wave; preserve serialized forms and `evallab.schemas` imports; no runtime-engine or database imports |
| `evidence` | Trajectory IR, ATIF telemetry, CAS, Parquet/DuckDB partitioning, digest synthesis | second wave; preserve raw → IR → pack authority, digests, schemas, rows, SQL, and query results |
| `execution` | trial lifecycle, Harbor sandbox orchestration, worker queues, quota gates | third wave, after Agent Data's Package 2 milestone 2 merges; preserve queue atomicity, fail-closed sandboxing, signals, cleanup, and command/environment snapshots |
| `cli` | parser, command handlers, entrypoints, compatibility alias | final wave; `evallab.cli:main` and `evallab.cli:legacy_main` remain resolvable and the 83-leaf golden surface remains byte-identical |

## Changed-path overlap inventory — 2026-08-27

Source: registered Git worktrees, branch changes against `origin/main`, and
working-tree status. A dirty worktree proves local mutation, not current owner
activity. Divergent history alone is not treated as an inevitable conflict.

| Classification | Branch / worktree | Observed migration-boundary paths | Disposition |
|---|---|---|---|
| controller scaffold | `main` / repository root | untracked `src/evallab/{schemas,evidence,execution,cli}/` directories containing package contracts | preserve and copy the contracts into `lane/migration`; do not mutate the primary checkout |
| confirmed active writer | `lane/execution` / `.worktrees/lane-execution` | dirty `execution_contracts.py`, `harbor_watchdog.py`, `queue.py`, `queue_driver.py`, `runner.py` | Agent Data owns these paths until its milestone PR merges; the migration execution wave is blocked on that merge |
| dirty, activity unconfirmed | `feature/data-backfill-command` / `.worktrees/data-backfill-command` | `cli.py`, `data_backfill.py` | preserve local work; no migration dependency unless the owner elects to land it before the corresponding wave |
| dirty, activity unconfirmed | `feature/trajectory-interpretation-v1` / `.worktrees/trajectory-interpretation-v1` | `acceptance.py`, `cli.py`, `evidence_pack.py`, `schemas.py`, `trajectory_alignment.py`, `trajectory_hydration.py`, `trajectory_ir.py`, `docs/repo-map.md`, CLI golden | preserve local work; owner must merge before the affected wave or rebase onto the post-migration layout |
| dirty, activity unconfirmed | `feature/canary-runs-ir-v1` / `.worktrees/canary-runs-ir-v1` | `canary_pipeline.py`, `docs/repo-map.md` | preserve local work; owner must merge before the affected wave or rebase after migration |
| dirty, activity unconfirmed | `feature/benchmark-semantic-facts-v1` / `/private/tmp/eval-lab-wave-shared` | `cli.py`, `semantic_facts.py` | preserve local work; owner must merge before the affected wave or rebase after migration |
| recent clean historical lane | `lane/storage` / `.worktrees/lane-storage` | `attach.py`, `data_backfill.py`, `parquet_compaction.py`, `paths.py`, `docs/repo-map.md` | Package 1 milestone 2 is already merged at `933b8a3`; no current dirty overlap |

Thirty-four registered worktree branches have some committed divergence touching
the broad migration boundary. That count is an integration watchlist, not
evidence that thirty-four branches are active. Any branch resumed during the
freeze follows the rule below.

## WIP and freeze rule

The freeze starts with `lane/migration` at
`.worktrees/lane-migration` and ends only when its migration PR merges or the
Architect explicitly releases it.

1. Migration-owned paths are all flat `src/evallab/*.py` modules, all files
   under `src/evallab/{schemas,evidence,execution,cli}/`, and the integration
   surfaces `pyproject.toml`, `docs/repo-map.md`, and
   `tests/golden/cli_surface.json`.
2. No branch other than `lane/migration` starts a new edit, rename, or move in
   those paths during the freeze. Repo Custodian efficiency work records
   findings only across this boundary until the migration lands.
3. Agent Data's already-started Package 2 milestone 2 is the sole exception.
   Agent Data remains the only writer for its five observed dirty modules; the
   migration does not move or edit them until that PR merges, then rebases onto
   `origin/main` and moves the merged result.
4. Existing uncommitted work is preserved. If an already-dirty branch must land,
   it merges before the affected migration wave; otherwise it waits and rebases
   onto the post-migration layout. No unpublished file is copied between
   worktrees.
5. An urgent fix to a migration-owned path lands on `main` first through its own
   reviewed PR. `lane/migration` then rebases before continuing. No parallel
   compatibility shim or second import convention is introduced.
6. The no-change boundary remains binding: public import paths, serialized
   forms, CLI arguments/defaults/help/exits, the 83-leaf golden CLI surface,
   Hatch package discovery, both console-script entrypoints, policy approvals,
   and the `recovery/__init__.py` explicit-facade convention do not change.

## Superseded staged plan (historical record)

The M0–M5 plan below records the pre-assignment gates. Its behavior,
compatibility, deletion, and generated-product constraints remain binding; its
plan-only implementation stop state and previous target shape are superseded by
the current assignment above.

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

## Gate refresh at `442e602`

M0 remains complete. PRs #199–#212 resolved historical correctness and
documentation gates but did not freeze the active ownership surfaces:

- **M1 HOLD:** the concrete jobs-Parquet discovery defect is fixed; active
  Platform/compaction ownership still blocks path extraction.
- **M2 HOLD:** runner, queue, network, auth, lifecycle, and dynamic Harbor
  consumers remain under active or preserved worktrees.
- **M3 HOLD:** Agent Data intermediary v2 and Platform parity work own the
  evidence/runtime boundary. No shared-DTO move while those outputs can change.
- **M4 HOLD:** the CLI added analysis/data-quality surfaces through PR #208 and
  is not frozen.

The only retained future sequence is M1 storage discovery, then M2 pure
execution value contracts, then M3 evidence DTO cycle breaking. Each remains a
separate opt-in package with old public imports and serialized behavior
preserved. M4 and every M5-scale split remain deferred. Current worktree names
are deliberately not copied into this plan: they are volatile coordination
state and must be checked live before any assignment.
