---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
created_at: 2026-08-26T04:30:00Z
updated_at: 2026-08-27T02:59:07Z
author: "Evaluation Architect, supporting repo-stabilization assignment"
purpose: "Reconciled mechanical consumer inventory, responsibility clusters, concept duplication audit, and plan-only stabilization packages for Eval Lab."
---

# Reconciled Repository Consumer Inventory & Stabilization Audit (Eval Lab)

**Baseline / Current delta:** PR #200 (`c08ba77`, reviewed head `7f840ac`) / `origin/main` `442e602`  
**Scope:** Baseline mechanical inventory of 105 `src/evallab` modules plus the §9 delta to 110 modules; `sql/`, `scripts/`, `docs/`, `research/`, `library/`, generated roots, dynamic consumers, and active worktrees  
**Audience:** Architect (`wK:p6`), Platform Builder (`wH:p1`), Analyst (`wK:p5`), Research - Capabilities Evals (`wH:p9`)

---

## 1. Executive Summary & Verified Closed-Loop System Tour

Eval Lab operates a closed-loop evaluation and capability measurement platform across ten deterministic execution phases. **Crucially, raw evidence never feeds directly into synthetic task generation.** All synthetic mutations are strictly downstream of verified intermediate representations, evidence packs, machine judgment, and human-approved analytical findings.

```text
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       VERIFIED CLOSED-LOOP SYSTEM TOUR                                  │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ [1. Task Packaging]                                                                                    │
│     `library/tasks/` & `library/benchmarks/` ➔ Validated via `task_workbench.py` & `registry.py`       │
│                                  │                                                                     │
│ [2. Bounded Execution]           ▼                                                                     │
│     `runner.py` executes Harbor sandbox under strict `harbor_network.py` & `quota.py` policy           │
│                                  │                                                                     │
│ [3. Raw Evidence Ingest]         ▼                                                                     │
│     `results.py` captures raw `result.json`, `trial.log`, `agent/trajectory.json` ➔ CAS immutable store │
│                                  │                                                                     │
│ [4. Quality Ledger Gate]         ▼                                                                     │
│     `trajectory_quality.py` audits raw ATIF & runner status ➔ `trajectory_quality_reports.parquet`     │
│                                  │                                                                     │
│ [5. IR & Evidence Packing]       ▼                                                                     │
│     `trajectory_ir.py` (IR) & `evidence_pack.py` (Pack) structure verified episodes ➔ Parquet          │
│                                  │                                                                     │
│ [6. Machine Judgment & Analysis] ▼                                                                     │
│     `analysis_worker.py` (fail-closed), `trajectory_judgment.py`, `analyst.py` ➔ `AcceptanceDecision`   │
│                                  │                                                                     │
│ [7. Campaign Report & Findings]  ▼                                                                     │
│     `cards.py` & `report.py` generate campaign reports ➔ `research/lessons.md` (statistically gated)   │
│                                  │                                                                     │
│ [8. Controlled Synthesis]        ▼  (ONLY from approved empirical failure findings; NO raw shortcuts) │
│     `synthetic_transform.py` & `synthetic_funcdag.py` generate failure-grounded perturbations         │
│                                  │                                                                     │
│ [9. 8-Point Certification Gate]  ▼                                                                     │
│     `synthetic_cert.py` (3x oracle, NOP, 3+ mutants) ➔ `SyntheticCertificate(status="experimental")`   │
│                                  │                                                                     │
│ [10. Analytical Queries & Status]▼                                                                     │
│     `attach.py` serves DuckDB analytical queries (Z2+Z3+Z4); `status_generator.py` reads `queue/` &    │
│     PostgreSQL catalog to project `docs/STATUS.md` deterministically                                   │
└────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Reconciled Module & Consumer Classification (`src/evallab`)

All **105 actual Python files** in `src/evallab/` are mapped by actual import callsites across `src/`, `tests/`, `dashboard/`, and `scripts/`.

### Classification Invariants & Dynamic Loading Rules
- **Dynamic Runtime Adapters**: Modules such as `harbor_codex.py`, `harbor_antigravity.py`, and `harbor_state_journal.py` are loaded dynamically by Harbor or container hooks via string lookup and CLI configuration. **They are active dynamic components and must NOT be classified as dead or test-only simply due to zero static Python import statements.**
- **Compatibility Facades**: Modules such as `recovery/__init__.py` and `credentials.py` are active compatibility facades re-exporting stable interfaces across version boundaries.

### Summary Distribution (105 Files Total)
- **Active Core Runtime Modules:** 100
- **Dynamic Harbor / Container Runtime Adapters:** 3 (`harbor_antigravity.py`, `harbor_codex.py`, `harbor_state_journal.py`)
- **Compatibility Facades:** 2 (`recovery/__init__.py`, `credentials.py`)
- **Test-Only Modules:** 0 (all modules have CLI, dynamic, or production ingestion bindings)
- **Dead / Unused Modules:** 0
- **Phantom / Removed Modules (Corrected):** Removed 24 phantom rows from earlier drafts. Restored `upstream_adapter.py` and `recovery/` subpackage (`bundle.py`, `certify.py`, `pilot.py`, `wrapper.py`).

### Complete Module Classification Table (105 Modules)

| Module Path | Lines | Classification | Primary Callers / Callsite Evidence | Subsystem Role |
| :--- | :---: | :--- | :--- | :--- |
| `__init__.py` | 3 | **active** | `CLI entrypoint / dynamic lookup` | Core runtime package root |
| `analysis_worker.py` | 1144 | **active** | `tests/test_trajectory_quality.py, tests/test_analysis_worker.py (+3 more)` | Guarded completion-to-analysis background worker (M006) |
| `analyst.py` | 1223 | **active** | `tests/test_modeladapter.py, tests/test_analyst.py (+1 more)` | Durable agent analysis runner with stored trajectories |
| `antigravity.py` | 341 | **active** | `tests/test_antigravity.py, src/evallab/harbor_antigravity.py` | Antigravity stream-json to ATIF converter |
| `atif.py` | 1016 | **active** | `tests/test_fixture_conformance.py, tests/test_cli_audit.py (+23 more)` | Canonical ATIF trajectory projection and export engine |
| `attach.py` | 415 | **active** | `tests/test_attach_properties.py, tests/test_traj.py (+11 more)` | Unified DuckDB attach surface (Z2+Z3+Z4) |
| `authoring.py` | 3619 | **active** | `tests/test_authoring.py, tests/test_authoring_properties.py (+1 more)` | Task authoring pipeline, mutation, and review scoring |
| `automation.py` | 1003 | **active** | `tests/test_analysis_worker.py, tests/test_status_generator.py (+7 more)` | Headless doctor and nightly automation cycle |
| `backups.py` | 173 | **active** | `tests/test_backups.py, src/evallab/cli.py` | Postgres backup and atomic manifest publishing utility |
| `behavior.py` | 736 | **active** | `tests/test_trajectory_context.py, tests/test_behavior.py (+6 more)` | Behavioral analysis over unified DuckDB attach surface |
| `behavior_calibration.py` | 274 | **active** | `tests/test_behavior_episode_acceptance.py, src/evallab/labels.py` | Human-grounded calibration for ATIF behavior episodes |
| `behavior_catalog.py` | 155 | **active** | `tests/test_behavior_episode_acceptance.py, src/evallab/behavior_calibration.py` | Reviewable catalog for frozen ATIF behavior dimensions |
| `behavior_episodes.py` | 847 | **active** | `tests/test_trajectory_context.py, tests/test_behavior_episode_acceptance.py (+1 more)` | Canonical ATIF behavior episodes and detectors |
| `calibrate.py` | 1719 | **active** | `tests/test_calibrate.py, tests/test_behavior_episode_acceptance.py (+2 more)` | Calibration evaluation engine and parameter optimization |
| `canary.py` | 168 | **active** | `tests/test_registry.py, tests/test_canary.py (+2 more)` | Paid agent authorization and canary execution check |
| `capability_contract.py` | 877 | **active** | `tests/test_capability_workflow.py, tests/test_capability_contract.py (+2 more)` | Evidence-bound capability admission contract model |
| `capability_workflow.py` | 420 | **active** | `tests/test_capability_workflow.py, src/evallab/cli.py` | Offline capability workflow integration |
| `cards.py` | 642 | **active** | `tests/test_cards.py, src/evallab/cli.py` | Eval-card generator with purpose-bound shape |
| `cli.py` | 4035 | **active** | `tests/test_curve.py, tests/test_registry.py (+29 more)` | Root CLI command dispatcher and entry point (40+ handlers) |
| `cohort.py` | 1316 | **active** | `tests/test_curve.py, tests/test_lessons.py (+17 more)` | Cohort manifest, definition, and association logic |
| `contextpack.py` | 1039 | **active** | `tests/test_lessons.py, tests/test_repomap.py (+7 more)` | Compiled context pack generator for agent context injection |
| `craft.py` | 1352 | **active** | `tests/test_craft.py, src/evallab/lessons.py (+3 more)` | Task-corpus analyzer over on-disk benchmarks |
| `credentials.py` | 170 | **compatibility** | `tests/test_queue_properties.py, tests/test_queue.py (+4 more)` | Legacy M003 compatibility shim wrapping `evallab.profiles` |
| `curve.py` | 589 | **active** | `tests/test_curve.py, src/evallab/cli.py (+1 more)` | Capability degradation and response curve generation |
| `database.py` | 534 | **active** | `tests/test_operator_surfaces.py, tests/test_analysis_worker.py (+24 more)` | PostgreSQL operational catalog and schema definition |
| `digest.py` | 858 | **active** | `tests/test_golden_rendering.py, tests/test_analysis_worker.py (+17 more)` | Daily digest compiler from events, catalog, and runs |
| `docindex.py` | 405 | **active** | `tests/test_docindex.py, src/evallab/tidy.py` | Front-matter driven documentation indexer (`docs/INDEX.md`) |
| `event_mart.py` | 379 | **active** | `tests/test_behavior_episode_acceptance.py, src/evallab/facts.py (+2 more)` | Deterministic agent event mart (`trajectory_events`, `agent_actions`) |
| `eventlog.py` | 50 | **active** | `tests/test_queue.py, src/evallab/queue.py (+2 more)` | Append-only execution event logging |
| `evidence_pack.py` | 507 | **active** | `tests/test_trajectory_runtime.py, tests/test_evidence_pack.py (+3 more)` | Hierarchical long-trajectory evidence packaging for agents |
| `evidence_store.py` | 177 | **active** | `tests/test_trajectory_hydration.py, tests/test_trajectory_runtime.py (+10 more)` | Durable storage and retrieval of evidence packs |
| `explorer.py` | 1455 | **active** | `tests/test_m035_ui.py, tests/test_explorer.py (+1 more)` | Trajectory and trial run explorer query interface |
| `facts.py` | 1739 | **active** | `tests/test_state_events.py, tests/test_truth.py (+36 more)` | Trial facts extraction, Parquet export, catalog ingestion |
| `fetch.py` | 1055 | **active** | `tests/test_fetch.py, src/evallab/cli.py` | Harbor dataset and remote asset acquisition tool |
| `gc.py` | 757 | **active** | `tests/test_pruning.py, tests/test_gc.py (+2 more)` | Ingested run garbage collection and pruning engine |
| `governance.py` | 245 | **active** | `tests/test_governance.py, src/evallab/cli.py` | Repository governance contracts and frozen root checks |
| `harbor_antigravity.py` | 161 | **active (dynamic)** | `tests/test_antigravity.py, dynamic Harbor adapter` | Harbor agent adapter for Google Antigravity CLI |
| `harbor_codex.py` | 28 | **active (dynamic)** | `dynamic Harbor adapter (PinnedCodex)` | Harbor agent adapter for OpenAI Codex subscription CLI |
| `harbor_network.py` | 279 | **active** | `tests/test_runner.py, tests/test_harbor_network.py (+1 more)` | Platform-aware Docker network policy adapter for macOS/Linux |
| `harbor_state_journal.py` | 293 | **active (dynamic)** | `tests/test_state_journal.py, dynamic container hook` | Filesystem state deltas and snapshot journaler |
| `ingest_verify.py` | 517 | **active** | `tests/test_ingest_verify.py, src/evallab/cli.py` | Parity verification between disk, catalog, and Parquet |
| `labels.py` | 857 | **active** | `tests/test_behavior_episode_acceptance.py, tests/test_labels.py (+3 more)` | Behavior and failure taxonomy labeling models |
| `ladder.py` | 1515 | **active** | `tests/test_ladder_screen.py, tests/test_ladder.py (+3 more)` | LADDER Cartesian evaluation grid generator |
| `lance.py` | 425 | **active** | `tests/test_lance.py, src/evallab/cli.py` | LanceDB vector table indexer for analyst conclusions |
| `lessons.py` | 1116 | **active** | `tests/test_lessons.py, src/evallab/cli.py` | Statistical lesson aggregation views and markdown findings |
| `lineage.py` | 722 | **active** | `tests/test_lineage.py, src/evallab/lessons.py (+2 more)` | Artifact lineage and dependency digest resolution |
| `modeladapter.py` | 772 | **active** | `tests/test_modeladapter.py, src/evallab/runner.py (+1 more)` | Provider and model invocation adapter bindings |
| `operational_restraint.py` | 240 | **active** | `tests/test_operational_restraint.py, src/evallab/cli.py` | Operational restraint and abstention evaluation |
| `parquet_compaction.py` | 739 | **active** | `tests/test_parquet_compaction.py, tests/test_compaction_properties.py (+1 more)` | Parquet partition compaction and cold storage rollups |
| `paths.py` | 207 | **active** | `tests/test_trajectory_quality.py, tests/test_lessons.py (+19 more)` | Environment path resolution and directory roots |
| `phoenix_annotations.py` | 463 | **active** | `tests/test_phoenix_annotations.py, tests/test_behavior_episode_acceptance.py (+1 more)` | OpenTelemetry span annotation exporter for Phoenix |
| `power.py` | 506 | **active** | `tests/test_power.py, src/evallab/cli.py` | Statistical power and sample size planning calculations |
| `preflight.py` | 1293 | **active** | `tests/test_preflight.py, src/evallab/cli.py` | Quota, queue purpose, and power safety preflight gate |
| `profiles.py` | 628 | **active** | `tests/test_trajectory_quality.py, tests/test_profiles.py (+14 more)` | Subscription agent profiles and credential probes |
| `provenance.py` | 258 | **active** | `tests/test_provenance.py, src/evallab/cli.py` | Authoring model provenance tracking |
| `queue.py` | 1374 | **active** | `tests/test_queue_properties.py, tests/test_queue.py (+14 more)` | File-based experiment queue with atomic `O_EXCL` leasing |
| `quota.py` | 1489 | **active** | `tests/test_quota.py, src/evallab/preflight.py (+6 more)` | Provider quota and rate limit accounting engine |
| `recovery/__init__.py` | 16 | **compatibility** | `tests/test_recovery.py, src/evallab/cli.py` | Facade re-exporting state bundle and recovery symbols |
| `recovery/bundle.py` | 196 | **active** | `tests/test_recovery.py, src/evallab/recovery/certify.py (+1 more)` | Inherited workspace state bundle manager |
| `recovery/certify.py` | 179 | **active** | `tests/test_recovery.py, src/evallab/recovery/pilot.py` | State bundle certification and invariant checker |
| `recovery/pilot.py` | 193 | **active** | `tests/test_recovery.py, src/evallab/cli.py` | Bounded recovery pilot experiment runner |
| `recovery/wrapper.py` | 199 | **active** | `tests/test_recovery.py, src/evallab/recovery/pilot.py` | Harbor task wrapper for inherited state execution |
| `registry.py` | 2095 | **active** | `tests/test_registry.py, src/evallab/cli.py` | Explicit task registry and human promotion gate |
| `repomap.py` | 321 | **active** | `tests/test_repomap.py, src/evallab/cli.py` | AST-derived repository map generator (`docs/repo-map.md`) |
| `report.py` | 511 | **active** | `tests/test_report.py, src/evallab/cli.py` | Trajectory family and eval-card markdown reporting |
| `researchers.py` | 363 | **active** | `tests/test_researchers.py, src/evallab/cli.py` | Bounded research worker loops and discovery passes |
| `results.py` | 258 | **active** | `tests/test_results.py, src/evallab/facts.py (+4 more)` | Raw Harbor result, job, and trial parser |
| `runner.py` | 1087 | **active** | `tests/test_runner.py, tests/test_harbor_network.py (+5 more)` | Harbor execution supervisor, container lifecycle, and staging |
| `schemas.py` | 2242 | **active** | `tests/test_schemas.py, tests/test_results.py (+38 more)` | Core Pydantic contracts and schema validation |
| `screen.py` | 741 | **active** | `tests/test_ladder_screen.py, src/evallab/ladder.py (+1 more)` | Multi-factor experiment screening and pruning |
| `semantic_facts.py` | 185 | **active** | `tests/test_attach.py, src/evallab/parquet_compaction.py (+3 more)` | Semantic action and capability fact models |
| `seqgen.py` | 647 | **active** | `tests/test_seqgen.py, src/evallab/cli.py` | Sequence-space task generation algorithm |
| `smoke.py` | 129 | **active** | `tests/test_smoke.py, src/evallab/cli.py` | End-to-end local Docker/Postgres integration test runner |
| `spine.py` | 536 | **active** | `tests/test_spine.py, src/evallab/cli.py` | Join spine integrity and table binding validation |
| `state_events.py` | 231 | **active** | `tests/test_state_events.py, src/evallab/facts.py (+1 more)` | Runner-level filesystem state modification events |
| `status.py` | 287 | **active** | `tests/test_operator_surfaces.py, tests/test_status.py (+2 more)` | Read-only operator status dashboard |
| `status_generator.py` | 639 | **active** | `tests/test_status_generator.py, src/evallab/cli.py` | `docs/STATUS.md` Markdown projection generator |
| `storm.py` | 165 | **active** | `tests/test_storm.py, src/evallab/status_generator.py` | Error storm alarm and rate anomaly detector |
| `synthetic_cert.py` | 557 | **active** | `tests/test_synthetic_cert.py, src/evallab/cli.py` | 8-Point Certification Gate for synthetic tasks |
| `synthetic_contracts.py` | 382 | **active** | `tests/test_synthetic_contracts.py, src/evallab/synthetic_transform.py (+4 more)` | Pydantic contracts for synthetic eval specs and certs |
| `synthetic_funcdag.py` | 1056 | **active** | `tests/test_synthetic_funcdag.py, src/evallab/cli.py` | Cleanroom Function-DAG synthetic task generator |
| `synthetic_projections.py` | 553 | **active** | `tests/test_synthetic_projections.py, src/evallab/cli.py` | DuckDB/Parquet projections for synthetic lineages |
| `synthetic_report.py` | 240 | **active** | `tests/test_synthetic_projections.py, src/evallab/cli.py` | Analytical capability report generator for synthetic evals |
| `synthetic_transform.py` | 1081 | **active** | `tests/test_synthetic_transform.py, src/evallab/cli.py` | Deterministic transformation engine (3 families) |
| `task_import.py` | 868 | **active** | `tests/test_task_import.py, src/evallab/cli.py` | Bulk task package importer and cataloger |
| `task_workbench.py` | 3722 | **active** | `tests/test_task_workbench.py, src/evallab/cli.py` | Task quality workbench, static audit, and certification runner |
| `tidy.py` | 763 | **active** | `tests/test_tidy.py, src/evallab/cli.py` | Workspace cleanup, stale branch sweeper, and orphan collector |
| `tracing.py` | 599 | **active** | `tests/test_tracing.py, src/evallab/cli.py` | ATIF trajectory to OpenTelemetry span converter for Phoenix |
| `traj.py` | 1424 | **active** | `tests/test_traj.py, src/evallab/facts.py (+4 more)` | Trajectory outline extraction, loop heuristics, Parquet exporter |
| `traj_baseline.py` | 664 | **active** | `tests/test_traj_baseline.py, src/evallab/trajectory_ir.py` | Mechanical baseline metrics and `v_trace_baseline` SQL view |
| `traj_card.py` | 526 | **active** | `tests/test_traj_card.py, src/evallab/cli.py` | Trajectory Interpretation Card markdown renderer |
| `trajectory_acceptance.py` | 524 | **active** | `tests/test_trajectory_acceptance.py, src/evallab/cli.py` | ATIF behavioral acceptance and regression gate |
| `trajectory_alignment.py` | 741 | **active** | `tests/test_trajectory_alignment.py, src/evallab/cli.py` | Trajectory step sequence alignment and edit-distance diffing |
| `trajectory_calibration.py` | 614 | **active** | `tests/test_trajectory_calibration.py, src/evallab/cli.py` | Trajectory-level judge and detector calibration |
| `trajectory_context.py` | 523 | **active** | `tests/test_trajectory_context.py, src/evallab/cli.py` | Context extraction and compression for trajectory analysis |
| `trajectory_hydration.py` | 423 | **active** | `tests/test_trajectory_hydration.py, src/evallab/traj_card.py` | Redacted CAS/raw-ATIF hydration API for cited evidence |
| `trajectory_ir.py` | 400 | **active** | `tests/test_trajectory_ir.py, src/evallab/trajectory_judgment.py` | Canonical Trajectory Intermediate Representation (IR) engine |
| `trajectory_judgment.py` | 425 | **active** | `tests/test_trajectory_judgment.py, src/evallab/analyst.py` | Model-as-a-judge trajectory evaluator |
| `trajectory_quality.py` | 618 | **active** | `tests/test_trajectory_quality.py, src/evallab/analysis_worker.py (+2 more)` | Pre-analysis quality ledger (`trajectory_quality_reports.parquet`) |
| `trajectory_runtime.py` | 509 | **active** | `tests/test_trajectory_runtime.py, src/evallab/cli.py` | Trajectory runtime supervisor and execution loop |
| `trajectory_semantic_producers.py`| 547 | **active** | `tests/test_trajectory_semantic_producers.py, src/evallab/cli.py` | Semantic producer adapters and evidence fact extractors |
| `trajectory_semantics.py` | 915 | **active** | `tests/test_trajectory_semantics.py, src/evallab/cli.py` | Trajectory semantic facts and episode derivation |
| `trajectory_sequence.py` | 450 | **active** | `tests/test_trajectory_sequence.py, src/evallab/cli.py` | Sequence alignment algorithms and Levenshtein metrics |
| `upstream_adapter.py` | 147 | **active** | `tests/test_upstream_adapter.py, src/evallab/cli.py` | Abstract upstream benchmark adapter interface |
| `verdicts.py` | 527 | **active** | `tests/test_verdicts.py, src/evallab/digest.py (+2 more)` | Append-only human verdict and discovery logging |

---

## 3. Non-`src/evallab` Ecosystem Audit & Governance

### 1. SQL Layer (`sql/`) — 11 Files, 33 Tables/Views
- **Public & Operator Analytical Surfaces:** Views in `sql/schema.sql`, `sql/traj_views.sql` (`v_trace_baseline`, `traj_features`), `sql/behavior.sql`, `sql/lessons.sql`, `sql/evidence_queries.sql`, `sql/analyst.sql`, `sql/calibration.sql`, and `sql/craft_views.sql`.
- **Policy Invariant:** Views without direct static Python callers must **NOT** be pruned or archived without query-log evidence; they serve as operator query surfaces via psql, Streamlit dashboard, and DuckDB CLI.

### 2. Scripts Layer (`scripts/`) — 19 Scripts
- **Active Automation:** `scripts/premerge.sh` (local CI gate), `scripts/promote_codex_bundle.py` (CAS evidence promotion), `scripts/setup-git.sh` & `scripts/git-merge-regen.sh` (git merge driver), and Keychain OAuth helpers (`with-claude-auth`, `harbor-auth-env.sh`, `claude-token-setup.sh`, `auth-status.sh`, `auth-verify.sh`).
- **Historical Migration Scripts:** `scripts/backfill_spec_purpose.py` (one-time migration for WS-E; completed).

### 3. Documentation Layer (`docs/`) — 100 Files
- **Living Document Index:** 57 living documents and 7 historical snapshots cataloged in `docs/INDEX.md`.
- **Compatibility Policy on Legacy CLI Alias:** Retained as a permanent compatibility entry point. No removal without verified zero-external-caller proof.
- **Unindexed Static Assets:** `docs/prompts/Untitled` (empty orphan draft), static HTML dashboards (`system-cartography.html`, `repository-state.html`, `repo_overview.html`, `eval-rd-roadmap.html`, `agent-workflow.html`).

### 4. Research Layer (`research/`) — 575 Files
- **Core Governance Files:** `research/problems/LEDGER.md` (live research ledger), `research/inbox/QUEUE.md` (intake queue), `research/experiments/PROGRAM.json`, `research/synthetic/LEGAL_AND_METHODOLOGY_AUDIT.md`.
- **Active Artifacts:** 8 eval cards in `research/cards/`, candidate verification trees in `research/registration/candidates/`, and 12 observation directories in `research/observations/`.

### 5. Library Layer (`library/`) — 4,496 Files (17.7 MB)
- Pinned benchmarks (`tasks/`, `benchmarks/`), adapter packages (`adapters/`), experimental synthetic suites (`synthetic/`, `tasks/experimental/`), and 18 candidate task directories (`curated/`).

### 6. Storage Estate & Worktree Governance
- `derived/`: 25.11 MB, 2,324 files (dominated by `derived/parquet/` with 21.59 MB across date partitions `dt=2026-08-23..25`, `evidence-cas/` 2.12 MB, `analyses/` 1.36 MB).
- `runs/`: 8.40 MB, 1,164 files across 6 trial runs.
- `.worktrees/`: 25 worktree directories on disk (~260 MB total).
- **Policy Rule on Worktree Pruning:**
  - **Active Working Directories:** Directories in `.worktrees/` represent live feature work and must never be deleted without owner sign-off.
  - **Stale Git Worktree Metadata:** Stale entries in `.git/worktrees/` from past deleted checkouts can be cleaned via safe metadata pruning (`git worktree prune`) without affecting any disk directories.

---

## 4. Giant-File Responsibility Clusters & Extraction Targets

The 10 largest files account for **24,250+ lines**. The responsibility clusters and clean extraction targets are:

| File | Lines | Internal Responsibility Clusters | Proposed Extraction Submodules |
| :--- | :---: | :--- | :--- |
| **`cli.py`** | 4,035 | 1. Argument Parser & Hierarchy (L1-450)<br>2. Domain Subcommand Handlers (L451-3600)<br>3. Environment & Execution Entry (L3601+) | `evallab.cli.parser`<br>`evallab.cli.commands.*`<br>`evallab.cli.entry` |
| **`task_workbench.py`** | 3,722 | 1. Task Definition Contracts (L1-400)<br>2. Static Sandbox/Network Validator (L401-1400)<br>3. Packet Compiler & Hasher (L1401-2500)<br>4. Certification Runner (L2501+) | `evallab.workbench.models`<br>`evallab.workbench.validator`<br>`evallab.workbench.compiler`<br>`evallab.workbench.runner` |
| **`authoring.py`** | 3,619 | 1. Proposal & Candidate Schemas (L1-420)<br>2. 4-Way Validation Battery (L421-1600)<br>3. 19-Criterion Rubric Evaluator (L1601-2700)<br>4. Qualification Ledger & Promotion Gate (L2701+) | `evallab.authoring.schemas`<br>`evallab.authoring.battery`<br>`evallab.authoring.rubric`<br>`evallab.authoring.ledger` |
| **`schemas.py`** | 2,242 | 1. Base `ContractModel` & Digest Hashes (L1-250)<br>2. Trajectory & ATIF Models (L251-800)<br>3. Experiment & LADDER Models (L801-1400)<br>4. Task, Policy & Quota Models (L1401+) | `evallab.core.contracts.base`<br>`evallab.core.contracts.atif`<br>`evallab.core.contracts.experiment`<br>`evallab.core.contracts.policy` |
| **`registry.py`** | 2,095 | 1. Registry Models & Enums (L1-350)<br>2. Inventory Discovery & Scanner (L351-1100)<br>3. Audit Verification Engine (L1101-1750)<br>4. Human Promotion & Registration Guard (L1751+) | `evallab.registry.models`<br>`evallab.registry.inventory`<br>`evallab.registry.audit`<br>`evallab.registry.promotion` |
| **`facts.py`** | 1,739 | 1. Trial Fact Extractors (L1-500)<br>2. Parquet Writers & Schemas (L501-1100)<br>3. Catalog Ingestion Engine (L1101-1400)<br>4. Stage-5 Analyzer Integration (L1401+) | `evallab.evidence.facts`<br>`evallab.evidence.parquet`<br>`evallab.evidence.catalog`<br>`evallab.evidence.analysis` |
| **`ladder.py`** | 1,515 | 1. Grid Schemas & Validations (L1-350)<br>2. Cartesian Factor-Arm Expansion (L351-950)<br>3. Shard Compiler & Spec Writer (L951-1300)<br>4. Hypothesis Text Renderer (L1301+) | `evallab.experiments.ladder.models`<br>`evallab.experiments.ladder.grid`<br>`evallab.experiments.ladder.shards`<br>`evallab.experiments.ladder.hypothesis` |
| **`traj.py`** | 1,424 | 1. StepOutline & TrajectoryOutline Models (L1-320)<br>2. Mechanical Loop & Cascade Heuristics (L321-750)<br>3. TrajectoryFeatures Extractor (L751-1100)<br>4. Parquet Table Exporter (L1101+) | `evallab.evidence.trajectory.models`<br>`evallab.evidence.trajectory.heuristics`<br>`evallab.evidence.trajectory.features`<br>`evallab.evidence.trajectory.export` |
| **`lessons.py`** | 1,116 | 1. DuckDB SQL Aggregation Views (L1-380)<br>2. Wilson 95% CI Statistical Gating (L381-700)<br>3. Markdown Findings Renderer (L701-1050)<br>4. Freshness Checker & CLI Entry (L1051+) | `evallab.interpretation.lessons.views`<br>`evallab.interpretation.lessons.stats`<br>`evallab.interpretation.lessons.renderer`<br>`evallab.interpretation.lessons.entry` |
| **`runner.py`** | 1,087 | 1. `RunRequest` & Validation (L1-280)<br>2. Process Supervisor & Command Builder (L281-700)<br>3. Docker Network Staging & Cleanup (L701-950)<br>4. Watchdog & Signal Handlers (L951+) | `evallab.execution.runner.request`<br>`evallab.execution.runner.process`<br>`evallab.execution.runner.staging`<br>`evallab.execution.runner.cleanup` |

---

## 5. Concept Model Distinctness & Overlap Clarifications

### 1. `BehaviorEpisode` vs `BehaviorEpisodeRecord` (Distinct, Not Merged)
* **`BehaviorEpisode` (`src/evallab/behavior_episodes.py`)**: In-memory Pydantic model used by real-time detector algorithms during ATIF trajectory parsing.
* **`BehaviorEpisodeRecord` (`src/evallab/synthetic_contracts.py`)**: Canonical Parquet storage contract model capturing persistent episode facts for analytical queries.
* **Decision**: They serve two distinct stages (detection vs analytical persistence); **do not merge or delete**.

### 2. Trajectory Representation Tiering
* **`TrajectoryOutline` (`traj.py`)**: Lightweight structural outline for rapid loop/step heuristic screening.
* **`ATIFTrajectory` (`schemas.py`)**: Full Pydantic representation of the standardized ATIF v1.7 exchange format.
* **`TrajectoryIR` (`trajectory_ir.py`)**: Canonical rich Intermediate Representation with indexed windows, tool call resolution, and state bindings.
* **Decision**: Establish `TrajectoryIR` as the standard input format for all machine judgment and interpretation engines.

---

## 6. Documentation Drift & Stale Claims Catalog

1. **Legacy CLI Alias Retained:** `README.md:126` noted deprecation through 2026-08-21. Retained as a permanent compatibility entry point.
2. **Stale Test Count Assertions:**
   - `docs/agent-profiles.md:68`: Claims `tests/test_profiles.py` has 22 tests (actual count: 35 tests).
   - `docs/engineering.md:156`: Cites historical 49-test suite (actual count: 124 test files, 1,759 test functions).
   - `docs/engineering.md:163`: References 2,043 tests across earlier branches.
3. **Command Invocation Drift:** Several docs cite commands as `evallab craft`, `evallab author`, `evallab context`, `evallab lance`, `evallab task-workbench`, and `evallab parquet_compaction`. Live code implements these via `python -m evallab.<module>`.
4. **Stale Research Status Date:** `docs/STATUS.md` is dated 2026-08-18 ("Recent: Yesterday 2026-08-17"); update to live daily projection.

---

## 7. Plan-Only Stabilization Packages (Design-Staged & Conflict-Gated)

> **CRITICAL GOVERNANCE INVARIANT:** All packages below remain **PLAN-ONLY**. PRs #197–#212 merged, but that cleared historical PR gates—not the current active-worktree and ownership gates recorded in §9. No code move, rename, extraction, or compatibility removal is authorized.

```text
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   PLAN-ONLY STABILIZATION PACKAGES                                     │
├──────────────────────────┬─────────────────────────────────────┬───────────────────────────────────────┤
│ Package ID & Target      │ Conflict & Track Dependencies       │ Behavior-Preserving Acceptance        │
├──────────────────────────┼─────────────────────────────────────┼───────────────────────────────────────┤
│ **Package 1 (Low-Blast   │ GATED BEHIND: In-flight PR #199     │ • DuckDB Z2+Z3+Z4 attach queries pass.│
│   Storage)**             │ (Agent Data Engineer Intermediary). │ • Partition discovery centralized     │
│ `role/stabilize-storage` │ *Risk:* Broken glob paths in Z3.    │   in `paths.py`.                      │
│                          │                                     │ • Zero unverified SQL view removals.  │
├──────────────────────────┼─────────────────────────────────────┼───────────────────────────────────────┤
│ **Package 2 (Low-Blast   │ GATED BEHIND: Runner / network      │ • Exact atomic `O_EXCL` leasing in    │
│   Execution)**           │ hardening branches.                 │   `queue.py` preserved.               │
│ `role/stabilize-execution`│ *Risk:* Breaking subprocess env/auth│ • 53 focused runner/network tests pass│
│                          │ propagation for Harbor lanes.       │ • Zero change in container lifecycle. │
├──────────────────────────┼─────────────────────────────────────┼───────────────────────────────────────┤
│ **Package 3 (Evidence    │ GATED BEHIND: PR #198 AgentAbstain  │ • `TrajectoryIR` unified as single IR.│
│   & Quality)**           │ admission and control runner.       │ • Exact Parquet schemas unchanged.    │
│ `role/stabilize-evidence`│ *Risk:* Breaking CAS hash resolution│ • 102 focused quality/IR tests pass.  │
├──────────────────────────┼─────────────────────────────────────┼───────────────────────────────────────┤
│ **Package 4 (CLI)**      │ GATED BEHIND: AST test modernization│ • `test_cli_registry.py` AST walker   │
│ `role/stabilize-cli`     │ and CLI subcommands freeze.         │   inspects package submodules.        │
│                          │ *Risk:* Breaking 82 `set_defaults`  │ • Golden CLI surface tests pass on    │
│                          │ contract in single `cli.py` file.   │   Python 3.12 and 3.14.               │
├──────────────────────────┼─────────────────────────────────────┼───────────────────────────────────────┤
│ **Package 5 (High-Blast  │ GATED BEHIND: All active tracks     │ • All Pydantic model imports re-      │
│   Contracts Split)**     │ (PR #197, #198, #199) merged.       │   exported via `schemas.py` facade.   │
│ `role/stabilize-contracts`│ *Risk:* Cyclic imports if profiles  │ • 0 typecheck diagnostics on `ty`.    │
│                          │ not decoupled before schema split.  │ • 100% serialization test pass.       │
└──────────────────────────┴─────────────────────────────────────┴───────────────────────────────────────┘
```

---

## 8. Handoff to Architect (`wK:p6`)

- **Baseline worktree:** `/Users/petermakhnatch/Developer/eval-lab/.worktrees/repo-stabilization-audit` (clean; PR #200 merged)
- **Artifact:** `research/analysis/repo-stabilization-audit.md`
- **Reconciliation status:** The PR #200 baseline categorized 105 modules and removed 24 phantom rows. The current §9 delta records 110 modules, five new active modules, changed consumers, and current plan-only gates.

---

## 9. Delta refresh: PR #200 baseline to `442e602`

### Census and changed surfaces

The current `src/evallab` census is 110 Python modules: five additions and no
removals or renames since the 105-module PR #200 baseline.

| Added module | Current responsibility / consumer |
|---|---|
| `agentabstain_gate.py` | Single-delta audit used by AgentAbstain adapters, audit script, and gate tests |
| `trajectory_data_quality.py` | Campaign integrity report registered through the analysis CLI and exercised by quality tests |
| `trajectory_readiness.py` | Durable trajectory readiness and HOLD classification |
| `trajectory_recipe_run.py` | Report-pinned R1–R7 batch runner and findings output |
| `trajectory_recipes.py` | Deterministic Analyst recipe engine |

Materially changed existing surfaces include `attach.py`, `cli.py`,
`database.py`, `evidence_pack.py`, `traj_baseline.py`, `traj_card.py`,
`trajectory_hydration.py`, `trajectory_ir.py`, and `trajectory_runtime.py`.
These are active runtime, CLI, SQL/projection, Data, Platform, or Analyst
surfaces—not cleanup candidates. `docs/repo-map.md` is the generated current
symbol/digest authority; this section records responsibility and gate changes,
not a second generated map.

### Current package gates

| Package | Current verdict | Blocking ownership/evidence |
|---|---|---|
| M0 documentation truth | **COMPLETE** | PR #203 merged; generated outputs remain generator-owned |
| M1 storage discovery leaf | **HOLD** | PR #206 fixed the concrete jobs-Parquet defect without starting M1; Platform data-trust and compaction/attach worktrees still own the surfaces |
| M2 execution contracts | **HOLD** | runner/queue/network/auth worktrees and dynamic Harbor consumers are not settled |
| M3 evidence layering | **HOLD** | Agent Data intermediary v2 and Platform parity work actively own IR/pack/runtime boundaries |
| M4 CLI handlers | **HOLD** | CLI surface changed through PR #208 and remains active in Platform work; no freeze |
| M5 broad splits/removals | **DEFERRED** | no new consumer evidence authorizes a schema, registry, synthetic, SQL, or compatibility split |

The active-worktree list is volatile and belongs to `git worktree list`, not a
hard-coded cleanup manifest. At this refresh, current or preserved worktrees
touch `parquet_compaction.py`, `runner.py`, `queue.py`, `facts.py`,
`schemas.py`, `trajectory_ir.py`, `canary_pipeline.py`, and `cli.py`. They are
user work and must not be pruned, overwritten, or treated as dead-code evidence.

### Incremental recommendation

No stabilization implementation is dependency-ready during the resumed
trajectory loop. Retain only three future candidates:

1. M1 pure storage discovery after Platform and compaction ownership clears;
2. M2 immutable execution DTO/validation extraction after runner/queue owners
   clear, preserving `O_EXCL`, auth, environment, lifecycle, and signals;
3. M3 shared evidence DTO cycle break after Agent Data and Platform contracts
   freeze, with byte-identical ATIF/IR/pack/CAS identities.

M4 and every broader package remain deferred. This refresh authorizes factual
documentation correction only; it schedules no move, deletion, archive,
worktree cleanup, facade, or refactor.
