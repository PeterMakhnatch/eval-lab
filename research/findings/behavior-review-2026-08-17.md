# Behavioral Review 2026-08-17

**Scope**: 92 trials from the trial corpus accessed exclusively through the unified attach surface (`evallab db attach` / `evallab.attach.attach()`, `trial_facts`, `steps`, and `tool_calls` tables) as of commit `61f5701`. The corpus spans 72 jobs from 2026-08-14 to 2026-08-16, comprising 76 measured trials (68 passed, 8 scored zero) and 16 never-measured trials (harness exceptions).

---

## 1. Effort vs Outcome Across the Corpus

### Finding: Effort Does Not Correlate Positively with Success

Across the 92-trial corpus, agent effort (measured by step count, tool invocations, LLM calls, and execution wall-clock time) exhibits **no positive correlation with evaluation success**.

1. **Overall Corpus Summary**:
   - **Passed trials (n=68)**: Mean step count = 0.96 (dominated by 56 single-shot oracle control passes with 0 steps; for codex passed trials n=11, mean step count = 10.36 [95% CI: 9.8, 10.9]).
   - **Scored-zero trials (n=8)**: Mean step count = 13.62 (comprising 2 nop zero-effort failures with 0 steps, and 6 codex failures with mean step count = 18.17 [95% CI: 16.0, 20.3]).
   - **Never-measured trials (n=16)**: Mean step count = 1.38 (10 setup failures with 0 steps; 6 execution crashes with mean 2.33 steps).

2. **Codex Sub-Corpus Analysis (n=17 measured trials)**:
   Restricting to the `codex` agent (`gpt-5.6-terra`) where trajectory telemetry is recorded:
   - **Passed cohort (n=11)**:
     - `local-lab/event-summary` (n=6): 10.83 steps [95% CI: 10.0, 11.5], 4.33 tool calls, 28.82s execution time.
     - `petermakhnatch/transaction-reconciliation` (n=5): 9.80 steps [95% CI: 9.2, 10.4], 3.00 tool calls, 1485.99s execution time.
     - Combined passed codex mean: 10.36 steps [95% CI: 9.8, 10.9].
   - **Scored-zero cohort (n=6)**:
     - `terminal-bench/html-js-filter` (n=6): 18.17 steps [95% CI: 16.0, 20.3], 11.67 tool calls, 538.22s execution time.
   - **Statistical comparison**:
     - Difference in means: +7.81 steps for failing trials.
     - Bootstrap 95% confidence intervals ([9.8, 10.9] vs [16.0, 20.3]) are strictly non-overlapping.
     - Verdict: **Distinguishable difference in step count**, with failing trials expending significantly *more* steps than successful trials.

3. **Generalization and Sample Power**:
   Across tasks, the relationship between effort and outcome is heavily confounded by task difficulty and environment requirements. With only 3 tasks in the current corpus, an omnibus correlation between effort and pass rate across tasks is **not distinguishable** and would be methodologically unsound to assert.
   - **Sample required to settle**: Establishing whether higher effort predicts success across diverse domains requires evaluating at least **15+ distinct task families** with both passing and failing trials per task under matched agent configurations.

---

## 2. Anatomical Comparison: `html-js-filter` Failures vs `event-summary` Successes

Beyond the raw pass/fail scores (0.0 vs 1.0), the behavioral telemetry reveals fundamentally different execution dynamics between the failing `html-js-filter` trials and the passing `event-summary` trials for the `codex` agent:

| Behavioral Dimension | `terminal-bench/html-js-filter` (Scored Zero, n=6) | `local-lab/event-summary` (Passed, n=6) | Ratio / Difference |
|---|---|---|---|
| **Primary Reward** | 0.00 | 1.00 | Fail vs Pass |
| **Mean Step Count** | 18.17 [95% CI: 16.0, 20.3] | 10.83 [95% CI: 10.0, 11.5] | 1.68× steps |
| **Mean Tool Calls (`exec`)** | 11.67 [95% CI: 10.2, 13.0] | 4.33 [95% CI: 3.8, 5.0] | 2.69× tool calls |
| **Mean LLM Calls** | 12.67 [95% CI: 11.3, 14.0] | 5.33 [95% CI: 4.8, 6.0] | 2.38× LLM calls |
| **Agent Execution Seconds** | 538.22s (~9.0 min) | 28.82s (~0.5 min) | 18.67× wall-clock |
| **Total Duration Seconds** | 869.86s (~14.5 min) | 99.20s (~1.7 min) | 8.77× duration |
| **Total Steps Recorded** | 109 steps (across 6 trials) | 65 steps (across 6 trials) | 1.68× steps |
| **Trajectory Source: Agent** | 76 steps (**69.7%**) | 32 steps (**49.2%**) | +20.5 percentage points |
| **Trajectory Source: System** | 21 steps (**19.3%**) | 21 steps (**32.3%**) | -13.0 percentage points |
| **Trajectory Source: User** | 12 steps (**11.0%**) | 12 steps (**18.5%**) | -7.5 percentage points |
| **Avg Input Tokens** | 289,624 | 76,816 | 3.77× tokens |
| **Avg Cache Tokens** | 262,144 | 69,461 | 3.77× cache |
| **Avg Output Tokens** | 11,645 | 777 | **14.99× output tokens** |
| **Mean Cost per Trial** | $0.2471 | $0.0379 | 6.52× cost |
| **Efficiency: Steps / Reward** | **Undefined** (reward 0) | **10.83** steps / point | Valid vs Undefined |
| **Efficiency: Seconds / Step** | 29.63 s/step | 2.66 s/step | 11.14× latency/step |

### Key Analytical Takeaways

1. **Active Struggle vs Early Surrender**:
   `html-js-filter` is not failing due to immediate tool refusal, syntax errors, or early exit. The agent engages in extended problem-solving: 18 steps, 12 tool executions, 9 minutes of execution, and nearly 12,000 generated output tokens per trial. It is an agent actively working, attempting multiple script filters, and failing the verifier.
2. **Trajectory Density**:
   Agent turns constitute nearly 70% of the entire trajectory on `html-js-filter`, compared to under 50% on `event-summary`. The conversational turns reflect repeated inspection and modification attempts in the container.
3. **Token & Generation Cost**:
   The output generation on `html-js-filter` is 15× larger (11.6k vs 777 tokens), driving trial cost to $0.25 vs $0.04.

---

## 3. Behavioral Telemetry Column Inventory

The `trial_facts` view presents 34 total columns describing evaluation trials. Below is a structured audit of their discriminative utility, sparsity, and reliability across the 92-trial corpus:

### Category A: High-Information / Discriminative (10 Columns)
*Columns that vary meaningfully across trials and reliably discriminate agent behavior, outcome, or execution shape:*

1. `task_name` (92/92 populated, 3 distinct): Discriminates evaluation problems (`local-lab/event-summary`, `petermakhnatch/transaction-reconciliation`, `terminal-bench/html-js-filter`).
2. `agent_name` (92/92 populated, 3 distinct): Distinguishes evaluated agents (`codex`, `oracle`, `nop`).
3. `primary_reward` (82/92 populated, 2 distinct values: 0.0, 1.0; 10 null on harness exceptions): Primary evaluation metric.
4. `exception_class` (16/92 populated, 2 distinct classes: `ValueError` n=9, `NonZeroAgentExitCodeError` n=7): Categorizes harness breakdown.
5. `duration_seconds` (92/92 populated): Total trial wall-clock (ranges from 1.0s to 3,143.7s).
6. `agent_execution_seconds` (91/92 populated, 1 null): Agent runtime excluding harness/verifier setup. Distinguishes fast single-turn executions (0.2s) from long-running agent workflows (1,486s).
7. `step_count` (92/92 populated, range 0 to 25): Trajectory length; separates single-shot tasks from multi-step interactive workflows.
8. `tool_call_count` (92/92 populated, range 0 to 14): Quantifies tool invocation frequency.
9. `llm_call_count` (92/92 populated, range 0 to 15): Correlates with step iterations.
10. `model_name` (24/92 populated): Identifies model (`gpt-5.6-terra`) for canary agent runs.

### Category B: Sparse Token Economics (4 Columns)
*Columns populated for a subset of trials (17 of 92 trials, 18.5%). Highly informative for the populated canary runs, but must never be averaged silently over the corpus:*

11. `input_tokens` (17/92 populated, mean 146,846 tokens): Ranging from 56.6k (`transaction-reconciliation`) to 289.6k (`html-js-filter`).
12. `cache_tokens` (17/92 populated, mean 132,499 tokens): Prompt caching utilization.
13. `output_tokens` (17/92 populated, mean 4,496 tokens): Generated tokens; distinguishes concise completions from massive multi-turn code generation.
14. `cost_usd` (17/92 populated, mean $0.1085): Direct inference cost; ranges from $0.025 to $0.305.

### Category C: Zero / Unpopulated Instrumentation Signals (4 Columns)
*Columns present in the schema but uniformly 0 across all 92 trials. These reflect unpopulated telemetry parsers rather than agent capabilities:*

15. `repeated_failed_command_count` (92/92 populated, **100% are 0**): The ATIF loop detector has not ingested repeated failing bash commands into this column.
16. `command_failure_count` (92/92 populated, **100% are 0**): Non-zero shell exit codes within trajectories are not yet mapped to this column.
17. `invalid_trajectory_count` (92/92 populated, **100% are 0**): All ingested ATIF trajectory documents passed schema validation.
18. `missing_artifact_count` (92/92 populated, 91 are 0, 1 is 1): Artifact existence check is clean across almost all trials.

### Category D: Invariant / Low-Variance Metadata (16 Columns)
*Identifiers, digests, or timing sub-components with minimal discriminative variation:*

19. `environment_digest` (92/92 populated, 1 distinct value): Single environment base image across all runs.
20. `agent_config_digest` (92/92 populated, 4 distinct values): Tracks agent configurations.
21. `verifier_digest` (92/92 populated, 6 distinct values): Tracks verifier revisions.
22. `task_digest` (92/92 populated, 4 distinct values): Task definition hash.
23. `artifact_set_digest` (92/92 populated, 13 distinct values): Artifact manifest hash.
24. `trajectory_count` (92/92 populated, values 0 or 1): 0 for smoke/control, 1 for ATIF-instrumented runs.
25. `artifact_count` (92/92 populated, values 2 to 3): Constant artifact emission.
26. `environment_setup_seconds` (92/92 populated, mean 2.8s): Docker container launch overhead.
27. `agent_setup_seconds` (92/92 populated, mean 11.2s): Agent initialization overhead.
28. `verifier_seconds` (82/92 populated, mean 15.6s): Verifier evaluation time.
29. `exception_phase` (16/92 populated, 100% `'unknown'`): Phase classification unpopulated during exception capture.
30. `agent_version` (92/92 populated, 3 distinct values: `1.0.0`, `0.147.0`).
31-34. `experiment_id`, `job_id`, `trial_id`, `job_name`, `trial_name`: Primary keys and lineage identifiers.

---

## 4. Summary Table of Behavioral Metrics by Task & Outcome

Accessed from `v_behavior_effort_by_outcome`, `v_behavior_efficiency`, and `v_behavior_token_economics`:

| Task Name | Agent | Outcome | n | Avg Steps [95% CI] | Avg Tools | Avg Exec (s) | Seconds / Step | Steps / Reward Point | Token Coverage | Avg Cost |
|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|
| `local-lab/event-summary` | `codex` | `never_measured` | 3 | 0.0 | 0.0 | 0.0 | - | undefined | 0 of 3 (0%) | - |
| `local-lab/event-summary` | `codex` | `passed` | 6 | 10.8 [10.0, 11.5] | 4.3 | 28.8 | 2.66 | 10.83 | 6 of 6 (100%) | $0.0379 |
| `local-lab/event-summary` | `nop` | `scored_zero` | 2 | 0.0 | 0.0 | 0.0 | - | undefined | 0 of 2 (0%) | - |
| `local-lab/event-summary` | `oracle` | `passed` | 56 | 0.0 [0.0, 0.0] | 0.0 | 0.3 | - | 0.00 | 0 of 56 (0%) | - |
| `petermakhnatch/transaction-reconciliation` | `codex` | `never_measured` | 7 | 2.1 [0.7, 4.3] | 0.0 | 9.0 | 3.60 | undefined | 0 of 7 (0%) | - |
| `petermakhnatch/transaction-reconciliation` | `codex` | `passed` | 5 | 9.8 [9.2, 10.4] | 3.0 | 1486.0 | 151.63 | 9.80 | 5 of 5 (100%) | $0.0255 |
| `petermakhnatch/transaction-reconciliation` | `oracle` | `passed` | 1 | 0.0 | 0.0 | 0.2 | - | 0.00 | 0 of 1 (0%) | - |
| `terminal-bench/html-js-filter` | `codex` | `never_measured` | 6 | 2.5 [0.8, 4.2] | 0.0 | 8.8 | 3.54 | undefined | 0 of 6 (0%) | - |
| `terminal-bench/html-js-filter` | `codex` | `scored_zero` | 6 | 18.2 [16.0, 20.3] | 11.7 | 538.2 | 29.63 | undefined | 6 of 6 (100%) | $0.2471 |
