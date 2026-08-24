Status: done
Last: merged as PR #126 (`317bfbb`)
Next: none
Blockers: none

# M020: QUEUE-PARALLEL Handoff

Status: complete
Last: Implemented atomic dispatch lease layer (`running/<spec>.lease`), 30s runner heartbeat, `--parallel` flag wiring, comprehensive unit and Hypothesis property test suite, and verified premerge gate.
Next: Integrator review and merge into main.
Blockers: none

## Summary of Implementation

This mission implements the dispatch lease layer specified in `docs/platform-architecture.md` §3.1:
1. **Atomic Lease Layer in `src/evallab/queue.py`**:
   - `DirectoryQueue.lease_path(spec)`: Deterministically maps experiment specs to `running/<spec>.lease`.
   - `DirectoryQueue.acquire_lease(spec, stale_seconds=300.0)`: Uses atomic `os.open(..., O_CREAT | O_EXCL)` to prevent double claims across concurrent workers/ticks. Unlinks and reclaims stale leases (> 300s old).
   - `DirectoryQueue.release_lease(spec)`: Cleanly unlinks the lease file upon successful completion or failure.
   - `DirectoryQueue.heartbeat_lease(spec)`: Touches the lease file to refresh `st_mtime`.
   - `DirectoryQueue.is_lease_stale(lease, stale_seconds=300.0)`: Checks if a lease has not received heartbeats within the stale window.
2. **Runner Wrapper Heartbeat in `src/evallab/runner.py`**:
   - Added `lease_path` to `RunRequest`.
   - Updated `run_harbor_process` watchdog loop to touch `lease_path` every 30 seconds (`DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0`).
3. **`--parallel` Flag Threading in `src/evallab/cli.py` & `src/evallab/queue.py`**:
   - Threaded `args.parallel` from `evallab tick` into `Executor.from_repo(root, parallel=args.parallel)`.
   - Supported `Executor.tick(parallel=N)` where `parallel=1` runs in strict sequential order (100% backward compatible with existing single-threaded semantics) and `parallel > 1` uses `ThreadPoolExecutor(max_workers=parallel)` bounded dispatch pool.

## Architecture Deltas from `docs/platform-architecture.md` §3.1

| Delta from §3.1 | Status in M020 | Implementation Notes |
|---|---|---|
| **1. Dispatch leases (`running/<spec>.lease`) & 30s heartbeat** | **LANDED** | Full kernel-level atomic `O_EXCL` acquisition, crash-safe release, runner watchdog heartbeat touch. |
| **2. Bounded parallel dispatch (`tick --parallel N`)** | **LANDED** | Threaded through CLI and Executor; `parallel=1` verified identical to single-threaded dispatch; `parallel > 1` uses bounded thread pool. |
| **3. Per-provider semaphores (`{codex: 2, claude-code: 2, ...}`)** | **DEFERRED** | Left for future mission to avoid overscoping per M020 brief. |
| **4. Orphan reconcile via Docker Compose container labels** | **DEFERRED** | Stale leases are reclaimed on acquire; live Docker Compose container inspection deferred to subsequent mission. |

## Mutation Evidence

### 1. Non-Atomic Claim Mutation
When lease acquisition is mutated from atomic `O_EXCL` to non-atomic check-then-create (`if not path.exists(): path.write_bytes(...)`):
```text
FAILED tests/test_queue_properties.py::test_property_concurrent_lease_acquire_is_strictly_exclusive - AssertionError: Expected exactly 1 successful claim among 2 racers, got 2
assert 2 == 1
 +  where 2 = len([PosixPath('.../queue/running/oracle-None.lease'), PosixPath('.../queue/running/oracle-None.lease')])
```
Restoring atomic `O_EXCL` open restored green: `1 passed in 0.28s`.

### 2. `--parallel 1` Compatibility Mutation
When `parallel=1` pass-through is altered to reverse execution order or deviate from sequential semantics:
```text
FAILED tests/test_queue.py::test_parallel_1_compatibility_matches_single_threaded - AssertionError: assert ['seq-spec-3', 'seq-spec-2', 'seq-spec-1'] == ['seq-spec-1', 'seq-spec-2', 'seq-spec-3']
  At index 0 diff: 'seq-spec-3' != 'seq-spec-1'
```
Restoring sequential pass-through restored green: `1 passed in 0.65s`.

## Quality Gates & Verification

- `uv run ruff check src/ tests/`: PASS (zero errors)
- `uv run pytest tests/test_queue.py tests/test_queue_properties.py`: PASS (44 passed in 7.44s)
- `./scripts/premerge.sh`: PASS
  - 1332 passed, 1 skipped, 1 xfailed in 89.97s
  - `SMOKE PASS both-stores-agree`
  - `ty` diagnostics: 27 <= baseline 28

## Integrator verification (independent of the authoring agent)

The authoring agent was killed twice by transport/rate-limit errors before it could push;
its work was checkpointed by the integrator and verified here from scratch.

### Mutation evidence, re-run independently

| Mutation | Result |
|---|---|
| `O_EXCL` replaced with check-then-create + plain `O_CREAT` | `test_property_concurrent_lease_acquire_is_strictly_exclusive` fails: *"Expected exactly 1 successful claim among 2 racers, got 2"* |
| `heartbeat_lease` made a no-op (returns before touching) | `test_lease_heartbeat_updates_mtime` fails: `assert 1787086588.78 >= (1787086688.78 - 5.0)` |

Restored → 44 passed.

### Concurrency is observed, not assumed

`test_parallel_dispatch_executes_multiple_specs_concurrently` instruments the runner and
records peak overlap, asserting `max_concurrent >= 2` for `tick(parallel=3)`, that all 3
specs reach `done`, and that `list_leases()` is empty afterwards — so the pool really
overlaps and the leases really get released.

### End-to-end on the real repository

```
$ uv run python -m evallab.cli tick --parallel 3
dispatched 0 experiment(s)
quarantined: no

$ uv run python -m evallab.cli tick --help
usage: evallab tick [-h] [--parallel N]
  --parallel N  Bounded parallel dispatch worker count (default: 1)
```

Zero dispatched because the approved queue is empty; the flag is wired, reaches
`Executor.from_repo(root, parallel=...)`, and no billable work was dispatched.

### What is NOT built, stated plainly

Two of the four §3.1 deltas are deferred and the board must not read this as E01 complete:

- **Per-provider semaphores** (`{codex: 2, claude-code: 2, oracle: 4}`) — not implemented.
  `--parallel N` is a single global cap. Provider-level concurrency limits still have to be
  managed by hand, which was the original complaint that motivated E01.
- **Orphan reconcile via Docker Compose labels** — not implemented. Stale leases are
  reclaimed opportunistically on the next `acquire_lease`, but nothing verifies that no live
  container exists for the task, and nothing transitions an interrupted spec to
  `failed(execution_interrupted)` while preserving its partial dir.

The lease layer those two features depend on now exists and is tested, which is what this
mission was narrowed to deliver.
