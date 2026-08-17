# Evidence Review 2026-08-17

Scope: 92 trials from the derived Parquet projection (`derived/parquet/` via `evallab.paths.derived_root_from_environment`), as of commit `331f4ba`, compared against 11 trials in the promoted evidence store (`research/evidence/runs/`).

## Corpus Scope and Store Discrepancy

The evaluation repository maintains two distinct evidence stores:
1. **Derived Parquet Projection (`derived/parquet/`)**: 92 trials across 72 jobs spanning 2026-08-14 to 2026-08-16. This projection records the complete experimental record: 76 measured trials and 16 never-measured trials (harness exceptions).
2. **Promoted Evidence Store (`research/evidence/runs/`)**: 11 trials across 5 curated job bundles (3 canary jobs from 2026-08-15 with 3 trials each, plus 2 control jobs from 2026-08-14 with 1 trial each). Contains 0 harness exceptions.

**Disagreement as a Finding**: The promoted evidence store exhibits survivor bias. By retaining only clean, post-fix runs from 2026-08-15, it completely omits the setup failures (`ValueError`, n=9) and container crashes (`NonZeroAgentExitCodeError`, n=6) from 2026-08-14, the 2026-08-16 canary runs (n=9, including 1 exception), and 49+ oracle smoke jobs. Analyzing only the promoted store creates an artificially restricted sample (n=3 per canary) that mischaracterizes powered failure findings as "underpowered" and hides significant harness fragility.

## terminal-bench/html-js-filter Record

`terminal-bench/html-js-filter` accounts for 12 total trials across 4 jobs, executed with `codex` (version `0.147.0`):
- **Never-measured cohort (6 trials, harness exceptions)**:
  - 3 trials on 2026-08-14 (`canary-terminal-bench-html-js-filter-codex-20260814`): threw `ValueError` during initialization/setup (phase `unknown`, `model_name=null`).
  - 3 trials on 2026-08-14 (`canary-terminal-bench-html-js-filter-codex-20260814-r2`): threw `NonZeroAgentExitCodeError` during execution (phase `unknown`, model `gpt-5.6-terra`).
- **Measured cohort (6 trials, scored runs)**:
  - 3 trials on 2026-08-15 (`canary-terminal-bench-html-js-filter-codex-20260815`): all scored `primary_reward=0.0` (passes=0, scored_failures=3, model `gpt-5.6-terra`).
  - 3 trials on 2026-08-16 (`canary-terminal-bench-html-js-filter-codex-20260816`): all scored `primary_reward=0.0` (passes=0, scored_failures=3, model `gpt-5.6-terra`).
- **Reward distribution**: 6 trials measured, all 6 scored `primary_reward=0.0` (0 passes, 6 scored failures).
- **Statistical evaluation**: Wilson 95% confidence interval on 0/6 passes computed via `evallab.cohort.wilson_interval` is **[0.0%, 39.0%]** (`[0.000, 0.390]`).
- **Verdict and Power Inversion**: At measured n=6, this cohort meets the lab's power threshold (`n >= 5`, defined in `src/evallab/lessons.py`). This result is a **powered finding of failure** (pass rate upper bound 39.0% at 95% confidence), directly inverting the prior report of "underpowered / not yet distinguishable (n=3)".
- **Evidence boundary**:
  - *Supported*: The `codex` agent consistently fails when execution completes (0/6 passes across two distinct dates), and the task exhibited severe harness fragility on 2026-08-14 (6/12 trials unmeasured).
  - *Unsupported*: The current record cannot determine whether the 0.0 scores stem from genuine agent limitation on HTML/JS filtering or a defective task verifier/environment, because the corpus contains **zero oracle control trials** for `terminal-bench/html-js-filter`.
- **Cheapest diagnostic check**: Execute a single trial of `terminal-bench/html-js-filter` with the `oracle` agent (`evallab run --task terminal-bench/html-js-filter --agent oracle --name html-js-filter-oracle-control`). If oracle scores 1.0, the environment and verifier are valid and the codex failure is genuine; if oracle scores 0.0 or raises an exception, the task definition is broken. (Do not run without approval).

## Exception Taxonomy

The full 92-trial corpus contains 16 harness exceptions across two distinct exception classes, both recorded with phase `unknown`:

| Exception Class | n | Phase | Tasks Affected | Task Breakdown | Date Range | Notes |
|---|---|---|---|---|---|---|
| `ValueError` | 9 | unknown | 3 | `local-lab/event-summary`: 3<br>`petermakhnatch/transaction-reconciliation`: 3<br>`terminal-bench/html-js-filter`: 3 | 2026-08-14 | Shared harness initialization / parameter parsing defect in initial 2026-08-14 canary batch (`canary-*-20260814`); blocked all tasks equally before step execution. |
| `NonZeroAgentExitCodeError` | 7 | unknown | 2 | `petermakhnatch/transaction-reconciliation`: 4<br>`terminal-bench/html-js-filter`: 3 | 2026-08-14 to 2026-08-16 | Agent execution container crashed with non-zero exit code on 2026-08-14-r2 (3 for transaction-reconciliation, 3 for html-js-filter) and 2026-08-16 (1 for transaction-reconciliation). |

Total harness exceptions: 16/92 trials (17.39% exception rate across corpus).

## Oracle/Nop Control Record

- **Oracle Controls**: 57 trials across the corpus (56 for `local-lab/event-summary`, 1 for `petermakhnatch/transaction-reconciliation` in `brief07-transaction-oracle`).
  - All 57/57 scored `primary_reward=1.0` (100% pass rate) with 0 exceptions across all dates (2026-08-14 through 2026-08-16).
  - Includes 49 smoke runs, 5 pipeline/control runs on 2026-08-14, 2 promoted trials, and 1 transaction-reconciliation control.
- **Nop Controls**: 2 trials for `local-lab/event-summary` (`event-summary-nop-evidence`), both scoring `primary_reward=0.0` with 0 exceptions.
- **Control Gap**: `terminal-bench/html-js-filter` has zero oracle or nop controls in the entire 92-trial corpus. This absence is the primary obstacle to determining whether `html-js-filter` failures are harness/verifier bugs or agent capability limits.

## Corpus Temporal Comparability

- **2026-08-14 (Early Harness Instability)**: 20 trials total. 15 harness exceptions (9 `ValueError` in round 1 across all 3 canary tasks; 6 `NonZeroAgentExitCodeError` in round 2 on transaction-reconciliation and html-js-filter) and 5 oracle passes (1.0).
- **2026-08-15 (Stable Canary Baseline)**: 9 trials total (3 per canary task), 0 exceptions. `event-summary` 3/3 (100%), `transaction-reconciliation` 3/3 (100%), `html-js-filter` 0/3 (0%).
- **2026-08-16 (Replicated Canary Runs)**: 9 trials total, 1 exception. `event-summary` 3/3 (100%), `transaction-reconciliation` 2/2 measured passes (100%) + 1 `NonZeroAgentExitCodeError`, `html-js-filter` 0/3 (0%).
- **Control & Smoke Cluster**: 54 trials (2026-08-14 to 2026-08-16), 0 exceptions: 52 oracle passes (1.0) and 2 nop failures (0.0).
- **Temporal Stability**: For measured trials, agent performance is identical across 2026-08-15 and 2026-08-16: `event-summary` codex (6/6 = 100%), `transaction-reconciliation` codex (5/5 = 100%), `html-js-filter` codex (0/6 = 0%).

## Summary Counts from Evidence Views

### Task Summary (`v_task_summary`)

| Task Name | Total n | Never Measured | Measured | Passes | Scored Failures | Pass Rate (Measured) |
|---|---|---|---|---|---|---|
| `local-lab/event-summary` | 67 | 3 | 64 | 62 | 2 | 96.88% |
| `petermakhnatch/transaction-reconciliation` | 13 | 7 | 6 | 6 | 0 | 100.00% |
| `terminal-bench/html-js-filter` | 12 | 6 | 6 | 0 | 6 | 0.00% |
| **Total** | **92** | **16** | **76** | **68** | **8** | **89.47%** |

### Outcome by Task and Agent (`v_outcome_by_task_agent`)

| Task Name | Agent | Total n | Measured n | Unmeasured n | Passes | Pass Rate | Harness Exceptions | Scored Failures | Wilson 95% CI (Measured) |
|---|---|---|---|---|---|---|---|---|---|
| `local-lab/event-summary` | `oracle` | 56 | 56 | 0 | 56 | 100.0% | 0 | 0 | [93.6%, 100.0%] (powered) |
| `petermakhnatch/transaction-reconciliation` | `codex` | 12 | 5 | 7 | 5 | 100.0% | 7 | 0 | [56.6%, 100.0%] (powered) |
| `terminal-bench/html-js-filter` | `codex` | 12 | 6 | 6 | 0 | 0.0% | 6 | 6 | [0.0%, 39.0%] (powered) |
| `local-lab/event-summary` | `codex` | 9 | 6 | 3 | 6 | 100.0% | 3 | 0 | [61.0%, 100.0%] (powered) |
| `local-lab/event-summary` | `nop` | 2 | 2 | 0 | 0 | 0.0% | 0 | 2 | [0.0%, 65.8%] (underpowered) |
| `petermakhnatch/transaction-reconciliation` | `oracle` | 1 | 1 | 0 | 1 | 100.0% | 0 | 0 | [20.7%, 100.0%] (underpowered) |

### Failure Classification (`v_failure_classification`)

| Failure Type | n | Distinct Tasks | Distinct Agents |
|---|---|---|---|
| `passed` | 68 | 2 | 2 |
| `harness_exception` | 16 | 3 | 1 |
| `scored_failure` | 8 | 2 | 2 |