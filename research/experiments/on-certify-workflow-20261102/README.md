> VERIFIED 2026-11-02: some numbers below were corrected by an independent verifier; read VERIFIER-NOTES.md first.

# CertifyGold Durable Temporal Workflow Prototype (`F5-CERTIFY-WF`)

**Workflow ID:** `WF-03` / `F5-CERTIFY-WF`  
**Location:** `research/experiments/on-certify-workflow-20261102/`  
**Target:** Durable replacement for ad-hoc certification scripts (`evallab certify-gold`, `run_gold_certification.py`, `run_gold_batch.py`)  

---

## 1. Overview

Corpus-scale certification evaluates hundreds of paid, sandboxed cells across dozens of task packages under tight budget ceilings. In previous campaigns (e.g. `W3B-EXHAUSTIVE-GOLD-CERTIFIER`, `A2-AUDIT-SWEEP`, `A4-GOLD-BATCH`), batch runs were driven by custom Python scripts with thread pools and ad-hoc disk markers. These scripts suffered from:
1. **Worker crash vulnerability:** Mid-run kills or host resets lost memory state, requiring manual restarts and risking duplicate cell executions.
2. **Premature spend gate exhaustion:** Upfront reservations without dynamic signal unblocking halted runs before actual caps were reached.
3. **Lack of dynamic approvals:** When caps were hit, scripts halted completely; resuming required code edits and CLI restarts.
4. **Unbounded event history:** Naive batch workflows accumulate thousands of events, threatening workflow replay limits.

This prototype implements `CertifyGold` as a durable Temporal workflow:
- **`CertifyCorpusWorkflow`**: Coordinates the corpus-level batch, tracks cumulative metered spend, parks upon `CapReached` waiting for `raise_cap` signal or timeout, manages route concurrency, and periodically invokes `continue_as_new` to bound history size.
- **`CertifyPackageWorkflow`**: Child workflow per package coordinating AST mutation generation, LLM test synthesis, 6-gate sandbox cell execution, and spend settlement.
- **Content-Addressed Idempotent Cells**: Every evaluation cell is keyed deterministically (`cell_key = sha256(...)`). Completed cells return immediately from disk cache (`cache_hit=True`, $0.00 spend).
- **Injectable Fake Activities**: All activities are deterministic stubs with injectable costs and failure modes: no Modal cloud dependencies, no model API calls, zero financial spend during tests.

---

## 2. Architecture & Pipeline Mapping

```
┌────────────────────────────────────────────────────────────────────────┐
│                        CertifyCorpusWorkflow                           │
│  - Dynamic Spend Gate (cap_usd, cumulative_spend, park on CapReached)  │
│  - Per-Route Concurrency Limits (Modal route, Synthesizer route)       │
│  - Signals: raise_cap(usd, approver), pause(bool), cancel_package(id)  │
│  - Query: board() -> CorpusBoard                                       │
│  - Continue-As-New (every 50 cells to keep history bounded)            │
└───────────────────┬────────────────────────────────────────────────────┘
                    │ spawns child workflows
                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        CertifyPackageWorkflow                          │
│  1. Activity: generate_mutants(pkg)                                    │
│  2. Deterministic Partition: train_mutants vs eval_mutants             │
│  3. Activity: synthesize_tests(pkg, train_mutants) [Route: Synth]     │
│  4. Cell Execution Loop: [Route: Modal]                                │
│     ├── reference cell       ──► run_cell(cell_key)                    │
│     ├── nop cell             ──► run_cell(cell_key)                    │
│     ├── witness / conftest   ──► run_cell(cell_key)                    │
│     ├── exit0 / .pth tamper  ──► run_cell(cell_key)                    │
│     ├── train mutant cells   ──► run_cell(cell_key) [N cells]          │
│     ├── eval mutant cells    ──► run_cell(cell_key) [M cells]          │
│     └── eval base mutants    ──► run_cell(cell_key) [M cells]          │
│  5. Activity: settle_spend(pkg_id, reserved, actual)                   │
│  6. Verdict Synthesis: gold_structural, gold_heldout, not_gold         │
└───────────────────┬────────────────────────────────────────────────────┘
                    │ returns PackageCertificationResult
                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Activity: write_manifest(manifest, output_dir)                        │
│  - Emits gold-v1-manifest.json with coverage k/N and metrics           │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Signals & Queries

### Signals
- **`raise_cap(usd: float, approver: str)`**: Increases the available spend cap and automatically unparks workflows parked on `CapReached`.
- **`pause(paused: bool)`**: Pauses or unpauses package dispatch.
- **`cancel_package(package_id: str)`**: Cancels pending or running child workflows for a specific package.

### Queries
- **`board() -> CorpusBoard`**: Returns live status without disk contention:
  - `total_packages`, `completed_packages`, `failed_packages`
  - `running_packages`, `package_statuses`
  - `cumulative_spend`, `cap_usd`
  - `coverage_k`, `coverage_n`, `is_partial`, `manifest_written`

---

## 4. Error Classes & Retry Policies

- **`InfraError`**:
  - Transient infrastructure failures (Modal network blip, container startup timeout).
  - Retried automatically via Temporal `RetryPolicy`: exponential backoff (initial 100ms, backoff factor 2.0, max attempts 4).
- **`CapReached`**:
  - Spend limit reached. Workflow parks safely on `workflow.wait_condition` waiting for `raise_cap` signal.
  - If approval times out, workflow finalizes and writes a **PARTIAL manifest** with coverage $k/N$.
- **`SubmissionFailure`**:
  - Evaluated candidate failure (exit code $\ne 0$, test failure within container).
  - Treated as a valid evaluation result, NOT an infrastructure error. Non-retryable.

---

## 5. Running Tests in a Clean Virtual Environment

To run the complete test suite from scratch:

```bash
# 1. Navigate to the experiment directory
cd research/experiments/on-certify-workflow-20261102

# 2. Run test suite using uv with dev extras
uv run --extra dev pytest -v
```

### Verified Test Suite (7/7 Passed):
| Test File | Verification Invariant | Status |
| :--- | :--- | :---: |
| `tests/test_durability.py` | Exactly-once cell execution under abrupt `kill -9` worker termination mid-execution | **PASSED** |
| `tests/test_cap_signal.py` | Spend cap reached, workflow parks, resumes after `raise_cap` signal, completes full manifest | **PASSED** |
| `tests/test_cap_timeout.py` | Spend cap reached, approval timeout fires, emits PARTIAL manifest with coverage $k/N$ | **PASSED** |
| `tests/test_concurrency.py` | Strict per-route concurrency enforcement (Modal limit $\le 2$, Synth limit $\le 1$) | **PASSED** |
| `tests/test_continue_as_new.py` | Processes 500 cells across 10 `continue_as_new` generations with bounded history (< 150 events) | **PASSED** |
| `tests/test_signals_and_failures.py` | Automatic `InfraError` backoff retry, non-retryable `SubmissionFailure`, and `pause`/`cancel` signals | **PASSED** |

---

## 6. Design Document

Detailed architectural mapping, scripts vs workflows demarcation, migration path (shadow mode), and honest operational costs are documented in [`DESIGN.md`](./DESIGN.md).
