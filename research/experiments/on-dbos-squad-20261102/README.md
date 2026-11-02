> VERIFIED 2026-11-02: some numbers below were corrected by an independent verifier; read VERIFIER-NOTES.md first.

# DBOS Squad Runner Prototype (`on-dbos-squad-20261102`)

Durable execution squad runner prototype built on **DBOS** (embedded SQLite step journaling, zero server daemons, zero network ports), evaluated side-by-side with our baseline in-memory squad runner (`ds_squad.py`) and F3's Temporal prototype under the standard five kill tests protocol (`_KILLTESTS.md`).

---

## 1. Clean-Venv Setup & Verification Steps Actually Ran

This prototype was developed and verified from a completely clean virtual environment without Docker or external server dependencies:

```bash
cd research/experiments/on-dbos-squad-20261102

# 1. Create clean Python 3.12 virtualenv
uv venv --python 3.12 .clean_venv
source .clean_venv/bin/activate

# 2. Install dependencies (dbos >=3.4.0, pytest >=8.0.0, pyyaml >=6.0.0)
uv pip install -e .

# 3. Run all unit and contract tests (zero Docker, zero background daemons)
pytest -v

# 4. Run lint checks
uvx ruff check .

# 5. Run the full Five Kill Tests suite
python src/driver.py --test all --kt1-trials 10

# 6. Run the Live Smoke test with 2 real omp -p items (attested google-antigravity/gemini-3.8-flash)
python src/driver.py --test smoke
```

---

## 2. Kill Tests Results Table

All tests were executed using the neutral test protocol (`_KILLTESTS.md`) and the standard `fake_agent.py` fixture.

| Test | Objective | Metric & Protocol | DBOS Result | Verdict | Exact Reproduction Command |
|---|---|---|---|---|---|
| **KT1 Crash** | Process survival across SIGKILL | 10 trials; random SIGKILL during execution; restart on same SQLite DB; assert completed, 1 effect per item, 0 manual intervention | 10/10 completed; exactly 1 effect per item across all 6 items in all 10 trials (0 duplicates); 6 to 8 raw executions per trial; 0 human steps needed | **PASS** (10/10) | `python src/driver.py --test kt1 --kt1-trials 10` |
| **KT2 Approval** | Human approval gate across reboot | Wait on gate signal; kill runner (simulating reboot); wait >60s; restart runner; deliver approval via `DBOS.send` | Resumed waiting on restart; 62.0s wait simulated; approval received; exactly 1 execution and 1 effect; state preserved | **PASS** | `python src/driver.py --test kt2` |
| **KT3 Quota Parking** | HTTP 429 / code 29 durable sleep | Exit 29 with quota reset at T = now + 90s; assert 0 attempts before T; resume at T + tolerance | Exactly 0 attempts before T (verified in ledger); resumed at T + 0.243s; 1 refusal, 1 execution, 1 effect | **PASS** | `python src/driver.py --test kt3` |
| **KT4 Code Change** | Orchestration change under live run | SIGKILL mid-flight; modify workflow code (swap/rename step); restart on same SQLite DB | Failed loudly with `DBOSUnexpectedStepError: During execution of workflow wf-kt4 step 1, function step_initial was recorded when step_MODIFIED was expected.` (If application_version bumped, refuses old workflows) | **FAILS LOUDLY** (visible error) | `python src/driver.py --test kt4` |
| **KT5 Caps** | Wall-clock timeout & retry iteration cap | Item 1 max_seconds=2.0 (duration 20s); Item 2 fail_transient=True (retry=2); verify status rows & no leaked child | Item 1 marked `killed-time`; Item 2 marked `failed` (exceeded retry cap); `pgrep -f fake_agent.py` empty (0 leaked PIDs) | **PASS** | `python src/driver.py --test kt5` |
| **Live Smoke** | Real `omp -p` execution with attestation | 2 items (haiku generation + critique); model `google-antigravity/gemini-3.8-flash`; subscription route | Both items completed; reports verified; 14 model requests served; 100% on-route (`gemini-3.8-flash`); 0 fallback events | **PASS** | `python src/driver.py --test smoke` |

---

## 3. Architecture & Core Components

- **`src/dbos_squad.py`**:
  - `@DBOS.workflow(name="squad_workflow")`: DAG coordinator evaluating `needs` dependencies, routing items to route queues, publishing live board status via `DBOS.set_event("board_status", ...)`, and enforcing cumulative dollar budget kills.
  - `@DBOS.workflow(name="item_workflow")`: Per-item lifecycle workflow handling human approval signals via `DBOS.recv(topic=...)`, catching `QuotaExhaustedError` and executing durable `DBOS.sleep()`, and handling continuation retries.
  - `@DBOS.step(...) launch_worker`: Subprocess executor spawning workers in isolated process groups (`start_new_session=True`), monitoring wall-clock caps and heartbeats, cleanly terminating process groups on cancellation, scanning OMP session logs for token/cost attestation, and classifying exits.
  - `DBOSSquadRunner`: High-level manager initializing embedded SQLite connection, running workflows with deterministic workflow IDs (`SetWorkflowID`), recovering interrupted runs, and sending signals (`DBOS.send`).
- **`src/fake_agent.py`**: Standard kill-test agent conforming strictly to `_KILLTESTS.md` CLI options (`--id`, `--ledger`, `--report`, `--seconds`, `--quota-until`, `--heartbeat`).
- **`src/driver.py`**: Automated test harness running and validating all kill tests and outputting dual-location receipts.

---

## 4. Comparison: `ds_squad.py` vs. Temporal (F3) vs. DBOS (F7)

| Dimension | `ds_squad.py` (Hand-rolled Baseline) | Temporal Prototype (F3) | DBOS Prototype (F7) |
|---|---|---|---|
| **Lines of Code** | 458 lines (single file) | 1,037 core LOC (2,797 total across 18 files) | **663 lines** (single core module) |
| **Persisted State** | None (in-memory dicts, periodic file rewrites) | Event-history replay in server DB | **Step-level journaling in SQLite** |
| **Processes & Servers** | 1 Python process | **2 processes**: Temporal dev server binary + Python worker | **1 Python process** (embedded library) |
| **Network Ports** | 0 ports (pure local process) | 1-2 ports (gRPC 7233, UI 8233 or ephemeral) | **0 ports** (SQLite connection) |
| **Crash Durability** | Process crash loses all running state; restart re-executes all items from zero | Full event history replay: recovers activities, completed workflows not re-run | Full step journal recovery: completed steps returned from SQLite; in-flight steps re-run |
| **External Binaries** | None | Downloads Temporal dev server binary on first run | **None** (pure `pip install dbos`) |
| **Determinism Rules** | None (standard Python) | **Strict determinism tax**: workflow code cannot use `time.time()`, random, or I/O directly | **Step-boundary determinism**: non-deterministic I/O allowed inside steps; workflow replays past steps |
| **Quota Parking** | None (crashes or requires human) | Activity retry with timer | Durable `DBOS.sleep`: 0 polling, 0 attempts before reset |
| **Human Approvals** | File presence polling (`gate: "approvals/x.txt"`) | Workflow signals (`release_gate`) | First-class `DBOS.recv` / `DBOS.send` with SQLite persistence across reboots |
| **History Compaction** | Manual cleanup | Requires manual `continue_as_new` | **Built-in step journal**: linear SQLite rows; no replay history limit |
| **Operational Cost** | Minimal | High (server process, network sockets, dev server lifecycle) | **Minimal** (single `.sqlite` file on disk) |

---

## 5. Workflow Friction (3 bullets)

1. **Step Name Determinism Requirement**: In DBOS, workflow and step functions must have stable module-level names (or explicit `@DBOS.step(name="...")`). Dynamically generated or closure-scoped functions cause recovery to fail with `is not a registered workflow function`.
2. **Timezone Pitfall on ISO Timestamps**: When parsing ISO reset timestamps from error logs (e.g. `2026-11-02T06:51:22.282745+00:00`), regexes that omit the subsecond or offset portion cause `datetime.fromisoformat()` to assume local machine time instead of UTC, converting a 90-second sleep into a 5-hour sleep.
3. **Queue Registration Lifecycle**: In DBOS 3.4.0, queues cannot be instantiated via constructor `Queue(...)` or registered before `DBOS.launch()`. Calling `DBOS.register_queue` before `DBOS.launch()` raises `System database accessed before DBOS was launched`.

---

## 6. What Surprised Me in DBOS (4 bullets)

1. **Zero-Server Durability is Genuine**: Unlike Temporal, which requires running a background daemon or embedded dev server listening on TCP ports, DBOS is a pure Python library that journals state straight to SQLite. A `kill -9` leaves a clean SQLite database that recovers instantly when a new process calls `DBOS.launch()`.
2. **Durable `DBOS.sleep` is Exact**: During KT3, `DBOS.sleep` parked the workflow in SQLite for the entire 90-second quota window without a single polling attempt against the ledger, waking up with a resume tolerance of only **0.243 seconds**.
3. **Clear Code-Change Detection (KT4)**: Modifying a workflow's step order or names between crash and restart triggers `DBOSUnexpectedStepError` immediately, failing loudly with an explicit message identifying the mismatched function name rather than silently producing corrupt outputs.
4. **Queue Primitives with Built-in Rate Limits**: DBOS queues include native `concurrency`, `worker_concurrency`, and `limiter` (`QueueRateLimit(limit=..., period=...)`), completely replacing custom semaphore and rate-limiting scripts for API routes.
