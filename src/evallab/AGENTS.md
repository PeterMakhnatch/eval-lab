# Eval Lab Core Python Package (src/evallab/)

## Responsibilities
Authoritative domain implementations for execution runners, benchmark management,
synthetic task generation, difficulty ladders, and primary CLI command entrypoints.

## Layout truth

Packages exist and are frozen. Most of the package is still **flat top-level
modules** (`src/evallab/*.py`, ~90 files). Nested `AGENTS.md` files describe
package *intent*; they do not mean the implementation has fully moved.

| Path | What is actually there |
|---|---|
| `schemas/` | Giant `schemas/__init__.py`. Some contracts still top-level (`execution_contracts.py`, `capability_contract.py`). |
| `storage/` | Paths, DuckDB attach, Parquet compaction, backfill. |
| `evidence/` | ATIF, facts, event mart. CAS remains `evidence_store.py` at top-level. |
| `interpretation/` | Trajectory packs, feature registry, recipes, runtime. |
| `recovery/` | State recovery certification. |
| `cli/` | **Empty reserved directory.** Implementation is `cli.py`. |
| `execution/` | **Empty reserved directory.** Implementation is `runner.py`, `queue.py`, `quota.py`. |

Two TrajectoryIR modules exist: `trajectory_ir.py` (lossless ATIF) and
`interpretation/trajectory_ir.py` (runtime/citation IR). Do not merge or
delete either without a Peter-approved gate.

## Authoritative subsystem paths

- `src/evallab/schemas/`: Immutable contracts, DTOs, and Pydantic domain models.
- `src/evallab/storage/`: Path resolution, DuckDB unified attach, Parquet compaction, and CAS.
- `src/evallab/evidence/`: Canonical ATIF normalization, fact extraction, and event marts.
- `src/evallab/interpretation/`: Trajectory IR, evidence packing, machine judgments, and acceptance decisions.
- `src/evallab/recovery/`: State recovery certification and paired-trajectory pilots.
- Flat top-level modules (`src/evallab/*.py`):
  - Execution & Runner: `runner.py`, `queue.py`, `preflight.py`, `quota.py`, `execution_contracts.py`
  - Benchmarks & Registry: `registry.py`, `ladder.py`, `screen.py`, `task_workbench.py`, `authoring.py`, `task_import.py`
  - Synthetic Generators: `seqgen.py`, `synthetic_funcdag.py`, `synthetic_transform.py`, `synthetic_cert.py`, `synthetic_contracts.py`
  - Data & features: `traj.py`, `semantic_facts.py`, `trajectory_ir.py`, `feature_registry.py` (shim)
  - CLI & Coordination: `cli.py`, `status.py`, `repomap.py`, `docindex.py`, `verdicts.py`, `governance.py`

## Core Invariants
1. Stable Locations: Module paths are authoritative and locked. Do not perform ad-hoc renames or moves.
2. CLI Surface Stability: CLI subcommands map deterministically to domain modules.
3. Feature-Unblocked Status: Core data and interpretation layers are stabilized; new development targets campaigns, curves, and evaluations directly.

## Testing & Verification
- Run focused tests for touched modules: `pytest tests/test_<feature>.py`
