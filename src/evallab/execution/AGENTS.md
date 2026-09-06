# Execution Subsystem (src/evallab/execution/)

## Purpose
Reserved namespace for execution runners, queueing, and quota controls.
Active implementation lives in top-level modules.

## What lives here / entry points
- Reserved empty package. Execution modules live in `src/evallab/`: `runner.py`,
  `queue.py`, `quota.py`, `execution_contracts.py`, `preflight.py`.

## Invariants or rules
1. Fail-Closed Sandboxing: All external tool invocations and container runs must enforce strict timeout and network isolation boundaries.
2. Quota Gate Check: Every trial execution must verify credential availability and budget allocation before dispatching to workers.
3. Zero State Mutation Outside Task Scope: Workspaces must be wiped or reset between consecutive runs.

## Tests or checks
- Targeted unit tests: `pytest tests/test_runner.py tests/test_queue.py tests/test_quota.py`

## What not to add here
Do not add modules here without Peter approval; package locations are frozen.
