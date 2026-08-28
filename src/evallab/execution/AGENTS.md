# Execution Subsystem

This directory is a **reserved empty package**. Execution lives in top-level
modules: `src/evallab/runner.py`, `queue.py`, `quota.py`,
`execution_contracts.py`, and `preflight.py`. Do not add modules here;
package locations are frozen.

## Invariants (enforced on those modules)
1. Fail-Closed Sandboxing: All external tool invocations and container runs must enforce strict timeout and network isolation boundaries.
2. Quota Gate Check: Every trial execution must verify credential availability and budget allocation before dispatching to workers.
3. Zero State Mutation Outside Task Scope: Workspaces must be wiped or reset between consecutive runs.

## Testing & Verification
- Targeted unit tests: `pytest tests/test_runner.py tests/test_queue.py tests/test_quota.py`
