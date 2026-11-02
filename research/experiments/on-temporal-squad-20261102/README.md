# Temporal Squad Runner Prototype (`on-temporal-squad-20261102`)

Durable execution runner prototype for overnight headless OpenHands/OMP squads, replacing ad-hoc in-memory polling scripts (`ds_squad.py`) with durable Temporal workflows.

## Why

Our original squad runner (`ds_squad.py`, 458 LOC) maintains all runtime orchestration state solely in process memory. If the runner process crashes or restarts, all active items are killed and restart from zero, loss of quota/balance halts progress until human intervention, and dynamic adjustments (like adding items or changing concurrency) require tricky polling or file-watching.

This prototype proves that Temporal's durable execution model solves these operational vulnerabilities without requiring Docker or heavyweight infrastructure, using Temporal Python SDK's embedded dev server.

---

## Setup & Clean Virtualenv Verification

To create a clean environment and run all tests and linting:

```bash
cd research/experiments/on-temporal-squad-20261102

# 1. Create clean virtualenv with Python >=3.12
uv venv --python 3.12 .clean_venv
source .clean_venv/bin/activate

# 2. Install dependencies (temporalio, pytest, pytest-asyncio, ruff)
uv pip install -e ".[dev]"

# 3. Run full test suite (ephemeral dev servers, zero external daemon prerequisites)
pytest -v

# 4. Run linter
ruff check src tests demo
```

To run the live smoke demo with real `omp -p` using `google-antigravity/gemini-3.8-flash`:
```bash
python demo/run_live_smoke.py
```

---

## Architecture

### 1. `SquadWorkflow` (Parent Workflow)
- **Input**: `SquadPlan` or `SquadWorkflowInput` (for Continue-As-New).
- **DAG & Gates**: Evaluates dependencies (`needs`) and gate conditions (`gate`). When dependencies are satisfied and gate is released (via signal `release_gate`), starts child `ItemWorkflow`.
- **Concurrency**: Per-route and global parallelism caps enforced via Temporal state tracking and workflow-level condition waiting (`workflow.wait_condition`).
- **Cumulative Budget**: Tracks cumulative spend across items and generations. If spend exceeds `budget_usd`, cancels in-flight items and marks pending items as `killed-budget`.
- **Dynamic Signals**:
  - `add_item(item: ItemPlan)`: Dynamically appends an item to the running squad.
  - `cancel_item(item_id: str)`: Cancels pending or running child workflows and process groups.
  - `set_parallelism(max_parallel: int)`: Dynamically adjusts concurrency.
  - `release_gate(gate_name: str)`: Signals gate release to unblock gated items.
- **Queries**:
  - `board() -> str`: Renders live markdown status table matching `status.md` format.
  - `status() -> dict`: Returns structured JSON dictionary of all item states and spend.
- **Continue-As-New**: When completed item count reaches `continue_as_new_threshold`, invokes `workflow.continue_as_new(...)`, compacting event history while carrying over cumulative spend and terminal item states.

### 2. `ItemWorkflow` (Child Workflow)
- **Input**: `ItemWorkflowInput` (`ItemPlan`, `SquadPlan`, attempt, why).
- **Activity Execution**:
  1. `route_quota_ok`: Verifies route quota balance; raises non-retryable `RouteExhausted` if depleted.
  2. `launch_worker`: Launches headless worker subprocess in new process group (`start_new_session=True`). Heartbeats every second with PID, log size, and elapsed time. Cancellation issues `SIGKILL` to entire process group and reaps child.
  3. `read_report_state`: Validates `report.md` terminal line (`DS-DONE` or `DS-BLOCKED`). If missing or invalid, raises `ReportMissing` which triggers continuation retry attempt up to `item.retry`.
  4. `settle_spend`: Scans session `.jsonl` records, validates served model attestation, and aggregates tokens/cost.

---

## Comparison: `ds_squad.py` vs. Temporal Prototype

| Dimension | `ds_squad.py` (Script) | `temporal-squad` (Temporal Prototype) |
|---|---|---|
| **Lines of Code** | 458 lines (single file) | 1,037 lines (clean package: models, activities, workflows, exceptions) |
| **Persisted State** | None (in-memory dicts + file overwrites to `status.md`) | Durable event history persisted in Temporal server; queryable anytime |
| **Crash Behavior** | Process crash (`kill -9`) kills everything; restart restarts all items from zero | Worker crash recovers seamlessly: in-flight activities re-attach or recover idempotently; completed items never re-run |
| **Retries** | Ad-hoc while loop with continuation re-launch | Native activity retry policies (backoff, non-retryable error filters) + workflow-level continuation retries |
| **Signals & Control** | Re-reads `plan.json` file mtime, polling `RUN/STOP` file | First-class Temporal signals (`add_item`, `cancel_item`, `set_parallelism`, `release_gate`) |
| **Observability** | Flat file append `_ds.log` + periodic overwrite `status.md` | Temporal Web UI (or CLI), `fetch_history()`, deterministic queries (`board`, `status`) |
| **History Scaling** | Unbounded memory dicts in single long-lived process | Native `continue_as_new` history compaction preserving cumulative state |
| **Concurrency Control** | `len(running) >= max_parallel` in polling sleep loop | Deterministic per-route queue / semaphore via `wait_condition` & dynamic `set_parallelism` |
| **Process Group Cleanup** | `os.killpg(SIGTERM)` with 20s poll loop | Activity cancellation hook with immediate process group `SIGKILL` and process reaping (zero orphan processes) |
| **Operational Footprint** | 1 Python process, 0 network ports, local disk | 1 Temporal dev-server binary (or ephemeral test server), 0 Docker containers, 1 ephemeral port |
| **Open Failure Modes** | Host reboot wipes `/tmp`; silent worker hang | Dev server persistence loss if using in-memory mode; network partition to remote server |

---

## Honest Boundaries: What Was NOT Made Durable

1. **Shared Git Worktree State**:
   Temporal persists workflow execution state and activity parameters, but it does NOT manage the state of Git working trees. If an agent commits half-finished or conflicting files in a shared Git worktree before crashing, Temporal will re-run or recover the activity, but the dirty Git checkout remains. Workspace isolation (e.g. ephemeral directories or dedicated branch worktrees per attempt) remains necessary.
2. **Untracked File System Side-Effects**:
   If a non-idempotent tool creates untracked scratch files outside `raw/<id>/` during an attempt, activity retry does not automatically rollback OS file modifications unless an explicit compensation activity is registered.
3. **Third-Party Model Provider API Quotas**:
   If an external provider (DeepSeek, Gemini, Anthropic) hard-rate-limits or exhausts project token quotas mid-flight, Temporal parks the item with `RouteExhausted` non-retryably, but restoring capacity still requires external account replenishment.
