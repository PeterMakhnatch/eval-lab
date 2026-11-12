# Evaluable task library

## Purpose
`library/` provides the version-pinned task supply evaluated by eval-lab. It contains
lab-authored tasks, curated third-party benchmarks, benchmark adapters, synthetic
task suites, and admission registry records.

## What lives here / entry points
- `library/tasks/`: Lab-authored benchmark tasks (e.g. `library/tasks/event-summary`).
- `library/curated/`: Verified third-party tasks with provenance cards.
- `library/benchmarks/`: Pinned frontier benchmark ingests (e.g. `library/benchmarks/action-memory-v1`).
- `library/adapters/`: Benchmark to Harbor task converters.
- `library/synthetic/`: Generated task sources and synthetic suites (e.g. `library/synthetic/seqgen-v0`).
- `library/registry/`: Task admission manifests and execution trust records.
- `library/external/`: External benchmark dependencies and upstream task references.
- `library/meta/`: Meta-task template packages for task synthesis.

## Invariants or rules
- Version-pinned immutability: `library/` content is version-pinned and immutable once registered; modifying a task requires a new version, never an edit in place (cited in `agents/STRUCTURE.md`).
- Hidden verifier inputs: Never place test suites or solutions in an evaluated agent's environment image (cited in `AGENTS.md`).
- Untouchable bytes: Registered task packages and promoted task versions are immutable source evidence (cited in `AGENTS.md`).
- Default local controls: Local controls (oracle and nop) must be validated before admitting new tasks (cited in `AGENTS.md`).

## Tests or checks
- `uv run pytest tests/test_registry.py`
- `uv run pytest tests/test_task_workbench.py`
- `uv run pytest tests/test_benchmark_program_contracts.py`

## What not to add here
- Do not store execution outputs, traces, or run directories here; use `runs/` or `research/evidence/runs/`.
- Do not place mutable scratch files or unversioned ad-hoc tasks here.
- Do not modify existing task definitions in place.

## Archived packages
| Group | Packages | Restore command |
|---|---:|---|
| GPQA-Diamond capability MCQ ingest | 198 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/gpqa-diamond` |
| HumanEvalFix unselected Python repair tasks | 152 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/humanevalfix` |
| AIME contest math capability ingest | 60 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/aime` |
| Terminal-Bench 2.0 sample ingest | 10 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/terminal-bench-sample` |
| BFCL-parity lock & ingest slice helper | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/bfcl-parity library/benchmarks/_verify_slice.sh` |
