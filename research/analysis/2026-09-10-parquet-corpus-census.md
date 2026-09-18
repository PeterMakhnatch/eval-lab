---
status: draft
audience:
  - analyst
  - operator
---

# Parquet corpus census — 2026-09-10

## Scope, provenance, and interpretation

This is a deterministic, read-only census of `derived/parquet/`, computed with the repository's Python environment and **DuckDB 1.5.5**. Snapshot fingerprint acquired at **2026-09-10T21:50:38.047867+00:00**. It follows [the analysis loop](../../docs/analysis-loop.md): retain denominators, distinguish unavailable evidence from failure, separate controls from model capability, and do not turn a heterogeneous corpus into a causal comparison.

**The principal cohort is the 465 distinct `trial_id` values in `trial_facts.parquet`, across 286 jobs and 128 task-name strings.** It includes successful, unsuccessful, exceptional, and partial-intake records—not 465 proven complete, valid model attempts. No task/verifier/image/version matching is imposed; all rates below are descriptive corpus bookkeeping, not a leaderboard. Repeated attempts are retained when their trial IDs differ. No source runs, Parquet files, or database rows were modified. No model calls or new trials were run.

The report is Stage 2–4 extraction and exploratory cross-corpus synthesis, **not a set of independently validated Stage 5 causal trial analyses**. Human review is pending. Primary records remain the Harbor job/trial evidence; this audit did not revalidate every raw artifact or verifier.

### Main findings

- **263/465 trials have `reward == 1.0` (56.56%); 161 have reward 0; 41 have no named reward.** Among the 424 reward-bearing trials, the pass fraction is **62.03%**.
- Action-memory is the largest family (**134**), followed by event-summary (**93**), the combined FuncDAG family (**85**), and Terminal-Bench (**49**).
- **63 trials have a recorded exception**, spanning seven distinct classes. Exception and non-pass are not interchangeable: trials can score zero without an exception, and unavailable reward is not a verifier failure.
- **245/465 trials have a projected trajectory**, accounting for **3,621 distinct steps**; their median is **6 steps**, mean **14.78**, maximum **272**. The remaining **220** lack trajectory data and must not be described as genuine zero-step attempts.
- Duration is present on **455/465** trials: average **160.348 seconds**, median **58.391 seconds**, p90 **277.063 seconds**. This is full trial wall time, not model latency.
- Compacted and uncompacted data overlap. **626 physical trial-fact rows collapse to 465 trials**; a recursive Parquet `COUNT(*)` overcounts by **161 rows (34.62% relative to the unique count)**.

## Data selection and integrity

### Deduplication

Read all `**/trial_facts.parquet` files using `union_by_name=true` and `hive_partitioning=false`. Keep one row per `trial_id`, preferring the uncompacted job partition over `compact/` and then the lexicographically first filename. This is an explicit deterministic census selection rule, not a claim that raw partitions are universally fresher.

There are 432 compacted rows and 194 uncompacted rows. Deduplication changes no task name, agent, model, primary reward, exception, step count, or duration: the metric-conflict check returned **zero** conflicting IDs. There are **36 IDs** with differences in `task_id`, `task_block_inputs_json`, and `task_block_id`; those fields are not used to infer comparability here. Distinct entire rows would still produce **501**, not 465, so `SELECT DISTINCT *` is insufficient.

Named rewards are taken from `reward_facts` with `reward_name='reward'`, deduplicated by `(trial_id, reward_value)`, and left-joined to the selected trial facts. No ID has conflicting values for that reward. **Do not average every reward dimension together.** Other dimensions include `correctness`, `input_preservation`, `output_hygiene`, `passed`, `score_sum`, and `num_questions`.

`trial_facts.primary_reward` is not perfectly equivalent to the named reward: **nine trials have null primary reward but named reward 0** (eight NOP FuncDAG trials and one Zai deepplanning trial). Thus primary reward alone yields 415 scored trials; the requested named-reward calculation yields 424. Pass numerators agree at 263. SQL below intentionally uses the named reward.

### Coverage and feature-table caveats

The independent feature table has **491 physical rows but only 309 distinct trial IDs**:

| status | row_count | trial_ids | job_trial_ids |
| --- | --- | --- | --- |
| featured | 358 | 185 | 185 |
| accounted_unavailable | 133 | 124 | 127 |

There are **264 overlapping IDs**, **201 fact-only IDs**, and **45 feature-only IDs**. The latter comprise seven named-task records plus 38 IDs with `task_name='unknown'` (41 physical rows). Therefore neither 491 feature rows nor a blind union is a defensible trial denominator. A feature-table behavioral rate would require `status='featured'` and deduplication; this report instead uses trial facts for the inventory and trajectory projection for steps. On shared featured IDs the recorded step counts agree with trial facts (zero mismatches).

The seven named-task feature-only records are listed explicitly below; they are **not silently added** to the 465-trial principal cohort. They require intake reconciliation, not exclusion from future research. Tau3 remains distinct from Terminal-Bench.

| trial_id | job_name | task_name | agent_name | model_name | primary_reward | status |
| --- | --- | --- | --- | --- | --- | --- |
| 787bf10c-60bc-44b2-901b-0a2d76bd162c | tau3-retail-1-nop-evidence | sierra-research/tau3-bench__tau3-retail-1 | nop | unknown | 0.0 | accounted_unavailable |
| 9000410f-335f-43e2-b428-5168560b0465 | tau3-retail-1-oracle-evidence | sierra-research/tau3-bench__tau3-retail-1 | oracle | unknown | 1.0 | accounted_unavailable |
| 1f52357c-0c7e-4b50-a2fd-ad08b73f4f3f | funcdag-codex-canary | evallab/syn-funcdag-easy | codex | gpt-5.6-terra | — | featured |
| 8c658358-475d-418f-9c7f-e13658d02af7 | funcdag-codex-canary | evallab/syn-funcdag-easy | codex | unknown | — | accounted_unavailable |
| d50dc421-93cc-48a8-9a1e-cff895838cec | funcdag-codex-canary | evallab/syn-funcdag-hard | codex | gpt-5.6-terra | 0.0 | featured |
| 86eb12d5-08eb-4fdf-9343-7a4e327872d4 | funcdag-codex-canary | evallab/syn-funcdag-medium | codex | gpt-5.6-terra | 1.0 | featured |
| 72d21d01-8cf1-4c86-afc9-399d5398b854 | funcdag-codex-canary | evallab/syn-funcdag-easy | codex | gpt-5.6-terra | 1.0 | featured |

## Trial counts by task family

Family is a census grouping derived from task names because `task_family` is null for **422/465 trials**; the only populated raw families are `family_b_funcdag_v2` (33) and `travel-planning` (10). The complete mapping is executable SQL below. It is a grouping convention, not proof of shared task/verifier versions.

| family | trials | passes | missing_rewards | exceptions |
| --- | --- | --- | --- | --- |
| action-memory | 134 | 74 | 0 | 2 |
| event-summary | 93 | 76 | 4 | 11 |
| mcp-funcdag (incl. syn-funcdag) | 85 | 49 | 11 | 17 |
| terminal-bench | 49 | 9 | 8 | 12 |
| transaction-reconciliation | 17 | 9 | 4 | 7 |
| deepplanning | 15 | 8 | 0 | 0 |
| mcp-recovery | 15 | 14 | 0 | 0 |
| seqgen | 14 | 7 | 2 | 2 |
| taskworld (tw) | 9 | 6 | 3 | 3 |
| facet | 7 | 2 | 1 | 1 |
| factory-development | 6 | 2 | 0 | 0 |
| agentabstain | 5 | 1 | 4 | 4 |
| rc-004-faulty-dependency | 5 | 2 | 1 | 1 |
| factory-runtime-generation | 4 | 3 | 0 | 0 |
| gaia2 | 4 | 0 | 2 | 2 |
| locomo | 2 | 1 | 0 | 0 |
| loca-bench | 1 | 0 | 1 | 1 |

FuncDAG combines 11 namespaced `mcp-funcdag` trials, 66 `syn-funcdag` trials, and eight baseline/screening/probe naming aliases. Terminal-Bench includes 45 namespaced trials plus four unnamespaced trials (`break-filter-js-from-html`, `gpt2-codegolf`, `llm-inference-batching-scheduler`, `query-optimize`), identifiable from their Terminal-Bench job names. Different Terminal-Bench versions are not asserted comparable. `tw_*` is grouped separately as the TaskWorld cohort; it is not Tau3. No task names remain unmapped.

## Pass rates by agent and model

`pass_pct_all = 100 × passes / trials` is the requested whole-cohort fraction: a missing reward contributes no pass but is **not relabeled reward 0**. `pass_pct_scored` is a sensitivity view over known named rewards only, not a capability estimate. Oracle and NOP are harness/task controls. These groups differ in task mix, versions, retry history, and evidence completeness.

| agent_group | trials | passes | scored | exceptions | pass_pct_all | pass_pct_scored |
| --- | --- | --- | --- | --- | --- | --- |
| Codex | 59 | 18 | 39 | 26 | 30.51 | 46.15 |
| Gemini | 25 | 9 | 25 | 4 | 36.0 | 36.0 |
| NOP | 36 | 0 | 34 | 2 | 0.0 | 0.0 |
| Oracle | 131 | 114 | 120 | 8 | 87.02 | 95.0 |
| Zai OpenCode | 214 | 122 | 206 | 23 | 57.01 | 59.22 |

Adapter normalization: `opencode` and `evallab.harbor_zai_opencode:SecretSafeZaiOpenCodeAgent` are grouped as Zai OpenCode; `antigravity-cli` as Gemini; `codex`, `oracle`, and `nop` retain their names. Raw model strings are kept distinct:

| agent_group | model | trials | passes | scored | pass_pct_all |
| --- | --- | --- | --- | --- | --- |
| Codex | [missing] | 9 | 0 | 0 | 0.0 |
| Codex | gpt-5.6-luna | 22 | 4 | 12 | 18.18 |
| Codex | gpt-5.6-terra | 28 | 14 | 27 | 50.0 |
| Gemini | gemini-3.7-flash | 1 | 0 | 1 | 0.0 |
| Gemini | gemini-3.7-flash-high | 4 | 4 | 4 | 100.0 |
| Gemini | gemini-3.7-flash-low | 16 | 3 | 16 | 18.75 |
| Gemini | gemini-3.7-flash-medium | 4 | 2 | 4 | 50.0 |
| NOP | [missing] | 36 | 0 | 34 | 0.0 |
| Oracle | [missing] | 131 | 114 | 120 | 87.02 |
| Zai OpenCode | glm-5.3 | 43 | 30 | 43 | 69.77 |
| Zai OpenCode | glm-5.3-flash | 164 | 92 | 163 | 56.1 |
| Zai OpenCode | zai-coding-plan/glm-5.3-flash | 7 | 0 | 0 | 0.0 |

The seven `zai-coding-plan/glm-5.3-flash` trials have no reward; their 0/7 entry is **not evidence of seven valid task failures**. This census does not diagnose a particular credential mismatch from an exception class alone.

### Task-mix visibility: passes / all trials

A dash means no trial, not a 0% pass rate.

| family | Zai OpenCode | Codex | Gemini | Oracle | NOP |
| --- | --- | --- | --- | --- | --- |
| action-memory | 72/130 | — | — | 2/2 | 0/2 |
| event-summary | 4/10 | 6/9 | 6/8 | 60/61 | 0/5 |
| mcp-funcdag (incl. syn-funcdag) | 27/49 | 3/3 | — | 19/21 | 0/12 |
| terminal-bench | — | 3/26 | 1/15 | 5/6 | 0/2 |
| transaction-reconciliation | — | 5/12 | 2/2 | 2/2 | 0/1 |
| deepplanning | 5/10 | 1/1 | — | 2/2 | 0/2 |
| mcp-recovery | 14/15 | — | — | — | — |
| seqgen | — | — | — | 7/8 | 0/6 |
| taskworld (tw) | — | — | — | 6/9 | — |
| facet | — | — | — | 2/6 | 0/1 |
| factory-development | — | — | — | 2/4 | 0/2 |
| agentabstain | — | 0/3 | — | 1/2 | — |
| rc-004-faulty-dependency | — | — | — | 2/3 | 0/2 |
| factory-runtime-generation | — | — | — | 3/4 | — |
| gaia2 | — | 0/4 | — | — | — |
| locomo | — | — | — | 1/1 | 0/1 |
| loca-bench | — | 0/1 | — | — | — |

## Step-count distributions

A step is an exported ATIF step, including system/user/agent entries; it is not necessarily an LLM call, tool call, or autonomous action. These summaries use `trial_facts.step_count` **only where `trajectory_count > 0`**. Distinct `(trial_id, document_id, step_id)` in the step projection confirms 3,621 steps across 245 trials, and there are 245 distinct trajectory documents. Missing trajectories stay outside the numerical distribution; controls have no projected trajectories here.

Quantiles use DuckDB `quantile_cont` (linear interpolation), so fractional quantiles are expected.

| agent_group | n | min | mean | p25 | median | p75 | p90 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ALL observed | 245 | 1 | 14.78 | 5.0 | 6.0 | 10.0 | 18.0 | 272 |
| Codex | 42 | 5 | 20.0 | 9.0 | 11.0 | 18.0 | 30.0 | 272 |
| Gemini | 13 | 1 | 64.23 | 3.0 | 66.0 | 104.0 | 122.6 | 172 |
| Zai OpenCode | 190 | 4 | 10.24 | 5.0 | 5.0 | 7.75 | 14.0 | 261 |

| bucket | trials |
| --- | --- |
| 1-5 | 114 |
| 101+ | 8 |
| 11-25 | 43 |
| 26-50 | 5 |
| 51-100 | 5 |
| 6-10 | 70 |
| unavailable (no trajectory) | 220 |

The longer Gemini mean applies to **13 observed trajectories out of 25 Gemini trials**, not all 25. Missingness differs by agent. Any comparison of efficiency needs matched tasks and complete capture, not these marginal summaries.

## Trial duration

Seconds from the recorded trial start to finish (`duration_seconds`). This includes environment setup, agent setup/execution, verifier work, and other wall-clock overhead represented by the result, rather than only token generation. Missing durations are excluded from statistics and displayed through each `n`; none is negative.

| agent_group | n | mean_s | median_s | p90_s | min_s | max_s |
| --- | --- | --- | --- | --- | --- | --- |
| ALL | 455 | 160.348 | 58.391 | 277.063 | 0.662 | 8935.518 |
| Codex | 59 | 613.209 | 100.491 | 712.887 | 1.292 | 8935.518 |
| Gemini | 25 | 175.114 | 60.048 | 354.182 | 24.609 | 898.545 |
| NOP | 36 | 24.563 | 8.796 | 14.76 | 3.035 | 543.61 |
| Oracle | 128 | 18.566 | 8.723 | 22.943 | 1.582 | 568.87 |
| Zai OpenCode | 207 | 140.776 | 109.999 | 278.755 | 0.662 | 1430.015 |

The mean is strongly affected by long Codex runs; representative largest durations:

| trial_id | job_name | agent_group | exception_class | duration_seconds |
| --- | --- | --- | --- | --- |
| 33a67054-9438-47c3-9102-d43348de7e1f | canary-transaction-reconciliation-codex-20260816 | Codex | — | 8935.518256 |
| e9242bd2-a149-490f-ae43-260abdfd4769 | canary-transaction-reconciliation-codex-20260816 | Codex | — | 6566.106752 |
| 2e654660-2835-4c63-b6e0-3f7776fd39d1 | canary-transaction-reconciliation-codex-20260816 | Codex | NonZeroAgentExitCodeError | 5173.492938 |
| bd471d16-d592-4c30-bb00-7bd533fc5ef9 | tb3-k1-ico-path-patch-luna-infra-retry1 | Codex | — | 2938.682061 |
| 18753c9f-dcf8-480f-8d9a-d322c1d9e088 | canary-terminal-bench-html-js-filter-codex-20260816 | Codex | — | 2698.483866 |

## All distinct recorded exception and failure signals

### Trial exceptions

All seven non-null `exception_class` values from trial facts, with exact recorded phase and one lookup UUID each:

| exception_class | exception_phase | trials | passes | example_trial |
| --- | --- | --- | --- | --- |
| NonZeroAgentExitCodeError | unknown | 31 | 0 | 1f2bd87d-2702-4689-b1bf-6492fd052de4 |
| ValueError | unknown | 15 | 0 | 08a1f816-8813-45a8-a98f-8a4a81e4032e |
| RuntimeError | unknown | 8 | 0 | 57862842-e62a-4bbb-b4f1-07a540c9604a |
| CancelledError | unknown | 3 | 0 | 0e5ec8fe-038e-489c-8d6c-e069c2c24947 |
| AgentTimeoutError | agent | 3 | 0 | 477a5527-c4f7-4667-92d3-e892c556f6a1 |
| RewardFileNotFoundError | verifier | 2 | 0 | 36eef4ed-d2c7-4448-804d-a6f8ac9622fb |
| ValidationError | unknown | 1 | 0 | d06a21a5-afee-4eee-89a7-b1e26153d2e2 |

No exceptional trial has reward 1. There are 402 trials without a recorded exception; absence of an exception is not evidence of valid execution. `ValueError`, `RuntimeError`, and generic nonzero exits do not identify an auth, task, or environment cause without raw logs. The feature table contains no additional exception classes.

### Trajectory quality and evidence availability

All distinct quality codes in the quality-findings Parquet file are listed below. Counts are the stored quality-table findings and unique trial IDs, not additive mutually exclusive failure categories. All referenced IDs are in the principal census; coverage is partial and multiple codes can apply to one trial.

| category | code | findings | trials |
| --- | --- | --- | --- |
| atif | ATIF_MISSING | 36 | 36 |
| atif | ATIF_UNPAIRED_TOOL_CALL | 1823 | 184 |
| control | CONTROL_NON_ATIF | 60 | 60 |
| infrastructure | INFRA_EXCEPTION | 38 | 38 |

- `ATIF_MISSING`: recorded message is `agent/trajectory.json is missing for billable/eval trial`.
- `ATIF_UNPAIRED_TOOL_CALL`: recorded messages vary by step and call count (`Step N has X tool calls but 0 observations`); this is one quality-code class, not hundreds of independent causal failure types. It may indicate adapter/projection limitations, not failed tools.
- `CONTROL_NON_ATIF`: `Control trial does not produce ATIF trajectory`; expected control evidence, **not** a model failure.
- `INFRA_EXCEPTION`: `Trial failed with infrastructure exception: Traceback (most recent call last):`. This stored message exposes only a traceback header, so its classification is not a root-cause diagnosis.

Distinct quality-report status/quarantine combinations:

| status | is_analysis_ready | quarantine_reason | trials |
| --- | --- | --- | --- |
| fail | False | missing_trajectory_file | 36 |
| pass | False |  | 57 |
| pass | True |  | 1 |
| quarantine | False | infrastructure_exception:Traceback (most recent call last): | 12 |
| warn | True |  | 182 |

The feature table's sole unavailable reason is `missing_trajectory_file` (124 unique IDs). This is a different coverage set from the 36 quality `ATIF_MISSING` findings; their counts should not be equated.

Distinct state-journal status/reason combinations:

| state_journal_status | state_journal_reason | trials |
| --- | --- | --- |
| absent | not_recorded | 267 |
| available | — | 136 |
| missing | partial_intake | 10 |
| unavailable | RuntimeError: Unable to find image 'evallab-state-journal:46bf038216ffe7ab' locally<br>docker: Error response from daemon: pull access denied for evallab-state-journal, repository does not exist or may require 'docker login'<br><br>Run 'docker run --help' for more information | 2 |
| — | — | 50 |

The explicit image-pull denial is an evidence-supported environment/journal availability problem on two trials. `absent/not_recorded`, `missing/partial_intake`, and null status are evidence gaps, not established agent capability failures.

### Signals that are unavailable or non-diagnostic

- Every projected observation has null `error_classification` and null `command_exit_code` (11,115 physical rows before deduplication). Trial-fact command-failure and repeated-failure totals are both 0. **These zeros cannot establish that no command failed.**
- Trajectory projection records `validation_status='valid'` throughout and null `harness_fault_signature`; trial-fact invalid-trajectory count totals 0. Schema validity is not complete tool-observation pairing or behavioral validity.
- There are **250 missing-artifact entries across 230 trials**. These are inventory flags; this census does not infer which missing files are required for a given adapter/control.
- State-event `invalid_reason` and `invalid_error_digest` are always null and `evidence_status='valid'`; action effects have only `unattributed` / `temporally_preceded` link statuses, not a causal failure label.
- The two label tables each contain only the demonstration label `test_label` (one record each). They supply no substantive failure taxonomy or accepted Stage 5 labels.

Accordingly, the data supports seven exception classes, four quality codes, and the availability reasons above—not an exhaustive semantic diagnosis of every failed trial. No `planning`, `context_management`, `tool_use`, or authentication-cause label is assigned without canonical trajectory/log evidence.

## Physical Parquet inventory

This inventory includes compact/raw duplicates and auxiliary tables; **physical rows are not unique trials**. Empty semantic tables have zero records, not zero observed capability failures.

| table | files | rows |
| --- | --- | --- |
| action_effects.parquet | 98 | 401 |
| agent_actions.parquet | 102 | 10694 |
| artifact_facts.parquet | 205 | 1531 |
| behavior_labels.parquet | 1 | 1 |
| capability_opportunities.parquet | 16 | 0 |
| constraint_facts.parquet | 16 | 0 |
| context_operation_facts.parquet | 16 | 0 |
| craft.parquet | 1 | 503 |
| evidence_coverage.parquet | 16 | 0 |
| jobs.parquet | 194 | 494 |
| ledger.parquet | 1 | 5 |
| llm_calls.parquet | 102 | 2952 |
| observations.parquet | 154 | 11115 |
| paired_condition_facts.parquet | 16 | 0 |
| process_step_facts.parquet | 16 | 0 |
| reward_facts.parquet | 201 | 948 |
| session_dependency_facts.parquet | 16 | 0 |
| state_changes.parquet | 98 | 401 |
| state_events.parquet | 93 | 2834 |
| steps.parquet | 154 | 4336 |
| tool_calls.parquet | 154 | 11123 |
| tool_usage.parquet | 152 | 736 |
| traj_features.parquet | 1 | 491 |
| traj_labels.parquet | 1 | 1 |
| trajectories.parquet | 154 | 295 |
| trajectory_events.parquet | 102 | 25201 |
| trajectory_phases.parquet | 102 | 634 |
| trajectory_quality_findings.parquet | 1 | 1957 |
| trajectory_quality_reports.parquet | 1 | 288 |
| trial_facts.parquet | 210 | 626 |

## Reproducible SQL and Python

Run from the repository root with `.venv/bin/python` and DuckDB 1.5.5. No database service is needed. The following initialization defines all views required by the queries. Readers explicitly disable automatic Hive partition extraction because compact and raw paths have different partition keys.

```sql
CREATE VIEW raw_facts AS SELECT * FROM read_parquet('derived/parquet/**/trial_facts.parquet', union_by_name=true, hive_partitioning=false, filename=true);
CREATE VIEW facts AS SELECT * FROM raw_facts QUALIFY row_number() OVER (
 PARTITION BY trial_id ORDER BY contains(filename, '/compact/'), filename)=1;
CREATE VIEW census AS SELECT *,
 CASE
 WHEN task_name LIKE '%action-memory%' THEN 'action-memory'
 WHEN task_name LIKE '%funcdag%' THEN 'mcp-funcdag (incl. syn-funcdag)'
 WHEN task_name LIKE '%/mcp-rec-%' THEN 'mcp-recovery'
 WHEN task_name LIKE 'terminal-bench/%' OR task_name IN ('break-filter-js-from-html','gpt2-codegolf','llm-inference-batching-scheduler','query-optimize') THEN 'terminal-bench'
 WHEN task_name LIKE '%event-summary' THEN 'event-summary'
 WHEN task_name LIKE 'deepplanning-%' THEN 'deepplanning'
 WHEN task_name LIKE '%transaction-reconciliation' THEN 'transaction-reconciliation'
 WHEN task_name LIKE 'factory-development/%' THEN 'factory-development'
 WHEN task_name LIKE 'factory-%' THEN 'factory-runtime-generation'
 WHEN task_name LIKE 'facet-semantic/%' OR task_name LIKE 'quality-dev/facet-%' THEN 'facet'
 WHEN task_name LIKE 'tw_%' THEN 'taskworld (tw)'
 WHEN task_name LIKE '%/seqgen-%' THEN 'seqgen'
 WHEN task_name LIKE '%/rc-004-%' THEN 'rc-004-faulty-dependency'
 WHEN task_name LIKE 'agentabstain/%' THEN 'agentabstain'
 WHEN task_name LIKE 'harbor-index/gaia2-%' THEN 'gaia2'
 WHEN task_name LIKE 'loca-bench/%' THEN 'loca-bench'
 WHEN task_name LIKE 'snap-research/locomo%' THEN 'locomo'
 ELSE 'unmapped' END AS family,
 CASE WHEN agent_name IN ('opencode','evallab.harbor_zai_opencode:SecretSafeZaiOpenCodeAgent') THEN 'Zai OpenCode'
 WHEN agent_name='codex' THEN 'Codex'
 WHEN agent_name='antigravity-cli' THEN 'Gemini'
 WHEN agent_name='oracle' THEN 'Oracle'
 WHEN agent_name='nop' THEN 'NOP'
 ELSE agent_name END AS agent_group
FROM facts;
CREATE VIEW named_rewards AS SELECT DISTINCT trial_id,reward_value FROM read_parquet('derived/parquet/**/reward_facts.parquet',union_by_name=true,hive_partitioning=false) WHERE reward_name='reward';
CREATE VIEW scored_census AS SELECT c.*,r.reward_value AS reward FROM census c LEFT JOIN named_rewards r USING(trial_id);
CREATE VIEW traj_features AS SELECT * FROM read_parquet('derived/parquet/traj_features/traj_features.parquet');
CREATE VIEW trajectory_quality_findings AS SELECT * FROM read_parquet('derived/parquet/trajectory_quality_findings.parquet');
CREATE VIEW trajectory_quality_reports AS SELECT * FROM read_parquet('derived/parquet/trajectory_quality_reports.parquet');
```

The following queries produced the report tables. They can be executed with `con.execute(sql).fetchall()` after running the initialization with `con = duckdb.connect()` and `con.execute(initialization_sql)`.
### Totals

```sql
SELECT count(*) trials,count(distinct job_id) jobs,count(distinct task_name) task_names,count(*) FILTER(WHERE reward=1) passes,count(*) FILTER(WHERE reward=0) zeros,count(*) FILTER(WHERE reward IS NULL) missing_rewards,count(*) FILTER(WHERE exception_class IS NOT NULL) exceptions FROM scored_census;
```

### Integrity

```sql
SELECT count(*) physical_rows,count(distinct trial_id) unique_trials,count(*) FILTER(WHERE contains(filename,'/compact/')) compact_rows FROM raw_facts;
```

### Key Variants

```sql
SELECT count(*) trials_with_metric_conflicts FROM (SELECT trial_id FROM raw_facts GROUP BY trial_id HAVING count(DISTINCT struct_pack(r:=primary_reward,e:=exception_class,s:=step_count,d:=duration_seconds,a:=agent_name,m:=model_name,t:=task_name))>1);
```

### Families

```sql
SELECT family,count(*) trials,count(*) FILTER(WHERE reward=1) passes,count(*) FILTER(WHERE reward IS NULL) missing_rewards,count(*) FILTER(WHERE exception_class IS NOT NULL) exceptions FROM scored_census GROUP BY 1 ORDER BY trials DESC,family;
```

### Agents

```sql
SELECT agent_group,count(*) trials,count(*) FILTER(WHERE reward=1) passes,count(reward) scored,count(*) FILTER(WHERE exception_class IS NOT NULL) exceptions,round(100.0*count(*) FILTER(WHERE reward=1)/count(*),2) pass_pct_all,round(100.0*count(*) FILTER(WHERE reward=1)/nullif(count(reward),0),2) pass_pct_scored FROM scored_census GROUP BY 1 ORDER BY 1;
```

### Models

```sql
SELECT agent_group,coalesce(model_name,'[missing]') model,count(*) trials,count(*) FILTER(WHERE reward=1) passes,count(reward) scored,round(100.0*count(*) FILTER(WHERE reward=1)/count(*),2) pass_pct_all FROM scored_census GROUP BY 1,2 ORDER BY 1,2;
```

### Family Agent

```sql
SELECT family,agent_group,count(*) trials,count(*) FILTER(WHERE reward=1) passes FROM scored_census GROUP BY 1,2 ORDER BY 1,2;
```

### Durations

```sql
SELECT agent_group,count(duration_seconds) n,round(avg(duration_seconds),3) mean_s,round(quantile_cont(duration_seconds,.5),3) median_s,round(quantile_cont(duration_seconds,.9),3) p90_s,round(min(duration_seconds),3) min_s,round(max(duration_seconds),3) max_s FROM census GROUP BY GROUPING SETS ((agent_group),()) ORDER BY 1 NULLS FIRST;
```

### Steps

```sql
SELECT agent_group,count(*) n,min(step_count) min,round(avg(step_count),2) mean,quantile_cont(step_count,.25) p25,quantile_cont(step_count,.5) median,quantile_cont(step_count,.75) p75,round(quantile_cont(step_count,.9),2) p90,max(step_count) max FROM census WHERE trajectory_count>0 GROUP BY GROUPING SETS ((agent_group),()) ORDER BY 1 NULLS FIRST;
```

### Step Buckets

```sql
SELECT CASE WHEN trajectory_count=0 THEN 'unavailable (no trajectory)' WHEN step_count=0 THEN '0 (trajectory present)' WHEN step_count<=5 THEN '1-5' WHEN step_count<=10 THEN '6-10' WHEN step_count<=25 THEN '11-25' WHEN step_count<=50 THEN '26-50' WHEN step_count<=100 THEN '51-100' ELSE '101+' END bucket,count(*) trials FROM census GROUP BY 1 ORDER BY 1;
```

### Exceptions

```sql
SELECT exception_class,exception_phase,count(*) trials,count(*) FILTER(WHERE reward=1) passes,min(trial_id) example_trial FROM scored_census WHERE exception_class IS NOT NULL GROUP BY 1,2 ORDER BY trials DESC;
```

### Quality

```sql
SELECT category,code,count(*) findings,count(distinct trial_id) trials FROM trajectory_quality_findings GROUP BY 1,2 ORDER BY 1,2;
```

### Quality Status

```sql
SELECT status,is_analysis_ready,quarantine_reason,count(distinct trial_id) trials FROM trajectory_quality_reports GROUP BY 1,2,3 ORDER BY 1,2;
```

### State Reasons

```sql
SELECT state_journal_status,state_journal_reason,count(*) trials FROM census GROUP BY 1,2 ORDER BY 1,2;
```

### Feature Status

```sql
SELECT status,count(*) row_count,count(distinct trial_id) unique_trials FROM traj_features GROUP BY 1;
```

### Feature Coverage

```sql
SELECT count(distinct f.trial_id) feature_ids,count(distinct f.trial_id) FILTER(WHERE t.trial_id IS NULL) feature_only_ids FROM traj_features f LEFT JOIN facts t USING(trial_id);
```

### Feature Only

```sql
SELECT f.trial_id,f.job_name,f.task_name,f.agent_name,f.model_name,f.primary_reward,f.status FROM traj_features f ANTI JOIN facts t USING(trial_id) WHERE f.task_name<>'unknown';
```

### Projection cross-checks and reward discrepancy query

```sql
SELECT count(*) AS distinct_steps, count(DISTINCT trial_id) AS trials
FROM (SELECT DISTINCT trial_id, document_id, step_id
      FROM read_parquet('derived/parquet/**/steps.parquet', union_by_name=true, hive_partitioning=false));

SELECT t.trial_id, t.task_name, t.agent_group, t.primary_reward, r.reward_value
FROM census t JOIN named_rewards r USING(trial_id)
WHERE t.primary_reward IS DISTINCT FROM r.reward_value;

SELECT trial_id FROM named_rewards GROUP BY trial_id HAVING count(*) > 1;
```

### Inventory and snapshot fingerprint

Fingerprint definition: sort consulted relative file paths; for each append `path + TAB + SHA256(file bytes) + NEWLINE`; SHA-256 the UTF-8 concatenation. This is a content fingerprint, not an immutable archived snapshot. Regeneration against changed files should produce a different fingerprint.

```python
from collections import Counter
from pathlib import Path
import duckdb
import hashlib

con = duckdb.connect()
files = sorted(Path("derived/parquet").rglob("*.parquet"))
for name, count in sorted(Counter(p.name for p in files).items()):
    paths = [str(p) for p in files if p.name == name]
    rows = con.execute(
        "SELECT count(*) FROM read_parquet(?, union_by_name=true, hive_partitioning=false)",
        [paths],
    ).fetchone()[0]
    print(name, count, rows)

used_names = {
    "trial_facts.parquet", "reward_facts.parquet", "steps.parquet",
    "trajectories.parquet", "observations.parquet", "traj_features.parquet",
    "trajectory_quality_reports.parquet", "trajectory_quality_findings.parquet",
    "behavior_labels.parquet", "traj_labels.parquet",
}
manifest = "".join(
    str(p) + "\t" + hashlib.sha256(p.read_bytes()).hexdigest() + "\n"
    for p in files if p.name in used_names
)
print(hashlib.sha256(manifest.encode()).hexdigest())
```

Consulted core file count: **878**. Manifest fingerprint: `18247708fd1a4e466da006721a71a16eb3f26cb59d06809b17e282389aceecf6`. Supplementary state-event/action-effect schemas were also inspected for failure-bearing columns; the core fingerprint above explicitly names its coverage.

## Review disposition and smallest next investigations

**Observed:** the principal census is larger than a 200-trial assumption, but data quality and duplicate representations materially affect denominators. Named reward and primary reward disagree on nine zeros; the feature table has duplicates, unknown-task entries, and asymmetric coverage. Exported command-error fields are uninformative on this snapshot.

**Interpretation:** these are reasons to reconcile the evidence/index layer before comparing model capability. They do not establish a model ranking or explain the cause of any specific OpenCode exception.

The smallest useful next actions require no new model spend: inspect the nine reward discrepancies against their canonical result files; reconcile feature-only records and repeated feature IDs; and audit required-artifact/trajectory availability separately for controls and billable adapters. For a capability comparison, first select a matched task/verifier/environment cohort and review its raw trial validity. Any proposed rerun must separately specify one changed variable, control expectations, attempts, limits, and the required approval. No rerun is authorized by this report.

## Verification receipt

All **19 SQL blocks** printed above executed successfully against the snapshot. Assertions checked 465 selected trials, 263 passes, 3,621 observed steps, and zero unmapped family names. Family trial/pass sums reconcile to the corpus totals. The core content fingerprint was recomputed after analysis and remained unchanged. No formatters, linters, project-wide tests, or builds were run.
