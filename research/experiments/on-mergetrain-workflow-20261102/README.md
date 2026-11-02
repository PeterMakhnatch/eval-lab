# Durable Temporal Merge Train Prototype (`on-mergetrain-workflow-20261102`)

Durable execution merge train workflow built with Temporal Python SDK, tested against an isolated local bare git repository (`origin.git`) and a JSON-driven fake `gh` CLI shim on `PATH`.

## Why

Merging $N$ approved pull requests across concurrent overnight agent squads is a stateful multi-step workflow:
1. Rebase feature branches onto advancing `main`.
2. Automatically resolve conflicts confined to generated documentation (`docs/repo-map.md`, `docs/INDEX.md`) by rerunning the generator and committing.
3. Classify external source conflicts as `needs-human` and skip to the next PR without halting the train.
4. Run verification gates (`ruff`, `repomap`, `governance`).
5. Push rebased commits with `--force-with-lease`.
6. Asynchronously poll and wait for external CI checks (`quality-required`, `typecheck-required`).
7. Gate on human approval signals (`approve(pr)`), support interactive operator overrides (`skip(pr)`, `abort`), and expose live inspection (`board()`).
8. Squash-merge to `main` with guaranteed idempotency and crash resilience.

Our baseline plain-Python script (`tools/merge_train.py`, 167 lines) runs in process memory and executes blocking `time.sleep` loops. If the host or worker process crashes mid-train, all runtime state is lost, the train stops, and restarting risks double-merging or re-running already completed rebases.

This prototype proves that Temporal provides durable timers, persistent event histories, crash deduplication, and push-based signals for unattended merge trains.

---

## Setup & Clean Virtualenv Verification

To build a clean virtual environment and run the test suite from scratch:

```bash
cd research/experiments/on-mergetrain-workflow-20261102

# 1. Create clean virtualenv with Python >=3.12
uv venv --python 3.12 .clean_venv
source .clean_venv/bin/activate

# 2. Install dependencies (temporalio, pytest, pytest-asyncio, ruff)
uv pip install -e ".[dev]"

# 3. Run the full test suite (starts ephemeral in-memory Temporal dev-servers)
pytest -v

# 4. Run linter
ruff check src tests
```

---

## Architecture

### 1. `MergeTrainWorkflow` (`src/mergetrain/workflows.py`)
- **State Machine**: Sequentially iterates through requested PR numbers.
- **Durable CI Polling**: Replaces blocking sleep with `await workflow.sleep(poll_interval)`. When run in Temporal's time-skipping test environment (`start_time_skipping()`), multi-minute CI polling runs instantly in sub-second wall time.
- **Signals**:
  - `approve(pr: int)`: Unblocks the PR at the human approval gate.
  - `skip(pr: int)`: Bypasses the current or upcoming PR, continuing the train immediately.
  - `abort()`: Cancels the train, marking active and pending PRs as `ABORTED`.
- **Queries**:
  - `board() -> str`: Returns a formatted Markdown table of all PR statuses (matching bridge status standards).
  - `status() -> dict`: Returns structured JSON dictionary of PR states, approval sets, and merge commits.

### 2. Activities (`src/mergetrain/activities.py`)
- `view_pr_activity`: Queries PR state, mergeability, head commit OID, and status checks via `gh pr view`.
- `rebase_pr_activity`: Spawns an isolated throwaway worktree (`on-mt-<N>`), fetches latest `origin/main`, rebases, detects conflicted files via `git diff --diff-filter=U`:
  - If conflicts $\subseteq$ `{"docs/repo-map.md", "docs/INDEX.md"}`: auto-resolves with `--ours`, continues rebase, reruns generator stub, and commits `chore: regenerate repo map after rebase`.
  - If any conflict outside generated files: aborts rebase, removes worktree, and flags `needs_human=True`.
  - Runs local linters/checks, pushes with `--force-with-lease`, and tears down the worktree.
- `check_ci_activity`: Inspects `statusCheckRollup` for required checks (`quality-required`, `typecheck-required`).
- `merge_pr_activity`: Checks whether PR is already merged (idempotency guard), invokes `gh pr merge --squash`, touches gate token files, and returns the merge commit.

### 3. Fake GitHub CLI Shim (`src/mergetrain/fake_gh.py`)
- Standalone CLI placed on `PATH` as `gh`.
- JSON-backed state (`FAKE_GH_STATE_FILE`) and append-only merge ledger (`FAKE_GH_MERGE_LEDGER`).
- Dynamic evaluation against bare git repo: evaluates mergeability via `git merge-tree --write-tree` and ancestor status via `git merge-base --is-ancestor`, exactly mirroring GitHub's pull request mergeability engine.
- Atomic squash-merge onto bare `main` branch via temporary detached worktree.

---

## Verified Scenarios (10/10 Passed)

| Scenario | Test Function | Description | Verdict |
|---|---|---|---|
| **1. Clean Merge Order** | `test_scenario_1_clean_merge_three_prs_in_order` | Merges PR 101, 102, 103 in sequence onto advancing `main`. | **PASS** |
| **2. Generated Conflict** | `test_scenario_2_conflict_in_repomap_autoresolved` | Conflicting `docs/repo-map.md` auto-resolved via generator stub and committed. | **PASS** |
| **3. Source Conflict** | `test_scenario_3_conflict_in_source_file_needs_human` | Conflicting `src/calc.py` marks PR 301 `NEEDS_HUMAN`; train advances to merge PR 302. | **PASS** |
| **4. CI Red Skipped** | `test_scenario_4_ci_red_skipped_and_reported` | Failing `quality-required` check marks PR 401 `CI_FAILED`; PR 402 merges. | **PASS** |
| **5. Time-Skipping CI** | `test_scenario_5_ci_pending_then_green_time_skipping` | `PENDING` checks wait via durable timer; skips time instantly upon green CI transition. | **PASS** |
| **6. Crash Recovery** | `test_scenario_6_worker_kill_midway_pr2_no_double_merge` | Worker killed with `kill -9` during PR 2; replacement worker resumes without double-merging PR 1. | **PASS** |
| **7. Human Gate** | `test_scenario_7_human_approve_signal_gates_merge` | PR held in `WAITING_APPROVAL`; merges cleanly once `approve(701)` signal is sent. | **PASS** |
| **8. Skip Signal** | `test_skip_signal_skips_pr_and_continues` | Operator signal `skip(801)` bypasses PR 801; PR 802 is approved and merged. | **PASS** |
| **9. Abort Signal** | `test_abort_signal_halts_train` | Operator signal `abort()` immediately halts train, marking pending PRs `ABORTED`. | **PASS** |
| **10. Dry Run** | `test_dry_run_mode` | Validates rebase and CI checks without executing merge activities. | **PASS** |

---

## Comparison: Plain Python Script vs. Temporal Workflow

| Metric / Dimension | Plain Python (`tools/merge_train.py`) | Temporal Prototype (`src/mergetrain/`) |
|---|---|---|
| **Lines of Code** | 167 raw (144 LOC) | 724 workflow/activities/models LOC (+ 271 LOC `fake_gh.py`) |
| **State Persistence** | None (lost on process exit or terminal close) | Fully durable event history in Temporal server |
| **Crash Mid-Train** | Process dies; restarting requires manual argument editing to avoid re-evaluating merged PRs | Seamless: completed activities replayed from history; in-flight work resumed |
| **Double-Merge Risk** | High if restarted without inspecting GitHub state | Zero: activity completion recorded in durable history + activity-level idempotency checks |
| **CI Waiting Mechanism** | Blocking `time.sleep(30)` loop tying up CPU thread | Durable `workflow.sleep` timer; testable with instant time-skipping |
| **Operator Interactivity** | None (must kill script via Ctrl-C or let it run) | Push-based signals (`approve`, `skip`, `abort`) evaluated while sleeping/waiting |
| **Observability** | Append-only log file (`merge-train.log`) | Queryable state (`board()`, `status()`), structured execution graph in Temporal Web UI |
| **Operational Overhead** | 1 single-threaded script, no infrastructure | 1 lightweight Temporal server process (zero Docker required, embedded dev server) |
