---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Content Inventory & Durable Asset Taxonomy

**Audit Snapshot Date**: 2026-08-27  
**Target Commit / HEAD**: `dc58bbdc62eaf8033ef1979d36d21324d5576ac7`  
**Tracked Files**: 5,011 files (30,040,539 bytes, ~28.65 MB)  
**Untracked Local Evidence Files**: 3 files (`excalidraw.log`, `research/experiments/manifests/cross-campaign-quality-summary.json`, `research/inbox/parked-glossary-evidence-2026-08-27.md`)  
**Aggregate Runtime Subroots**: 14 live primary entries (`runs/`, `derived/`, `queue/`, `backups/postgres`)  
**Total Inventory Rows**: 5,028 rows (5,011 tracked + 3 untracked + 14 aggregate subroots)  
**Machine-Readable Companion**: `research/analysis/content-inventory-2026-08-27.json`  
**Consumer & Lineage Evidence Companion**: `research/analysis/content-inventory-evidence-2026-08-27.md`  

---

## 1. Executive Summary & Classification Topography

This inventory establishes the exhaustive, machine-verifiable baseline of all tracked files, primary live runtime aggregate roots, untracked local research assets, generated build authorities, and historical evidence in `eval-lab` at commit `dc58bbdc62eaf8033ef1979d36d21324d5576ac7` (PRs #232–#237 rebased head).

### 1.1 Tracked Estate Classification Breakdown

| Classification | Tracked File Count | Lifecycle Roles Present | Description | Primary Roots |
|---|---|---|---|---|
| `active-runtime` | 4,165 | `operational-state` | Live production code, CLI handlers, SQL schemas/views, task definitions, living prompts, and benchmark datasets. | `src/`, `sql/`, `library/`, `dashboard/`, `docs/`, `scripts/`, `agents/` |
| `active-test` | 386 | `operational-state` | Pytest test modules, unit fixtures, and CI automation scripts. | `tests/`, `dashboard/tests/`, `scripts/agentabstain/`, `scripts/profile/` |
| `generated` | 17 | `generated-rebuildable-projection` | Deterministic build products created by canonical Python generator entrypoints and Git merge drivers. | `docs/INDEX.md`, `docs/repo-map.md`, `docs/STATUS.md`, `research/lessons.md`, contract schemas |
| `compatibility` | 154 | `operational-state` | Upstream benchmark adapters and vendored dataset compatibility layers. | `library/external/`, `src/evallab/upstream_adapter.py`, `scripts/backfill_spec_purpose.py` |
| `historical` | 288 | `raw-durable-evidence` | Immutable point-in-time run traces, completed milestone briefs, research analyses, and audit logs. | `research/evidence/runs/`, `research/analysis/`, `docs/archive/prompts/`, `docs/checkpoints/` |
| `proven-unused` | 1 | `operational-state` | Unreferenced stray artifacts verified with zero code, test, CLI, config, or dynamic consumers. | `docs/prompts/Untitled` (Preserved as Deletion candidate) |
| `unknown` | 0 | — | Unclassified content requiring manual operator adjudication (zero unclassified items). | None |

### 1.2 Untracked Local Files Classification Breakdown

| Classification | Untracked Count | Files | Primary Lifecycle Role |
|---|---|---|---|
| `generated` | 1 | `excalidraw.log` | `cache` |
| `active-runtime` | 1 | `research/experiments/manifests/cross-campaign-quality-summary.json` | `raw-durable-evidence` |
| `historical` | 1 | `research/inbox/parked-glossary-evidence-2026-08-27.md` | `raw-durable-evidence` |

### 1.3 Aggregate Runtime Subroots Classification Breakdown

| Classification | Subroot Count | Subroots Included | Primary Lifecycle Role |
|---|---|---|---|
| `active-runtime` | 7 | `runs/.executor/`, `runs/scratch_and_tests/`, `runs/trial_jobs/`, `runs/specs/`, `derived/evidence-cas/`, `queue/`, `backups/postgres/` | `operational-state`, `raw-durable-evidence` |
| `generated` | 7 | `derived/parquet/`, `derived/analyses/`, `derived/interpretation/`, `derived/interpretation_artifacts/`, `derived/machine_judgments/`, `derived/acceptance_decisions/`, `derived/reports/` | `generated-rebuildable-projection` |

---

## 2. Invariant Rules, Guardrails & Policy Contracts

### 2.1 Module Location Stability Statement
> **Authoritative Policy**: Current module locations across `src/evallab/` and its subpackages (`evallab.storage`, `evallab.evidence`, `evallab.interpretation`, `evallab.schemas`, `evallab.recovery`) are **STABLE**. No file moves, renames, backwards-compatibility shims, or package directory reorganizations may be introduced. All new work, tools, and imports MUST use the authoritative current module paths directly.

### 2.2 Reconciling Prior-Audit Stale Classifications (PRs #232–#236)
During the migration waves in PRs #232 through #236, several modules were relocated into focused subpackages:
- `schemas.py` $\rightarrow$ `evallab.schemas` (PR #232)
- `atif.py`, `facts.py`, `event_mart.py` $\rightarrow$ `evallab.evidence` (PR #234)
- `paths.py`, `attach.py`, `parquet_compaction.py`, `data_backfill.py` $\rightarrow$ `evallab.storage` (PR #235)
- 19 trajectory analysis & execution modules $\rightarrow$ `evallab.interpretation` (PR #236)

**Caveat**: `src/evallab/repomap.py` uses `src_dir.glob('*.py')` (shallow AST inspection) when generating `docs/repo-map.md`. As a result, subpackage modules do not appear in `docs/repo-map.md`. **This is an AST generator limitation, NOT an indicator of unused code.** Every subpackage module has verified live consumers in `src/evallab/cli.py`, `dashboard/`, and the test suites.

### 2.3 Mixed-Authority Runtime Roots & No Wholesale Cleanup Guardrail
The primary runtime directories `runs/`, `derived/`, `queue/`, and `backups/` are **mixed-authority** environments and MUST NOT be treated as homogeneous wholesale cleanup candidates:
1. **`derived/evidence-cas/` (`raw durable evidence`, `active-runtime`)**: Holds 424 immutable CAS payload blobs (3.99 MB) referenced by `TrajectoryIR`, `MachineJudgment`, and `AcceptanceDecision` pipelines. Deleting this root destroys unpromoted evaluation evidence.
2. **`runs/trial_jobs/` (`raw durable evidence`, `active-runtime`)**: Holds 1,012 unpromoted trial files across 47 active jobs (8.57 MB). Governed exclusively by `evallab gc --apply` with a 14d uncompressed $\rightarrow$ 60d compressed $\rightarrow$ tombstone lifecycle.
3. **`queue/` (`operational-state`, `active-runtime`)**: Holds 78 active queue scheduling files (207 KB) and the append-only `events.jsonl` ledger. Must not be cleared while background worker tasks or researcher loops are scheduled.
4. **`derived/parquet/` & Projected Subroots (`generated-rebuildable-projection`, `generated`)**: Holds 2,126 query lake files (22.64 MB) rebuildable via `evallab.storage.data_backfill`.

### 2.4 Zone 1 Raw Immutable Evidence Guardrails (Retention $\infty$)
- `research/evidence/runs/*` and `library/benchmarks/_trajectories/*` constitute Zone 1 ground-truth immutable run traces.
- **Guardrail**: These paths MUST NEVER be deleted, modified, or overwritten by automated janitorial tools. Retention policy is permanent ($\infty$).

### 2.5 Deletion Candidate Protocol (`docs/prompts/Untitled`)
- `docs/prompts/Untitled` is a 30-byte truncated scrap fragment (`atus packet for an unclear run`) with zero inbound references, zero AST imports, and exclusion from `docs/prompts/README.md`.
- **Protocol**: It is cataloged strictly as a **Deletion Candidate** (`proven-unused`). In accordance with no-delete safety constraints, it is preserved in this inventory pass.

---

## 3. Primary Live Runtime Aggregate Roots

The table below documents the live state of untracked primary execution roots (4,411 total runtime files across 50,222,610 bytes), detailing their exact file counts, sizes, lifecycle roles, authorities, and rebuild/prune policies:

| Root / Subroot Path | Classification | Lifecycle Role | File Count | Bytes | Composite Digest / Authority | Retention Policy | Rebuild / Prune Policy |
|---|---|---|---|---|---|---|---|
| `runs/.executor/` | `active-runtime` | `operational-state` | 20 | 26,375 B | Harbor execution worker process logs | ephemeral operational state / trailing log rotation | rebuildable-via-runner-executions / Pruned via log rotation or evallab gc |
| `runs/scratch_and_tests/` | `active-runtime` | `operational-state` | 257 | 392,049 B | Ephemeral test execution and harness CI runs (_omp-audit-speed, _smoke, _premerge) | ephemeral / purgeable after test runs | rebuildable-via-test-suite / Safe to purge scratch workspaces; rebuildable via tests |
| `runs/trial_jobs/` | `active-runtime` | `raw-durable-evidence` | 1012 | 8,574,567 B | Zone 1 unpromoted execution traces across 47 trial jobs awaiting promotion/GC | 14d uncompressed -> 60d compressed -> tombstone; permanent once promoted | ephemeral (re-execute trial) / Sole deleter evallab gc --apply; do not delete wholesale |
| `runs/specs/` | `active-runtime` | `operational-state` | 3 | 1,078 B | Standalone root spec definitions (mender-*, reframe-*) | active queue / runner specs | version-controlled / re-creatable / Active experiment specifications |
| `derived/evidence-cas/` | `active-runtime` | `raw-durable-evidence` | 424 | 3,996,968 B | sha256:0e55d54932052223edde0b2613a6fee17fcfedd7ee378afa038237afb3e001ab | durable while cited by active campaigns/manifests; tombstone on GC | immutable payload restore from CAS URIs / DO NOT DELETE WHOLESALE: contains unpromoted raw CAS citations |
| `derived/parquet/` | `generated` | `generated-rebuildable-projection` | 2126 | 22,639,358 B | Z3 DuckDB lake & LanceDB query cache across 120 jobs and daily compacts | 7-day granular retention -> compacted daily tables; fully rebuildable | python -m evallab.storage.data_backfill --all / Rebuildable projection cache; managed via evallab.storage.parquet_compaction |
| `derived/analyses/` | `generated` | `generated-rebuildable-projection` | 444 | 13,133,869 B | Generated trajectory analysis artifacts and markdown summaries | rebuildable projection / pruned with source trial GC | evallab analyze batch <inventory> / Rebuildable from CAS evidence and judge models |
| `derived/interpretation/` | `generated` | `generated-rebuildable-projection` | 35 | 806,591 B | Trajectory interpretation batch inspection outputs | rebuildable projection / pruned with source trial GC | evallab interpret batch / Rebuildable projection |
| `derived/interpretation_artifacts/` | `generated` | `generated-rebuildable-projection` | 1 | 45,397 B | Projected interpretation artifact index (interpretation_artifacts.parquet) | rebuildable projection cache | evallab interpretation index rebuild / Rebuildable projection |
| `derived/machine_judgments/` | `generated` | `generated-rebuildable-projection` | 1 | 17,178 B | Projected machine judgment index (machine_judgments.parquet) | rebuildable projection cache | evallab judgment index rebuild / Rebuildable projection |
| `derived/acceptance_decisions/` | `generated` | `generated-rebuildable-projection` | 1 | 21,503 B | Projected acceptance decisions index (acceptance_decisions.parquet) | rebuildable projection cache | evallab acceptance index rebuild / Rebuildable projection |
| `derived/reports/` | `generated` | `generated-rebuildable-projection` | 2 | 2,331 B | Markdown and JSON evaluation summary reports | rebuildable report summaries | evallab report generate / Rebuildable projection |
| `queue/` | `active-runtime` | `operational-state` | 78 | 207,015 B | sha256:2444116a12a51c3c5a00f3520b7436c8bdd00c84f2c97f99a99f6c6cfc747019 | ephemeral task lifecycle state | reset / re-enqueue specs / DO NOT DELETE: active task scheduler state and event ledger |
| `backups/postgres/` | `active-runtime` | `operational-state` | 7 | 358,431 B | sha256:25940c54d8330dd2957cd724b50f26a43b28380dd8e3979ba50a6d42b4c46370 | trailing 14-day rolling window | restore via pg_restore / Managed via nightly rotation; retain 14-day rolling window |

---

## 4. Untracked Primary Evidence & Cache Files

Specific untracked files present in the working root are cataloged below with exact cryptographic digests to protect active research from accidental deletion:

| Path | Classification | Lifecycle Role | SHA-256 Digest | Authority & Role | Rebuild / Deletion Policy |
|---|---|---|---|---|---|
| `excalidraw.log` | `generated` | `cache` | `08ebb78843fcb2a4a8ec36ff066986a0f969d576a41f0341cacd52301e06aed0` | Untracked local artifact; safe cache file in working directory; no direct repository consumer | rebuildable-via-diagram-export / Safe cache; non-blocking deletion or retention |
| `research/experiments/manifests/cross-campaign-quality-summary.json` | `active-runtime` | `raw-durable-evidence` | `689922d6ef7ed69655858e4c8fa4d92c49303dc524f4f5d657486d6c6ed025c0` | Untracked durable research asset in research/experiments/manifests/; unique active research evidence | non-rebuildable-primary-research / DO NOT DELETE: unique durable active research evidence |
| `research/inbox/parked-glossary-evidence-2026-08-27.md` | `historical` | `raw-durable-evidence` | `95f1cb05d036436360f0d5b9e672ecce1522066557f4a02cdb3b62b2bdce161f` | Untracked durable research asset in research/inbox/; unique historical resume evidence | non-rebuildable-primary-research / DO NOT DELETE: unique durable active research / historical resume evidence |

---

## 5. Deterministic Generated Authorities

The following committed artifacts are deterministic build products managed by canonical generator entrypoints and custom Git merge drivers (`scripts/git-merge-regen.sh`):

| Path | Generator Entrypoint | Consumer / Purpose | Verification Command |
|---|---|---|---|
| `docs/INDEX.md` | `python -m evallab.docindex generate` | Authoritative documentation index with sha256 digests | `python -m evallab.docindex check` |
| `docs/repo-map.md` | `python -m evallab.repomap generate` | Living repository symbol & top-level AST map | `python -m evallab.repomap check` |
| `docs/STATUS.md` | `evallab status --write` (`src/evallab/status_generator.py`) | Daily research status and queue metrics | `evallab status` |
| `research/lessons.md` | `python -m evallab.lessons` | Synthesized statistical lessons from canonical runs | `python -m evallab.lessons` |
| `research/experiments/STATUS.md` | `evallab status --write` | Campaign status projection | `evallab status` |
| `research/registration/inventory.json` | `evallab registry refresh` | Authoritative task registration index | `evallab registry audit --json` |
| `tests/fixtures/contracts/*.json` | Pydantic Schema Model Export (`evallab.schemas`) | Golden contract schema fixtures | `pytest tests/test_contracts.py` |
| `docs/diagrams/*.svg` | Mermaid CLI (`.mmd`) / Excalidraw Export | Visual vector diagrams | Inspect vector rendering |

---

## 6. Comprehensive Tracked Directory Breakdown

### 6.1 `.claude/` (3 files, 5,846 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `.claude/skills/lab-status/SKILL.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.claude/skills/mission-launch/SKILL.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.claude/skills/review/SKILL.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |

### 6.2 `.githooks/` (2 files, 4,248 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `.githooks/post-merge` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.githooks/post-rewrite` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |

### 6.3 `.github/` (7 files, 16,891 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `.github/pull_request_template.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.github/workflows/agentabstain-linux.yml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.github/workflows/ci.yml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.github/workflows/loca-lean.yml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.github/workflows/perf.yml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.github/workflows/tau-knowledge.yml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.github/workflows/typecheck.yml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |

### 6.4 `agents/` (145 files, 910,693 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `agents/CHECKS.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/OWNERS.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/ROLES.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/STRUCTURE.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/WORKFLOW.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/archive/2026-08-15-handoffs/INDEX.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/archive/2026-08-15-handoffs/a001-state-audit.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/archive/2026-08-15-handoffs/adapter.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/archive/2026-08-15-handoffs/analyst.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/archive/2026-08-15-handoffs/autopilot.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/archive/2026-08-15-handoffs/coord-gc.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/archive/2026-08-15-handoffs/curator.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/archive/2026-08-15-handoffs/dashboard.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/archive/2026-08-15-handoffs/data-strategy.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `agents/archive/2026-08-15-handoffs/evidence.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `... and 130 additional files under agents/` | `collection` | `active-runtime` | `operational-state` | `Platform` | `standard` | See JSON inventory for full line-by-line file entries |

### 6.5 `authoring/` (3 files, 22,459 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `authoring/templates/category.yaml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `authoring/templates/difficulty.yaml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `authoring/templates/scenario.yaml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |

### 6.6 `containers/` (3 files, 12,718 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `containers/state-journal/Dockerfile` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `containers/state-journal/producer.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `containers/state-journal/watch.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |

### 6.7 `dashboard/` (9 files, 55,202 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `dashboard/README.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `dashboard/__init__.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `dashboard/app.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `dashboard/explorer.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `dashboard/projection.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `dashboard/queries.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `dashboard/tests/__init__.py` | `test_source` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `dashboard/tests/fixtures/dashboard.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `dashboard/tests/test_queries.py` | `test_source` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |

### 6.8 `digests/` (5 files, 58,955 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `digests/2026-08-13.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `digests/2026-08-14.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `digests/2026-08-15.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `digests/2026-08-16.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `digests/DISCOVERIES.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |

### 6.9 `docs/` (101 files, 1,169,623 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `docs/INDEX.md` | `file` | `generated` | `generated-rebuildable-projection` | `evallab.docindex` | `standard` | `python -m evallab.docindex generate` |
| `docs/STATUS.md` | `file` | `generated` | `generated-rebuildable-projection` | `evallab.status_generator` | `standard` | `evallab status --write` |
| `docs/agent-analysis.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/agent-profiles.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/agent-workflow.html` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/analysis-loop.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/analysis-worker.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/architecture-review-2026-08-16.md` | `file` | `historical` | `raw-durable-evidence` | `Platform / Integrator` | `standard` | `version-controlled` |
| `docs/architecture.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/attach-surface.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/authoring.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/automated-trajectory-interpretation-architecture-v1.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/behavior-analysis.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/build-plan.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `docs/canaries.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `... and 86 additional files under docs/` | `collection` | `generated` | `generated-rebuildable-projection` | `evallab.docindex` | `standard` | See JSON inventory for full line-by-line file entries |

### 6.10 `grids/` (1 files, 345 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `grids/event-summary-elicitation.yaml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |

### 6.11 `library/` (3,686 files, 18,249,350 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `library/adapters/agentabstain/__init__.py` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/agentabstain/adapter.py` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/agentabstain/controls.py` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/agentabstain/evidence.json` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/agentabstain/materialize.py` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/agentabstain/runtime.py` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/agentabstain/source/canary.json` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/agentabstain/source/canary_state.json` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/agentabstain/source/pins.json` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/agentabstain/templates.py` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/exgentic/adapter-manifest.json` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/quixbugs/.python-version` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/quixbugs/README.md` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/quixbugs/adapter-review-report.md` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `library/adapters/quixbugs/adapter_metadata.json` | `file` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | `version-controlled` |
| `... and 3671 additional files under library/` | `collection` | `active-runtime` | `operational-state` | `Tasks lane` | `standard` | See JSON inventory for full line-by-line file entries |

### 6.12 `policy/` (2 files, 3,010 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `policy/canary-suite.yaml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `policy/standing-approvals.yaml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |

### 6.13 `research/` (571 files, 2,831,293 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `research/analysis/README.md` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/automated-trajectory-overnight-ledger.md` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/completed-trial-data-layer-backfill-contract.md` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/control-oracle-vs-nop.json` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/git-estate-handoffs-2026-08-27.md` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/git-estate-inventory-2026-08-27.json` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/incremental-package-migration-plan.md` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/pr-186-architect-integration-review.md` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/preserved-primary-evidence-AGENTS-2026-08-27.md` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/queries.sql` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/repo-stabilization-audit.md` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/stage5-prompt.md` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/stage5-rubric.json` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/stub-codex-html-js-filter-analysis.json` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `research/analysis/stub-oracle-analysis.json` | `research_archive` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | `version-controlled` |
| `... and 556 additional files under research/` | `collection` | `historical` | `raw-durable-evidence` | `Research / Analyst` | `standard` | See JSON inventory for full line-by-line file entries |

### 6.14 `root/` (10 files, 328,402 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `.env.example` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.gitattributes` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.gitignore` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `.python-version` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `AGENTS.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `Makefile` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `README.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `compose.yaml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `pyproject.toml` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `uv.lock` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |

### 6.15 `scripts/` (24 files, 123,776 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `scripts/agentabstain/assert_reward.py` | `file` | `active-test` | `operational-state` | `Platform / CI` | `standard` | `version-controlled` |
| `scripts/agentabstain/ci.py` | `file` | `active-test` | `operational-state` | `Platform / CI` | `standard` | `version-controlled` |
| `scripts/agentabstain/run_130_audit.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/auth-status.sh` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/auth-verify.sh` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/backfill_spec_purpose.py` | `file` | `compatibility` | `operational-state` | `Platform` | `standard` | `historical-migration-script` |
| `scripts/claude-token-setup.sh` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/fleet-status.sh` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/git-merge-regen.sh` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/harbor-auth-env.sh` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/premerge.sh` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/profile/.gitignore` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/profile/README.md` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/profile/__init__.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/profile/budgets.json` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/profile/check_budgets.py` | `file` | `active-test` | `operational-state` | `Platform / CI` | `standard` | `version-controlled` |
| `scripts/profile/gh_stub.sh` | `file` | `active-test` | `operational-state` | `Platform / CI` | `standard` | `version-controlled` |
| `scripts/profile/harness.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/promote_codex_bundle.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/setup-git.sh` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/tau_knowledge/controls.py` | `file` | `active-test` | `operational-state` | `Platform / CI` | `standard` | `version-controlled` |
| `scripts/tau_knowledge/materialize.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/tau_knowledge/preflight.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `scripts/with-claude-auth` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |

### 6.16 `sql/` (11 files, 91,825 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `sql/analyst.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |
| `sql/behavior.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |
| `sql/calibration.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |
| `sql/craft_views.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |
| `sql/evidence_queries.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |
| `sql/ingest_views.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |
| `sql/lessons.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |
| `sql/schema.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |
| `sql/traj_views.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |
| `sql/verdicts.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |
| `sql/views.sql` | `file` | `active-runtime` | `operational-state` | `Platform / Database` | `standard` | `version-controlled` |

### 6.17 `src/` (119 files, 3,543,763 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `src/evallab/__init__.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/agentabstain_gate.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/analysis_worker.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/analyst.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/antigravity.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/authoring.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/automation.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/backups.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/behavior.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/behavior_calibration.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/behavior_catalog.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/behavior_episodes.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/calibrate.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/canary.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `src/evallab/capability_contract.py` | `file` | `active-runtime` | `operational-state` | `Platform` | `standard` | `version-controlled` |
| `... and 104 additional files under src/` | `collection` | `active-runtime` | `operational-state` | `Platform` | `standard` | See JSON inventory for full line-by-line file entries |

### 6.18 `tests/` (309 files, 2,612,140 bytes)

| Path | Kind | Classification | Lifecycle Role | Owner / Generator | Retention Policy | Rebuild / Source |
|---|---|---|---|---|---|---|
| `tests/fixtures/analysis_worker/jobs/job-exc/config.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-exc/join-trial/agent/trajectory.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-exc/join-trial/config.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-exc/join-trial/lock.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-exc/join-trial/result.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-exc/join-trial/verifier/reward.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-exc/lab-metadata.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-exc/lock.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-exc/result.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-fail/config.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-fail/join-trial/agent/trajectory.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-fail/join-trial/config.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-fail/join-trial/lock.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-fail/join-trial/result.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `tests/fixtures/analysis_worker/jobs/job-fail/join-trial/verifier/reward.json` | `test_fixture` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | `version-controlled` |
| `... and 294 additional files under tests/` | `collection` | `active-test` | `operational-state` | `Test Engineering / QA` | `active-test-suite` | See JSON inventory for full line-by-line file entries |
