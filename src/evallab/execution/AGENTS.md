# Execution namespace

## Purpose
Reserved directory. Production execution lives in the current top-level runner, queue, quota, contracts, and preflight modules.

## What lives here / entry points
- Reserved empty package. Execution modules live in `src/evallab/`: `runner.py`,
  `queue.py`, `quota.py`, `execution_contracts.py`, `preflight.py`.

## Invariants or rules
- Task processes and external invocations need bounded lifetimes and the isolation/network policy of their selected supported backend.
- Free oracle/nop controls do not need model credentials. Noncontrol or remote specs retain policy, current authorization, and accounting requirements; stored credentials do not authorize spend.
- Allocate fresh owned per-trial workspaces. Do not wipe retained job evidence, shared worktrees, or user data to reset a trial.

## Tests or checks
Relevant existing tests: `tests/test_runner.py`, `tests/test_queue.py`, `tests/test_quota.py`; the assigned verifier runs only affected checks.

## What not to add here
No second execution implementation or incidental relocation into this namespace.
