# Platform implementation

## Purpose
`src/` contains the core Python source packages implementing the eval-lab platform.
It powers the execution runner, job queue, evidence ingestion pipeline, and
analytical state machines.

## What lives here / entry points
- `src/evallab/`: Primary Python package for the platform implementation.
- `src/evallab/cli.py`: Unified command-line interface entry point (`evallab`).
- `src/evallab/runner.py`: Harbor execution engine wrapper.
- `src/evallab/queue.py`: File-based queue and execution manager.
- `src/evallab/evidence/`: Evidence ingestion, ATIF parsing, and projection pipelines.

## Invariants or rules
- Python-first repository: All application code, adapters, and verifiers must be Python (cited in `AGENTS.md`).
- Frozen module layout: Module locations in `src/evallab/` are frozen; no reorganizations without explicit approval (cited in `docs/NOW.md`).
- Dual TrajectoryIR invariant: Do not merge or delete the two TrajectoryIR modules (`src/evallab/trajectory_ir.py` and `src/evallab/interpretation/trajectory_ir.py`) (cited in `docs/NOW.md`).
- Reserved packages: `src/evallab/cli/` and `src/evallab/execution/` are empty reserved directories; CLI logic lives in `src/evallab/cli.py` (cited in `docs/NOW.md`).

## Tests or checks
- `uv run evallab --help`
- `uv run pytest tests/test_runner.py`
- `uv run pytest tests/test_governance.py`

## What not to add here
- Do not add Java/JVM code, TypeScript, or non-Python runtimes without explicit approval (cited in `AGENTS.md`).
- Do not store raw run outputs, execution logs, or test data here; use `runs/` or `tests/`.
- Do not store task definitions or benchmarks here; use `library/`.
