Status: done
Last: merged as PR #144 (`58880a9`)
Next: none
Blockers: none

# M044 queue repair

## Scope

Repaired the sanctioned directory-queue path after the Antigravity interruption report.
No paid model or provider call was run.

## Root causes

- `evallab tick` rendered preflight in the CLI and `Executor._tick_locked` rendered it again. The executor call was also hidden from the operator-facing dispatch path.
- A sequential drain had no progress callback. The queue state moved to `running`, but the CLI did not identify the spec or the child log until completion.
- Restart reconciliation treated an incomplete child as indefinitely running. The runner now writes an atomic `.executor/<spec>.state.json` launch record before Harbor starts; reconciliation fails an expired state with `trial_wall_clock_timeout` and writes a queue reason.
- The gate and `approve` used one checkout-wide quota reader. A Codex rate-limit snapshot could therefore be printed for Antigravity or Cursor and the approval text always named ChatGPT.

## Changes

- `Executor.from_repo(..., progress=...)` and dispatch progress messages identify each spec, agent, child log, and terminal queue state.
- Tick preflight is rendered once by the CLI; library executor ticks remain silent.
- Runner state records `running` before child launch and `completed`/`failed` metadata after the child exits. Existing Harbor watchdogs still enforce per-trial and aggregate spec deadlines.
- `PolicyGate` accepts a per-agent headroom reader and caches readings by agent. `Executor._repo_headroom` scans only the declared agent lane.
- Quota snapshots are currently measured only for Codex. Cursor and Antigravity token rollouts remain consumption evidence with an unknown allowance; they cannot inherit Codex's snapshot.
- Billing text names Google subscription OAuth for Antigravity, ChatGPT/Codex for Codex, and Cursor subscription/API-key policy state for Cursor.

## Verification

Targeted suites passed:

```text
uv run pytest -q tests/test_m044_queue_repair.py tests/test_queue.py tests/test_quota.py tests/test_quota_gate.py tests/test_preflight.py tests/test_runner.py
```

Targeted Ruff passed for changed Python files. Three deliberate mutants were each killed by regression tests:

- removed the non-Codex quota filter;
- disabled expired-running-state recovery;
- disabled dispatch progress callbacks.

The mutation runner was unavailable (`mutmut` is not installed), so the checks used temporary source mutations with automatic restoration and direct pytest exit codes.
