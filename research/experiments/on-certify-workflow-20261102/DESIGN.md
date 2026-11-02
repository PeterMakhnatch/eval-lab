# CertifyGold Durable Workflow Architecture (DESIGN.md)

**Workflow ID:** `WF-03` / `F5-CERTIFY-WF`  
**Date:** 2026-11-02  
**Target System:** `evallab.gold_certifier`, `evallab.mutant_auditor`, `evallab.modal_cells`  
**Author:** Eval Lab Fleet Engineering (F5-CERTIFY-WF)

---

## 1. Executive Summary & Problem Context

Corpus-scale certification (`evallab certify-gold`) runs hundreds of paid, sandboxed evaluation cells across dozens of software packages under hard spend caps and strict time limits. Each package undergoes a multi-stage pipeline:
1. AST-level semantic mutation generation (`mutant_auditor.py`).
2. Train/eval mutant partitioning and LLM behavioral test synthesis (`gold_certifier.py`).
3. Multi-gate sandbox execution on Modal (`modal_cells.py`: reference, nop, witness, exit0/.pth, train mutants, eval mutants).
4. Spend gate accounting (reserve up front, settle to measured sandbox seconds).
5. 6-gate verdict synthesis (`gold_structural`, `gold_heldout`, `not_gold`).
6. Aggregated manifest generation (`gold-v1-manifest.json`).

Historically (e.g. overnight campaigns `W3B-EXHAUSTIVE-GOLD-CERTIFIER`, `A2-AUDIT-SWEEP`, `A4-GOLD-BATCH`), this pipeline ran as custom Python batch scripts (`run_gold_certification.py`, `run_gold_batch.py`) utilizing `ThreadPoolExecutor`, ad-hoc disk JSON markers, and manual CLI resume logic.

### Systematic Failure Modes in Current Script Architecture
- **Worker Crashes & Double Invocations:** If a machine restarts or a process is killed mid-run, thread pools lose state. Resuming requires custom scanning of disk folders. If a cell was executing when killed, it may be re-executed from scratch, double-billing Modal compute.
- **Premature Cap Halts Without Resume:** Upfront spend reservations exhaust caps prematurely (e.g. reserving $0.05/cell on $0.008 real spend). When a cap is hit, scripts exit with `sys.exit(1)`. To continue, operators must edit the script file, adjust cap variables, and re-run.
- **Brittle Signal & Timeout Handling:** Long batches killed at wall-clock limits produce zero output or unrecovered spend. No mechanism exists to park a run safely, ping an operator for a budget increase, and automatically resume or cleanly write a partial manifest upon timeout.
- **Lack of Observability:** Coordinators must tail log files or run ad-hoc counting scripts to know which packages are currently executing, which failed, and how much money has been spent.

Adopting a durable orchestration engine (Temporal) eliminates these failure modes:
- **Durable State:** Workflow execution survives worker kills, host reboots, and network disconnects.
- **Per-Cell Idempotency:** Completed cells are cached by content-addressed keys (`cell_key = sha256(...)`), returning instantly at $0.00 cost.
- **First-Class Signals & Gates:** Spend caps can be raised dynamically via `raise_cap(usd, approver)` signals. If approval times out, the workflow gracefully produces a validated **PARTIAL manifest** with coverage $k/N$.
- **Interactive Board Query:** `board()` query exposes live progress, cumulative spend, active routes, and package statuses without file-locking races.

---

## 2. Mapping the Pipeline to Workflows & Activities

The Temporal architecture splits certification into two workflow tiers and five core activities:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        CertifyCorpusWorkflow                           │
│  - Spend Gate (cap_usd, cumulative_spend, park on CapReached)          │
│  - Route Concurrency Management (Modal route, Synth route)             │
│  - Signals: raise_cap, pause, cancel_package | Query: board()          │
│  - Continue-As-New (every N cells to bound history size)               │
└───────────────────┬────────────────────────────────────────────────────┘
                    │ spawns child workflows
                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        CertifyPackageWorkflow                          │
│  1. Activity: generate_mutants(package_id)                             │
│  2. Partition: split_mutants (deterministic train vs eval)             │
│  3. Activity: synthesize_tests(package_id, train_mutants) [Route: LLM]│
│  4. Cell Execution Loop: [Route: Modal]                                │
│     ├── reference cell       ──► run_cell(cell_key)                    │
│     ├── nop cell             ──► run_cell(cell_key)                    │
│     ├── witness / conftest   ──► run_cell(cell_key)                    │
│     ├── exit0 / .pth tamper  ──► run_cell(cell_key)                    │
│     ├── train mutant cells   ──► run_cell(cell_key) [N cells]          │
│     ├── eval mutant cells    ──► run_cell(cell_key) [M cells]          │
│     └── eval base mutants    ──► run_cell(cell_key) [M cells]          │
│  5. Activity: settle_spend(package_id, reserved_usd, actual_usd)       │
│  6. Verdict Adjudication: gold_structural, gold_heldout, not_gold      │
└───────────────────┬────────────────────────────────────────────────────┘
                    │ reports results to corpus parent
                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Activity: write_manifest(manifest, output_dir, partial=...)           │
│  - Produces gold-v1-manifest.json with coverage k/N and Wilson CIs     │
└────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Workflow Definitions
- **`CertifyCorpusWorkflow`**:
  - **Inputs:** `CorpusPlan` containing task list, `cap_usd`, approval metadata, route concurrency caps (`modal_concurrency`, `synth_concurrency`), `cap_timeout_seconds`, and `continue_as_new_threshold`.
  - **Responsibilities:**
    - Coordinates batch execution of all package tasks.
    - Manages global spend gate: checks whether projected spend fits within `cap_usd`.
    - Parks execution upon `CapReached`, waiting for `raise_cap` signal or timeout.
    - If timeout fires without a signal, finalizes execution and emits a partial manifest ($k/N$).
    - Supports interactive `pause` / `resume` and `cancel_package` signals.
    - Periodically executes `workflow.continue_as_new` to compact event history when processing large batches (e.g. 500+ cells).
- **`CertifyPackageWorkflow`**:
  - **Inputs:** `PackageSpec`, package-specific options, shared results path.
  - **Responsibilities:**
    - Runs the package certification sequence in strict topological order.
    - Executes test synthesis on the synthesizer route.
    - Schedules cell evaluation activities on the Modal route.
    - Computes 6-gate structural and held-out certification verdicts based on evaluated cells.
    - Emits structured `PackageCertificationResult`.

### 2.2 Activity Definitions
All non-deterministic operations and I/O live exclusively in activities:
1. **`generate_mutants(pkg: PackageSpec) -> list[MutantSummary]`**:
   - Parses repository AST, applies STING mutation operators (`boundary_flip`, `equality_flip`, `guard_drop`, etc.).
   - Deterministic output pinned by package content hash.
2. **`synthesize_tests(pkg: PackageSpec, train_mutants: list[MutantSummary]) -> SynthesisResult`**:
   - Calls LLM (e.g. Gemini 3.8 Flash via OMP headless CLI) with train mutants only (strictly blind to eval mutants).
   - Validates syntax and unittest conformance (`validate_contract_test`).
3. **`run_cell(spec: CellSpec, config: CellConfig) -> CellResult`**:
   - Content-addressed idempotency: checks `<results_dir>/<cell_key>/result.json` first.
   - If found: returns cached result immediately (`cache_hit=True`, $0.00 spend).
   - If not found: acquires Modal sandbox, executes candidate container, measures runtime seconds, writes `result.json` atomically, returns `CellResult`.
4. **`settle_spend(req: SettleSpendRequest) -> SpendSummary`**:
   - Reconciles conservative reservation with measured sandbox seconds.
   - Flushes transaction to durable JSON ledger under file lock.
5. **`write_manifest(manifest: CorpusManifest, output_dir: str) -> str`**:
   - Emits finalized `gold-v1-manifest.json` and summary metrics.

### 2.3 Error Classes & Retry Policies
- **`InfraError`**:
  - Transient network blips, Modal sandbox provisioning timeouts, 502/503 errors.
  - Handled by Temporal `RetryPolicy`: initial interval 1.0s, backoff coefficient 2.0, maximum attempts 4.
- **`CapReached`**:
  - Budget ceiling hit. Handled at workflow level (NOT retried as activity).
  - Workflow parks with `workflow.wait_condition(..., timeout=...)`.
  - Signal `raise_cap` unblocks; expiration transitions workflow to partial manifest emission.
- **`SubmissionFailure`**:
  - Real program execution outcome (exit code $\ne 0$, test failure, syntax error in submission).
  - Evaluated candidate result; **non-retryable**. Activity returns clean `CellResult` with `exit_code != 0`.

---

## 3. What Must Stay Scripts vs What Moves to Workflows

A common anti-pattern in workflow adoption is wrapping every trivial script in a heavy workflow. We establish a clear architectural boundary:

| Pipeline Component | Recommended Form | Rationale |
| :--- | :---: | :--- |
| **AST Mutation Parsing (`evallab tasks anatomy`)** | **Script / Activity** | Pure CPU AST traversal (<15ms). Trivial local script; run as an activity within a workflow, never a workflow itself. |
| **Single Throwaway Cell Debugger** | **Script** | Interactive developer diagnosis. Launching a full Temporal workflow for a 1-off local debug run adds latency and ceremony. |
| **Pre-Registration Generation (`prereg.md`)** | **Script** | Fast static file generation and SHA-256 calculation prior to campaign launch. |
| **Corpus-Scale Sweep (A2 / A4 / W3B)** | **Temporal Workflow** | Multi-hour, distributed, hundreds of paid cells, spend caps, high cost of crash, needs resume and live query board. |
| **Behavioral Test Synthesizer Batch** | **Temporal Workflow** | Parallel LLM calls with rate limits, retries, and spend tracking. |
| **Nightly Certification Cron** | **Temporal Workflow** | Scheduled recurring workflow execution with built-in retries, alerting, and history auditing. |

---

## 4. Migration Path: From Ad-Hoc Scripts to Temporal

To guarantee zero regression during overnight operations, we propose a four-phase migration path:

```
Phase 0: Prototype & Test Skeleton (F5)
  ├── Fake activities, deterministic test harness
  └── Verifies idempotency, cap signals, concurrency, and history limits
         │
         ▼
Phase 1: Real Activity Adapters
  ├── Implement real adapters wrapping evallab.modal_cells and evallab.gold_certifier
  └── Test against single DEV task in local test environment
         │
         ▼
Phase 2: Shadow Mode Execution
  ├── Run Temporal workflow in parallel with legacy run_gold_batch.py on 5 DEV packages
  ├── Compare output manifests: assert identical cell keys, identical verdicts
  └── Measure overhead, throughput, and memory consumption
         │
         ▼
Phase 3: Primary Runner with Script Fallback
  ├── Temporal becomes default CLI runner (evallab certify-gold --workflow)
  └── Operator dashboard monitors live status via workflow queries
         │
         ▼
Phase 4: Full Cutover & Script Retirement
  └── Remove legacy threading/file-lock resume loops from evallab.gold_certifier
```

### Shadow Mode Validation Criteria
During Phase 2, shadow mode must satisfy:
1. **Key Determinism:** Every `cell_key` generated by the workflow matches the script's `cell_key` for identical package commits.
2. **Verdict Parity:** $100\%$ agreement on `gold_structural` and `gold_heldout` classifications across the DEV cohort.
3. **Spend Equivalence:** Settled spend matches within $\pm 0.5\%$ (variance only due to minor cloud sandbox container startup delta).

---

## 5. Honest Costs & Operational Tradeoffs

Adopting Temporal brings immense durability advantages, but introduces specific operational costs that must be managed honestly:

### 5.1 Infrastructure Footprint
- **Server Dependency:** Temporal requires an orchestration server and persistence backend (PostgreSQL, SQLite, or Cassandra). For local single-machine evaluation, the embedded dev server (`WorkflowEnvironment.start_local` / temporal CLI) has zero external dependencies, but production multi-host squads require a managed server cluster.
- **Worker Daemons:** Workers must be running and polling task queues. If worker processes die, workflows stall until workers are restarted (though no state is lost).

### 5.2 Developer Learning Curve & Sandbox Constraints
- **Determinism Rules:** Workflow code executes inside a strict sandbox. Standard Python primitives (`datetime.now()`, `time.time()`, `random.random()`, `os.environ`, file I/O, `asyncio.sleep`) are forbidden in workflow logic and must use `workflow.now()`, `workflow.wait()`, or activities. Violations result in `WorkflowDeterminismError` during replay.
- **Activity Heartbeating:** Long-running activities (such as a 3-minute Modal sandbox cell) must send periodic heartbeats (`activity.heartbeat(...)`), otherwise Temporal may assume the worker died and issue redundant activity retries.

### 5.3 Workflow Versioning & In-Flight Migrations
- Once a workflow starts, its event history must replay against compatible code. If workflow definitions are modified while multi-hour certification batches are active, replays can break without explicit versioning.
- Mitigation:
  1. Utilize `workflow.patched("fix-spend-gate-v2")` branching for in-place bug fixes.
  2. Maintain isolated task queue names for major workflow breaking changes (`certify-gold-v2`).
  3. Short-lived child workflows (per package) ensure that in-flight batches clear quickly without requiring indefinite backward compatibility.

### 5.4 Continue-As-New for Large Corpora
- In Temporal, every workflow event is appended to an immutable history. If a single workflow processes 1,000 cells directly, its event history can exceed Temporal's recommended limit (10,000 events) or hard limit (50,000 events).
- Solution: The `CertifyCorpusWorkflow` implements automated `continue_as_new` after every batch of packages (or every $N$ cells), serializing accumulated results, settled spend, and coverage into the next generation. This keeps each workflow generation's history compact (< 100 events).

---

## 6. Conclusion

The `CertifyGold` durable workflow bridges the gap between fragile one-off research scripts and production-grade evaluation infrastructure. By isolating sandboxed Modal cells into idempotent activities, exposing dynamic spend-approval signals, and enforcing bounded per-route concurrency, the system guarantees that overnight certification campaigns run reliably to completion with zero duplicate cloud spend.
