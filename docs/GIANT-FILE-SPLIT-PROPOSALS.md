---
status: historical
audience:
  - builder
  - analyst
  - runner
  - operator
---

# Giant-File Split Proposals (Design Only — No Implementation Authorized)

This document catalogs structural decomposition proposals for the repository's largest monoliths identified during the repository audits ([docs/content-inventory.md](content-inventory.md), [research/analysis/repo-stabilization-audit.md](research/analysis/repo-stabilization-audit.md)).

> **CRITICAL GOVERNANCE CONSTRAINT**: This document is **PROPOSAL-ONLY**. No implementation, file moves, class extractions, refactorings, or packaging changes are authorized by this document. Current module locations across `src/evallab/` and its subpackages are **STABLE**. All current module paths remain authoritative.

---

## 1. Governance & Non-Authorization Mandate

- **Design Record Only**: The analyses and decomposition plans below record architectural responsibilities, dependency risks, and required verification test suites for future staged engineering waves.
- **Strict Stability Stance**: No subpackages or modules may be created or split based on these proposals without an explicit, lane-authorized architectural ADR.
- **Zero In-Flight Disruption**: Existing imports across CLI, API, dashboard, and test harnesses MUST remain unchanged.

---

## 2. Giant-File Inventory & Responsibility Clusters

The ten largest source files account for over 24,250 lines of code. The internal responsibility clusters and proposed extraction boundaries are detailed below:

### 2.1 `src/evallab/cli.py` (4,035 lines)
- **Current Responsibilities**:
  1. Top-level argument parsing, hierarchy construction, and global flags (Lines 1–450).
  2. Subcommand dispatcher implementations across 40+ command domains (Lines 451–3600).
  3. Environment initialization, profile loading, and main execution entrypoints (`main`, `legacy_main`) (Lines 3601–4035).
- **Proposed Extraction Submodules**:
  - `evallab.cli.parser`: CLI argument schema and parser builders.
  - `evallab.cli.commands.*`: Domain-specific command handlers grouped by subsystem (`storage`, `registry`, `analysis`, `runner`).
  - `evallab.cli.entry`: Main entrypoint routines, legacy alias bindings, and error handlers.
- **Primary Risks & Blast Radius**:
  - Breaking 83-leaf golden CLI surface contract (`tests/test_cli_golden.py`).
  - Breaking `set_defaults` handler dispatch bindings across Python 3.12 and 3.14.
- **Required Verification Tests**:
  - `tests/test_cli_golden.py` (complete surface matching).
  - `tests/test_cli_audit.py`, `tests/test_cli.py`.

---

### 2.2 `src/evallab/task_workbench.py` (3,722 lines)
- **Current Responsibilities**:
  1. Task definition contracts, metadata schemas, and configuration models (Lines 1–400).
  2. Static sandbox, network policy, and file permission validators (Lines 401–1400).
  3. Task packet compiler, asset packager, and digest hashing engine (Lines 1401–2500).
  4. Task certification runner, multi-model execution probes, and result aggregators (Lines 2501–3722).
- **Proposed Extraction Submodules**:
  - `evallab.workbench.models`: Core task definition and validation contract models.
  - `evallab.workbench.validator`: Static sandbox and network policy audit engine.
  - `evallab.workbench.compiler`: Packet compiler and SHA-256 asset bundler.
  - `evallab.workbench.runner`: Task certification execution harness.
- **Primary Risks & Blast Radius**:
  - Breaking task certification pipelines and asset packaging for external Harbor benchmarks.
  - Modifying digest calculation logic for registered task bundles.
- **Required Verification Tests**:
  - `tests/test_task_workbench.py`.
  - `tests/test_registry.py`.

---

### 2.3 `src/evallab/authoring.py` (3,619 lines)
- **Current Responsibilities**:
  1. Proposal and candidate schema definitions (Lines 1–420).
  2. 4-Way Validation Battery (syntax, schema, isolation, execution) (Lines 421–1600).
  3. 19-Criterion Rubric Evaluator and task quality scoring models (Lines 1601–2700).
  4. Qualification ledger, audit trail, and promotion gating engine (Lines 2701–3619).
- **Proposed Extraction Submodules**:
  - `evallab.authoring.schemas`: Proposal, rubric, and candidate Pydantic models.
  - `evallab.authoring.battery`: 4-Way validation execution battery.
  - `evallab.authoring.rubric`: 19-criterion quality scoring evaluator.
  - `evallab.authoring.ledger`: Human qualification ledger and promotion state machine.
- **Primary Risks & Blast Radius**:
  - Altering 19-criterion rubric evaluation scoring thresholds.
  - Corrupting candidate qualification state in `library/tasks/_proposed/`.
- **Required Verification Tests**:
  - `tests/test_authoring.py`.
  - `tests/test_authoring_properties.py`.

---

### 2.4 `src/evallab/schemas.py` / `src/evallab/schemas/__init__.py` (2,242 lines)
- **Current Responsibilities**:
  1. Base `ContractModel`, cryptographic hashing, and serialization helpers (Lines 1–250).
  2. Trajectory, step, and ATIF v1.7 exchange format models (Lines 251–800).
  3. Experiment, run request, and LADDER grid configuration schemas (Lines 801–1400).
  4. Task packaging, security policy, and quota accounting models (Lines 1401–2242).
- **Proposed Extraction Submodules**:
  - `evallab.schemas.base`: `ContractModel` and digest mixins.
  - `evallab.schemas.atif`: ATIF schema models and trajectory step contracts.
  - `evallab.schemas.experiment`: LADDER grid and experiment execution models.
  - `evallab.schemas.policy`: Quota, authorization, and network policy models.
- **Primary Risks & Blast Radius**:
  - High-blast circular import risk across `evallab.profiles`, `evallab.quota`, and `evallab.storage`.
  - Breaking JSON schema serialization golden fixtures in `tests/fixtures/contracts/*.json`.
- **Required Verification Tests**:
  - `tests/test_schemas.py`.
  - `tests/test_contracts.py`.

---

### 2.5 `src/evallab/registry.py` (2,095 lines)
- **Current Responsibilities**:
  1. Task registry contracts, metadata records, and status enums (Lines 1–350).
  2. On-disk task discovery scanner and filesystem cataloger (Lines 351–1100).
  3. Audit verification engine and integrity validator (Lines 1101–1750).
  4. Human registration gate and task promotion ledger manager (Lines 1751–2095).
- **Proposed Extraction Submodules**:
  - `evallab.registry.models`: Task registration schemas and status definitions.
  - `evallab.registry.inventory`: Filesystem discovery scanner.
  - `evallab.registry.audit`: Task bundle validation and integrity checks.
  - `evallab.registry.promotion`: Registration transition engine and inventory writer.
- **Primary Risks & Blast Radius**:
  - Desynchronization of `research/registration/inventory.json`.
  - Permitting unpromoted or uncertified tasks to execute in production benchmark runs.
- **Required Verification Tests**:
  - `tests/test_registry.py`.
  - `tests/test_canary.py`.

---

### 2.6 `src/evallab/facts.py` (1,739 lines)
- **Current Responsibilities**:
  1. Trial fact extraction from raw trial logs and ATIF trajectories (Lines 1–500).
  2. Parquet schema definitions and batch table writers (Lines 501–1100).
  3. Catalog ingestion pipeline and database record synchronization (Lines 1101–1400).
  4. Stage-5 analyzer integration and derived metric extractors (Lines 1401–1739).
- **Proposed Extraction Submodules**:
  - `evallab.evidence.facts`: Core trial fact extraction routines.
  - `evallab.evidence.parquet`: Parquet table writers and schema definitions.
  - `evallab.evidence.catalog`: PostgreSQL catalog synchronization.
  - `evallab.evidence.analysis`: Derived metric aggregation hooks.
- **Primary Risks & Blast Radius**:
  - Schema drift between DuckDB parquet storage and PostgreSQL catalog views.
  - Disruption of `evallab.storage.data_backfill` pipeline.
- **Required Verification Tests**:
  - `tests/test_facts.py`, `tests/test_truth.py`.
  - `tests/test_state_events.py`.

---

### 2.7 `src/evallab/ladder.py` (1,515 lines)
- **Current Responsibilities**:
  1. LADDER evaluation grid schemas and factor validation rules (Lines 1–350).
  2. Cartesian factor-arm expansion and experiment permutation engine (Lines 351–950).
  3. Shard compiler, partition assigner, and task spec file writer (Lines 951–1300).
  4. Hypothesis formulation text renderer and matrix summarizer (Lines 1301–1515).
- **Proposed Extraction Submodules**:
  - `evallab.experiments.ladder.models`: Grid and factor configuration schemas.
  - `evallab.experiments.ladder.grid`: Cartesian expansion algorithm.
  - `evallab.experiments.ladder.shards`: Shard partitioner and spec writer.
  - `evallab.experiments.ladder.hypothesis`: Hypothesis template generator.
- **Primary Risks & Blast Radius**:
  - Non-deterministic shard generation altering randomized experiment arms.
  - Desynchronization of queue specs in `runs/specs/`.
- **Required Verification Tests**:
  - `tests/test_ladder.py`.
  - `tests/test_ladder_screen.py`.

---

### 2.8 `src/evallab/traj.py` (1,424 lines)
- **Current Responsibilities**:
  1. `StepOutline` and `TrajectoryOutline` model definitions (Lines 1–320).
  2. Mechanical loop detection, cascade heuristics, and cycle scorers (Lines 321–750).
  3. `TrajectoryFeatures` extractor and token accounting (Lines 751–1100).
  4. Trajectory Parquet export and compression formatters (Lines 1101–1424).
- **Proposed Extraction Submodules**:
  - `evallab.evidence.trajectory.models`: Outline and feature Pydantic models.
  - `evallab.evidence.trajectory.heuristics`: Loop and cascade detection algorithms.
  - `evallab.evidence.trajectory.features`: Trajectory metric extraction.
  - `evallab.evidence.trajectory.export`: Parquet table exporter.
- **Primary Risks & Blast Radius**:
  - Breaking trajectory outline screening used by fast-fail filters.
  - Altering feature column definitions in `traj_features.parquet`.
- **Required Verification Tests**:
  - `tests/test_traj.py`.
  - `tests/test_traj_baseline.py`.

---

### 2.9 `src/evallab/lessons.py` (1,116 lines)
- **Current Responsibilities**:
  1. DuckDB SQL aggregation queries over Z2/Z3/Z4 query lake (Lines 1–380).
  2. Wilson 95% Confidence Interval statistical gating logic (Lines 381–700).
  3. Markdown findings renderer and lesson synthesizer (`research/lessons.md`) (Lines 701–1050).
  4. Freshness checker, validation gates, and CLI entrypoint (Lines 1051–1116).
- **Proposed Extraction Submodules**:
  - `evallab.interpretation.lessons.views`: DuckDB analytical SQL view queries.
  - `evallab.interpretation.lessons.stats`: Statistical significance gating.
  - `evallab.interpretation.lessons.renderer`: Markdown lesson synthesizer.
  - `evallab.interpretation.lessons.entry`: CLI invocation and verification handler.
- **Primary Risks & Blast Radius**:
  - Rendering inaccurate statistical claims in `research/lessons.md`.
  - Breaking automated nightly digest and campaign report pipelines.
- **Required Verification Tests**:
  - `tests/test_lessons.py`.

---

### 2.10 `src/evallab/runner.py` (1,087 lines)
- **Current Responsibilities**:
  1. `RunRequest` schema validation and task sandbox staging (Lines 1–280).
  2. Process supervisor, subprocess runner, and container command builder (Lines 281–700).
  3. Docker network isolation staging and cleanup lifecycle (Lines 701–950).
  4. Process watchdog, timeout enforcers, and signal handlers (Lines 951–1087).
- **Proposed Extraction Submodules**:
  - `evallab.execution.runner.request`: Run request models and validation.
  - `evallab.execution.runner.process`: Process management and command construction.
  - `evallab.execution.runner.staging`: Workspace and network isolation staging.
  - `evallab.execution.runner.cleanup`: Teardown hooks and signal handling.
- **Primary Risks & Blast Radius**:
  - Container escape or network leak if isolation staging is compromised.
  - Subprocess hang or zombie worker processes during interrupted trial runs.
- **Required Verification Tests**:
  - `tests/test_runner.py`.
  - `tests/test_harbor_network.py`.

---

## 3. Extraction Summary Matrix

| Target Giant File | Current Lines | Primary Responsibility Count | Proposed Extraction Modules | Risk Level | Gating Test Suite |
|---|---|---|---|---|---|
| `cli.py` | 4,035 | 3 | 3 | High | `tests/test_cli_golden.py` (83 leaves) |
| `task_workbench.py` | 3,722 | 4 | 4 | Medium | `tests/test_task_workbench.py` |
| `authoring.py` | 3,619 | 4 | 4 | High | `tests/test_authoring.py` |
| `schemas.py` | 2,242 | 4 | 4 | High | `tests/test_schemas.py`, `tests/test_contracts.py` |
| `registry.py` | 2,095 | 4 | 4 | Medium | `tests/test_registry.py` |
| `facts.py` | 1,739 | 4 | 4 | High | `tests/test_facts.py`, `tests/test_truth.py` |
| `ladder.py` | 1,515 | 4 | 4 | Low | `tests/test_ladder.py` |
| `traj.py` | 1,424 | 4 | 4 | Medium | `tests/test_traj.py` |
| `lessons.py` | 1,116 | 4 | 4 | Low | `tests/test_lessons.py` |
| `runner.py` | 1,087 | 4 | 4 | High | `tests/test_runner.py`, `tests/test_harbor_network.py` |
