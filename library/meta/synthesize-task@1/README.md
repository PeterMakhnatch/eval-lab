# Synthesize Task (Meta-Task @ 1)

A Terminal-Bench-format task whose subject is authoring another evaluation task.
Implements the Meta-Task pattern (arXiv:2607.27929) for the Eval Lab authoring plane.

## Overview

The generator agent runs inside an environment containing:
- `/app/spec.json`: Target task specification (category, scenario, difficulty, goals).
- `/app/skeleton/`: Structured skeleton directories and template files.
- `/app/exemplar/`: A reference exemplar task from the verified task library.
- `/app/templates/`: Authoring guidelines and anti-pattern checklists.

The agent produces a complete, self-contained Terminal-Bench task package in `/app/output/task/`.

## Quality Contract

The generated package must satisfy four automated completeness checks:
1. **Package Structure**: Valid `task.toml`, `instruction.md`, `environment/Dockerfile`, `solution/solve.sh`, `tests/Dockerfile`, and `tests/test.sh`.
2. **Oracle Execution**: The oracle solution runs cleanly and produces expected outputs.
3. **Task Tests Pass**: The task's verifier passes on the oracle solution output and fails on empty work.
4. **No Answer Leakage**: Golden solutions, test fixtures, and answer keys are strictly isolated from `instruction.md` and `environment/`.
