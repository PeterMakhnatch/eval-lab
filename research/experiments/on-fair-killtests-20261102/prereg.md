# Pre-Registration: F11-FAIR-KILLTESTS
**Title:** Fair Kill-Test Comparison: Matched Protocols, Hardened Baseline Included, Real Runs Only, Hang Test  
**Author:** F11-FAIR-KILLTESTS Worker  
**Date:** 2026-11-02  
**Evaluation Target:** 5 Orchestration Engines across 7 Standardized Kill Tests (KT1–KT7)

---

## 1. Research Question & Objective
F9's kill-test suite suffered from critical methodological flaws caught by independent verifiers (VF-F2-FINAL and VF-F-WORKFLOWS):
1. **Unmatched protocols**: In KT1, Baseline was killed at 4–25s on 20s items, while Temporal/DBOS/Dagu were killed at 2–5s on 4–6s items. "Baseline re-runs completed work" was not demonstrated (5 of 10 baseline trials had 0 re-runs).
2. **Missing candidate**: The R7-hardened baseline (`ds_squad_v2.py`, with surviving worker adoption, quota parking, atomic heartbeat, and terminal status rows) was never benchmarked through the kill tests.
3. **App code vs. engine capabilities**: In KT3, F9 claimed "only DBOS parks natively", but Temporal failed purely due to F3 lacking an exit-29 handler. A 20-line stock Temporal activity using `ApplicationError(next_retry_delay=...)` achieves zero attempts before $T$ and resumes at $+0.026$s.
4. **Unexecuted & hardcoded tests**: KT4 for Temporal was a hardcoded string; DBOS KT4 crashed on an argparse failure; Dagu KT4 "recovers" contradicted its receipt (`succeeded: false`, rc 1).
5. **Orphan concurrency blindness**: DBOS KT1 launched duplicate agent processes on crash restart (overlapping up to 3.6s), and effects stayed 1 only because `fake_agent.py` guarded by ID.
6. **Weak KT5 predicates**: KT5 checked only whether an agent started (`exec_count >= 1`), not verifying engine status rows (`killed-time`, `failed`).

The objective of F11 is to execute a rigorous, fair, matched-protocol comparison of all five engines using real executions, ledger-derived metrics, verified status rows, and an explicit live-hang test (KT6) and cross-runner worktree race test (KT7).

---

## 2. Cohort of Engines
Five engines are evaluated on identical test scenarios:
1. **Baseline Original (`ds_squad.py`)**: Legacy hand-rolled Python squad runner using in-memory state, subprocess management with `start_new_session=True`, and no worker adoption or quota parking.
2. **Baseline Hardened R7 (`ds_squad_v2.py`)**: Phase 0 hardened Python squad runner with atomic `RUN/state.json`, surviving worker adoption (`AdoptedProc`), automatic HTTP 429 / quota classification and parking (`parked-quota`), atomic `heartbeat.json`, and terminal spend rows.
3. **Temporal (`temporalio>=1.35.0`)**: Temporal dev server configured with SQLite persistent store (`--db-filename`), executing Python workflows and activities.
4. **DBOS (`dbos>=3.4.0`)**: Embedded Python workflow engine executing steps and workflows with SQLite system database (`sqlite:///<path>`).
5. **Dagu (`dagu v1.16+`)**: Single Go binary DAG engine managing filesystem-backed attempts (`dag.json`) and YAML step definitions.

---

## 3. Fixture Specification: `fake_agent.py`
The agent fixture operates in two strictly separated modes:
- **GUARDED Mode**: Idempotent execution. Records execution start, sleeps for the specified duration, and appends a single idempotent effect to the ledger iff no effect for `item_id` has been recorded.
- **UNGUARDED Mode**: Non-idempotent execution. In addition to start/exit events, every execution appends an unguarded side-effect entry (`unguarded-effect`), making duplicate or concurrent executions immediately visible.

### Ledger Record Contract:
Every trial emits an atomic append-only JSONL ledger containing:
- Start event: `{"kind": "start", "id": item_id, "pid": pid, "t": float}`
- Heartbeat event (optional): `{"kind": "heartbeat", "id": item_id, "pid": pid, "t": float}`
- Effect event: `{"kind": "effect", "id": item_id, "pid": pid, "t": float}` (guarded)
- Unguarded effect: `{"kind": "unguarded-effect", "id": item_id, "pid": pid, "t": float}` (unguarded)
- Quota refusal event: `{"kind": "quota-refusal", "id": item_id, "pid": pid, "t": float, "reset_at": float}`
- Exit event: `{"kind": "exit", "id": item_id, "pid": pid, "t": float, "rc": int}`

No metric may be derived from an unverified in-memory counter; all counts must be computed from raw ledger rows.

---

## 4. Matched Test Protocols

### KT1: Runner/Worker `kill -9` Mid-Run (Event-Driven Trigger)
- **Workload**: 6 items (A1, A2, A3, B1, B2, B3) arranged in 2 parallel chains of 3 sequential items (concurrency 2). Each item runs for 20.0 seconds.
- **Trigger**: Runner or worker is terminated with `SIGKILL` (`kill -9`) precisely when **at least 1 item has COMPLETED and at least 1 item is actively IN FLIGHT**.
- **Execution**: 10 independent trials per engine under GUARDED mode; 10 trials per engine under UNGUARDED mode.
- **Orphan Scenarios**:
  - Scenario O1 (Surviving Orphans): Worker is killed, child agent processes survive (realistic POSIX process group / session behavior).
  - Scenario O2 (Terminated Workers): Worker and all child agent processes are killed simultaneously.
- **Metrics**:
  - Completed items re-executed (Target: 0).
  - In-flight items duplicated (Target: 0 for adoption; <= 1 attempt restart for at-least-once).
  - Overlap seconds of concurrent live copies (Target: 0.0s).
  - Items lost / uncompleted (Target: 0).
  - Time to recovery (wall-clock seconds from kill to workflow completion).

### KT2: Human Approval Survives Reboot
- **Protocol**: 2-step pipeline (`PREP` -> `GATE` -> `DEPLOY`). `PREP` runs for 2s. The workflow blocks waiting for human approval.
- **Intervention**: Runner and engine server (where applicable) are terminated cleanly. The machine remains "down" for 60+ seconds.
- **Resumption**: Engine is restarted, and approval is delivered $\ge 60$ seconds after restart.
- **Requirement**: `PREP` must NOT re-execute; `DEPLOY` must execute exactly once.
- **Cleanup**: Zero stray processes permitted after test completion.

### KT3: Quota Park Until $T$ With Auto-Resume
- **Protocol**: Item encounters quota exhaustion resetting at $T = \text{now} + 90.0\text{s}$ (exit code 29 with ISO reset timestamp).
- **Mechanism Rule**: Each adapter must implement its engine's idiomatic parking mechanism in $\le 30$ lines of adapter code:
  - Temporal: `ApplicationError("quota", next_retry_delay=timedelta(seconds=wait))` (Server-side timer backoff).
  - DBOS: Catch `QuotaExhaustedError`, invoke `DBOS.sleep(wait)` (Durable workflow sleep).
  - Baseline R7: `classify_quota()`, transition to `parked-quota`, auto-resume when `now >= reset_time`.
  - Dagu: Dynamic wrapper or delay retry policy.
- **Metrics**:
  - Attempts before $T$ (Target: 0).
  - First execution start after $T$ (Target: within $+2.0\text{s}$ of $T$).
  - Retries consumed (Target: 0 consumed during parking).

### KT4: Workflow Code Change Between Crash and Resume
- **Protocol**: Workflow executing Step 1 is killed. Before restart, the workflow definition is modified (a step is inserted or renamed).
- **Requirement**: Real execution only. Must capture actual exception text, error codes, and recovery behavior. Literal hardcoded classification strings are strictly prohibited.

### KT5: Caps Enforcement & Status-Row Predicates
- **Protocol**:
  1. Time cap: Item sleeping for 30s with cap set to 3s.
  2. Retry cap: Item failing immediately (`fail-transient`) with max attempts set to 3.
- **Predicate Verification**: The test passes iff the engine's own internal persistent state records explicit terminal status:
  - Time cap item must record `killed-time` (or engine timeout equivalent).
  - Retry cap item must record `failed` with exactly 3 execution attempts.
- **Process Leak Check**: Verify 0 lingering `fake_agent.py` processes, runner processes, or server daemons.

### KT6: Live Child Process Hang (0% CPU, Silent Output)
- **Problem Formulation**: A child worker process remains alive indefinitely at 0% CPU with zero stdout/stderr output and zero network sockets (Incident A).
- **Evaluation**: Which mechanism catches the hang, and at what latency:
  1. Engine-level wall-clock timeout (`start_to_close` / `max_seconds`).
  2. Process heartbeat loop (testing whether PID-only checks fail vs. output timestamp checks).
  3. Output-silence watchdog daemon.
- **Requirement**: Measure exact time to termination and verify child process group is terminated cleanly.

### KT7: Multi-Runner Worktree Lock Race (Incident C)
- **Protocol**: $N = 4$ independent runner processes execute `git worktree add -b` concurrently into the same repository within 1.0 second.
- **Measurement**: Does the engine's internal concurrency mechanism prevent Git lock contention (`.git/config.lock` / `.git/worktree.lock`) across separate processes?
- **Baseline Fix**: `fcntl.flock` or randomized retry with jitter.

---

## 5. Statistical Methods & Reporting Standards
- **Rates and Proportions**: Reported as $k/n$ with 95% Wilson score intervals (`stats.wilson(k, n)`).
- **Paired Comparisons**: When outcomes are paired across identical scenarios, report discordant pairs $b, c$ and exact McNemar $p$ via `stats.paired()`. If data are unpaired, McNemar is prohibited.
- **Receipt Integrity**: Every number in the report must be computed directly from persisted JSON/JSONL receipt files. All receipt files must have their SHA256 hashes recorded in the master manifest and cited in the final report.
