# Evaluable task library

## Purpose
`library/` holds version-pinned Harbor tasks, task-variant lineage records, benchmark
subsets retained as run evidence, adapters, and admission registry records.

## What lives here / entry points
- `library/tasks/`: Lab-authored and registered tasks (e.g. `library/tasks/event-summary`).
- `library/registry/` and `library/frozen/`: Task admission records and frozen gym manifests.
- `library/task-variants/` and `library/task-findings/`: Variant lineage and task findings.
- `library/benchmarks/`: Retained benchmark subsets (`hello-world`, `humanevalfix`, `cheatbench`).
- `library/adapters/`: Retained benchmark adapters (`ceo_bench`, `quixbugs` manifest).
- `library/external/`: Frozen external screen tasks (`terminal-bench-v3.0.0`).

## Invariants or rules
- Version-pinned immutability: `library/` content is immutable once registered; modify via a new version (`agents/STRUCTURE.md`).
- Hidden verifier inputs: Never place test suites or solutions in an agent's environment image (`AGENTS.md`).
- Untouchable bytes: Registered task packages and promoted task versions are immutable source evidence (`AGENTS.md`).
- Default local controls: Validate local oracle and nop controls before admitting new tasks (`AGENTS.md`).

## Tests or checks
- `uv run evallab registry audit --json`
- `uv run pytest tests/test_registry.py tests/test_task_lint.py`

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
| QuixBugs generated Python tasks & template | 41 | `git checkout archive/pre-round3-2026-11-12 -- library/adapters/quixbugs` |
| Terminal-Bench 2.0 sample ingest | 10 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/terminal-bench-sample` |
| Restraint canary synthetic suite (`restraint-canary-v1`) | 8 | `git checkout archive/pre-round3-2026-11-12 -- library/synthetic/restraint-canary-v1` |
| Sequential generation synthetic suite (`seqgen-v0`) | 4 | `git checkout archive/pre-round3-2026-11-12 -- library/synthetic/seqgen-v0` |
| Task-synthesis meta packages (`synthesize-task@1`) | 3 | `git checkout archive/pre-round3-2026-11-12 -- library/meta/synthesize-task@1` |
| Operational-restraint S7 conflict pair (`s7-conflict-pair-v0`) | 2 | `git checkout archive/pre-round3-2026-11-12 -- library/synthetic/operational-restraint-s7-conflict-pair-v0` |
| Action-memory synthetic benchmark (`action-memory-v1`) | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/action-memory-v1` |
| MCP recovery synthetic benchmark (`mcp-recovery-v1`) | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/mcp-recovery-v1` |
| MCP FuncDAG synthetic benchmark (`mcp-funcdag-v1`) | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/mcp-funcdag-v1` |
| LOCA-lean context benchmark (`loca-lean-v1`) | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/loca-lean-v1` |
| Tau-knowledge cohort manifest & configs (`tau-knowledge`) | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/benchmarks/tau-knowledge` |
| Curated third-party pointer cards (`curated`) | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/curated` |
| DeepPlanning materializer & cohort (`deepplanning-v1`) | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/external/deepplanning-v1` |
| Reef publication gate adapter (`reef_gate`) | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/adapters/reef_gate` |
| AgentAbstain materializer & adapter (`agentabstain`) | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/adapters/agentabstain` |
| Stub adapters & benchmark helpers (`exgentic`, `recovery-bench`, `bfcl-parity`, `_verify_slice.sh`) | 0 | `git checkout archive/pre-round3-2026-11-12 -- library/adapters/exgentic library/adapters/recovery-bench library/benchmarks/bfcl-parity library/benchmarks/_verify_slice.sh` |
