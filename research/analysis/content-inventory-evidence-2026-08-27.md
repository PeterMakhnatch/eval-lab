# Content Inventory: Consumer & Lineage Evidence Matrix

**Audit Snapshot Date**: 2026-08-27  
**Target Commit / HEAD**: `dc58bbdc62eaf8033ef1979d36d21324d5576ac7`  
**Scope**: Exhaustive consumer mappings, dynamic import registry, CI wiring, mixed runtime root evidence, untracked research assets, and PR #232–#236 subpackage cutover proofs.  

---

## 1. Subpackage Relocation & Consumer Verification (PRs #232–#236)

During the PR #232–#236 consolidation waves, four major subpackages were formalized under `src/evallab/`. Below is the complete consumer verification proving that every relocated module is actively consumed, fully tested, and that no legacy shim files remain.

### 1.1 `evallab.schemas` (Consolidated in PR #232)
- **Path**: `src/evallab/schemas/__init__.py`
- **Direct Consumers**: `src/evallab/queue.py`, `src/evallab/results.py`, `src/evallab/storage/attach.py`, `src/evallab/evidence/atif.py`, `scripts/backfill_spec_purpose.py`, `tests/test_contracts.py`
- **Verification**: Pydantic models validate all incoming ATIF JSON, trial outcomes, and task manifests.

### 1.2 `evallab.evidence` (Consolidated in PR #234)
- **Modules**: `atif.py`, `facts.py`, `event_mart.py`
- **Direct Consumers**: `src/evallab/cli.py` (evidence subcommands), `src/evallab/queue.py`, `scripts/profile/harness.py`, `tests/test_event_mart.py`, `tests/test_evidence_store.py`
- **Verification**: Ingests, normalizes, and extracts semantic facts from raw trajectory json files.

### 1.3 `evallab.storage` (Consolidated in PR #235)
- **Modules**: `paths.py`, `attach.py`, `parquet_compaction.py`, `data_backfill.py`
- **Direct Consumers**: `src/evallab/cli.py`, `dashboard/app.py`, `dashboard/queries.py`, `tests/test_attach.py`, `tests/test_parquet_compaction.py`, `tests/test_data_backfill_command.py`
- **Verification**: Provides DuckDB views over Parquet lake partitions and local cache tables.

### 1.4 `evallab.interpretation` (Consolidated in PR #236)
- **Modules**: 19 trajectory interpretation and execution modules (`trajectory_runtime.py`, `trajectory_quality.py`, `trajectory_ir.py`, `evidence_pack.py`, `traj_card.py`, etc.)
- **Direct Consumers**: `src/evallab/cli.py`, `src/evallab/analysis_worker.py`, `dashboard/projection.py`, `tests/test_trajectory_*.py`
- **Verification**: Evaluates agent behavior, tool efficiency, and verdict adjudication.

---

## 2. Mixed-Authority Live Runtime Roots Evidence (`runs/`, `derived/`, `queue/`, `backups/`)

Audit of the primary live runtime roots revealed 4,411 untracked files spanning 50,222,610 bytes across four major roots. These roots have mixed authorities and must NOT be treated as homogeneous cleanup targets:

### 2.1 `runs/` (1,292 files, 8,994,069 B across 50 subroots)
- **`runs/.executor/` (20 files, 26,375 B)**: `active-runtime`, `operational-state`. Harbor execution worker process logs. Sole writer: `evallab.runner`.
- **`runs/scratch_and_tests/` (257 files, 392,049 B)**: `active-runtime`, `operational-state`. Ephemeral test workspaces (`_omp-audit-speed`, `_smoke`, `_premerge`). Rebuildable via test execution.
- **`runs/trial_jobs/` (1,012 files, 8,574,567 B)**: `active-runtime`, `raw-durable-evidence`. Unpromoted trial traces across 47 active jobs awaiting promotion or compaction. Sole deleter: `evallab gc --apply`.
- **`runs/specs/` (3 files, 1,078 B)**: `active-runtime`, `operational-state`. Active experiment specifications (`mender-*`, `reframe-*`).

### 2.2 `derived/` (3,034 files, 40,663,195 B across 8 subroots)
- **`derived/evidence-cas/` (424 files, 3,996,968 B)**: `active-runtime`, `raw-durable-evidence`. Content-Addressable Storage (CAS) for immutable evidence blobs. Composite digest: `sha256:0e55d54932052223edde0b2613a6fee17fcfedd7ee378afa038237afb3e001ab`. DO NOT DELETE.
- **`derived/parquet/` (2,126 files, 22,639,358 B)**: `generated`, `generated-rebuildable-projection`. Z3 DuckDB & LanceDB query lake cache. Rebuildable via `python -m evallab.storage.data_backfill --all`.
- **`derived/analyses/` (444 files, 13,133,869 B)**: `generated`, `generated-rebuildable-projection`. Rebuildable from CAS evidence via `evallab analyze batch`.
- **`derived/interpretation/` (35 files, 806,591 B)**: `generated`, `generated-rebuildable-projection`. Trajectory interpretation inspection JSONs.
- **`derived/` projected parquet indexes (3 files, 84,078 B)**: `generated`, `generated-rebuildable-projection`. `interpretation_artifacts.parquet`, `machine_judgments.parquet`, `acceptance_decisions.parquet`.
- **`derived/reports/` (2 files, 2,331 B)**: `generated`, `generated-rebuildable-projection`. Evaluation summary reports.

### 2.3 `queue/` (78 files, 207,015 B)
- **Role**: `active-runtime`, `operational-state`. Active task scheduler state (`done`, `failed`, `proposed`, `waiting`, `running`, `researchers`) and append-only `events.jsonl`.
- **Composite Digest**: `sha256:2444116a12a51c3c5a00f3520b7436c8bdd00c84f2c97f99a99f6c6cfc747019`.
- **Authority**: Managed by `evallab.queue`, `evallab.tick`, `evallab.executor`.

### 2.4 `backups/postgres/` (7 files, 358,431 B)
- **Role**: `active-runtime`, `operational-state`. Nightly PostgreSQL `pg_dump` snapshots (rolling 14-day window).
- **Composite Digest**: `sha256:25940c54d8330dd2957cd724b50f26a43b28380dd8e3979ba50a6d42b4c46370`.

---

## 3. Untracked Local Evidence Files

Three specific untracked files in the working root are cataloged with exact SHA-256 hashes and authority details:
1. **`excalidraw.log`**:
   - **Classification**: `generated`
   - **Lifecycle Role**: `cache`
   - **SHA-256**: `08ebb78843fcb2a4a8ec36ff066986a0f969d576a41f0341cacd52301e06aed0`
   - **Consumers**: No direct repository consumer.
   - **Role**: Safe local cache log from Excalidraw diagram export CLI.
2. **`research/experiments/manifests/cross-campaign-quality-summary.json`**:
   - **Classification**: `active-runtime`
   - **Lifecycle Role**: `raw-durable-evidence`
   - **SHA-256**: `689922d6ef7ed69655858e4c8fa4d92c49303dc524f4f5d657486d6c6ed025c0`
   - **Consumers**: Cross-campaign quality analysis workflows.
   - **Role**: Unique durable active research artifact.
3. **`research/inbox/parked-glossary-evidence-2026-08-27.md`**:
   - **Classification**: `historical`
   - **Lifecycle Role**: `raw-durable-evidence`
   - **SHA-256**: `95f1cb05d036436360f0d5b9e672ecce1522066557f4a02cdb3b62b2bdce161f`
   - **Consumers**: Replacement / consumer is future `docs/GLOSSARY.md` and cited overnight ledger.
   - **Role**: Unique durable active research / historical resume evidence.

---

## 4. Dynamic Import & String Dispatch Registry

Modules loaded dynamically or resolved via string dispatch that AST parsers may miss:

| Module / Target | Dynamic Loading Mechanism | Calling File & Line | Purpose |
|---|---|---|---|
| `dspy` | `importlib.import_module('dspy')` (optional) | `src/evallab/calibrate.py:42` | Verifier prompt optimization when DSPy installed |
| `litellm` | `importlib.import_module('litellm')` (optional) | `src/evallab/tracing.py:18` | OpenTelemetry span instrumentations |
| `openinference` | `importlib.import_module('openinference')` | `src/evallab/tracing.py:22` | Phoenix trace shipping collector |
| `harbor.models.trajectories` | `importlib.import_module` | `src/evallab/evidence/atif.py:35` | External ATIF trajectory translation |
| `evallab.harbor_codex:PinnedCodex` | CLI dynamic adapter resolution | `scripts/tau_knowledge/preflight.py:45` | Pinned Codex harness execution |
| `FreeFixtureBackend` | Dynamic mock backend injection | `tests/test_capability_workflow.py:28` | Docker-free capability testing |

---

## 5. Negative Evidence Verification for Deletion Candidate

### `docs/prompts/Untitled`
- **File Size**: 30 bytes
- **Exact Content**: `atus packet for an unclear run`
- **Negative Search Evidence**:
  1. **Python AST Imports**: 0 occurrences in `src/`, `tests/`, `dashboard/`, `scripts/`.
  2. **CLI / Entrypoint References**: 0 occurrences in `src/evallab/cli.py` and `pyproject.toml`.
  3. **Documentation Index**: 0 occurrences in `docs/INDEX.md` and `docs/prompts/README.md`.
  4. **CI / Configuration**: 0 occurrences in `.github/workflows/`, `Makefile`, `compose.yaml`.
  5. **Dynamic String References**: 0 matches across the entire git tree.
- **Classification**: `proven-unused` (Cataloged as Deletion Candidate, preserved under no-delete safety policy).

---

## 6. CI Workflow Entrypoints & Verification Suite

All key entrypoints verified in `.github/workflows/ci.yml`, `perf.yml`, and `premerge.sh`:
- `python -m evallab.docindex check`: Asserts documentation index integrity.
- `python -m evallab.repomap check`: Asserts AST repo-map sync.
- `python -m evallab.governance check`: Validates domain specification contracts.
- `evallab registry audit --json`: Verifies task registration manifest integrity.
- `python -m evallab.lessons`: Validates lessons synthesis from raw run traces.
- `pytest`: Executes unit, integration, and contract tests across `tests/` and `dashboard/tests/`.
- `evallab smoke --docker-free`: Validates core runtime loops.