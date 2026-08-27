# Eval Lab Core Python Package (src/evallab/)

## Responsibilities
Authoritative domain implementations for execution runners, benchmark management,
synthetic task generation, difficulty ladders, and primary CLI command entrypoints.

## Authoritative Subsystem Paths
- `src/evallab/schemas/`: Immutable contracts, DTOs, and Pydantic domain models.
- `src/evallab/storage/`: Path resolution, DuckDB unified attach, Parquet compaction, and CAS.
- `src/evallab/evidence/`: Canonical ATIF normalization, fact extraction, and event marts.
- `src/evallab/interpretation/`: Trajectory IR, evidence packing, machine judgments, and acceptance decisions.
- `src/evallab/recovery/`: State recovery certification and paired-trajectory pilots.
- Flat top-level modules (`src/evallab/*.py`):
  - Execution & Runner: `runner.py`, `queue.py`, `preflight.py`, `quota.py`, `execution_contracts.py`
  - Benchmarks & Registry: `registry.py`, `ladder.py`, `screen.py`, `task_workbench.py`, `authoring.py`, `task_import.py`
  - Synthetic Generators: `seqgen.py`, `synthetic_funcdag.py`, `synthetic_transform.py`, `synthetic_cert.py`, `synthetic_contracts.py`
  - CLI & Coordination: `cli.py`, `status.py`, `repomap.py`, `docindex.py`, `verdicts.py`, `governance.py`

## Core Invariants
1. Stable Locations: Module paths are authoritative and locked. Do not perform ad-hoc renames or moves.
2. CLI Surface Stability: CLI subcommands map deterministically to domain modules.
3. Feature-Unblocked Status: Core data and interpretation layers are stabilized; new development targets campaigns, curves, and evaluations directly.

## Testing & Verification
- Run focused tests for touched modules: `pytest tests/test_<feature>.py`
