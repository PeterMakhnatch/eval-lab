---
status: living
audience:
  - analyst
  - builder
---

# Behavioral Analysis

Behavioral analysis expands the evaluation laboratory beyond binary pass/fail scores by analyzing *how agents worked* across execution trajectories.

All behavioral telemetry is accessed exclusively through the unified attach surface (`evallab db attach`, `evallab.storage.attach.attach()`, reading `trial_facts`, `steps`, and `tool_calls`) or via trace query mode (`evallab db attach --trace`, reading `v_trace_trials` and `v_trace_steps`). Direct Parquet globbing is prohibited.

---

## 1. Unified Behavioral Views (`sql/behavior.sql`)

The repository defines six reusable DuckDB views in `sql/behavior.sql`. Every aggregate view carries sample size `n` (and populated counts) beside every metric.

### `v_behavior_trial_summary`
Per-trial unified classification joining `trial_facts` with outcome categories and computed per-trial efficiency metrics:
- `outcome`: Categorized into `'passed'` (`primary_reward >= 1.0` and no exception), `'scored_zero'` (`primary_reward == 0.0` and no exception), or `'never_measured'` (harness exception or null reward).
- `seconds_per_step`: `agent_execution_seconds / step_count` (`NULL` when `step_count = 0`).
- `steps_per_reward_point`: `step_count / primary_reward` (`NULL` when reward is 0 or trial is unmeasured).

### `v_behavior_effort_by_outcome`
Aggregates execution effort by `task_name`, `agent_name`, and `outcome`:
- `n`: Total trials in slice.
- `avg_steps`: Mean step count per trial.
- `avg_tool_calls`: Mean tool calls (`exec`) per trial.
- `avg_llm_calls`: Mean LLM API calls per trial.
- `avg_execution_seconds`: Mean agent execution runtime in seconds.
- `avg_duration_seconds`: Mean total trial wall-clock time in seconds.
- `avg_reward`: Mean primary reward.

### `v_behavior_efficiency`
Evaluates agent resource efficiency and cost-to-solution:
- `seconds_per_step`: `sum(agent_execution_seconds) / sum(step_count)` (`NULL` if 0 steps).
- `steps_per_reward_point`: `sum(step_count) / sum(primary_reward)` for passed trials (`NULL` for scored zero or unmeasured trials).

### `v_behavior_struggle_signals`
Tracks looping and error signals from trajectory execution:
- `repeated_failed_command_count`: Frequency of repeated failing shell commands.
- `command_failure_count`: Frequency of non-zero command exits.
- `invalid_trajectory_count`: Malformed trajectory schema instances.

### `v_behavior_step_shape`
Analyzes trajectory structural composition from the `steps` table:
- `n_trials_with_steps` / `n_trials`: Trials with recorded ATIF trajectories.
- `total_steps_n`: Total step documents across the cohort.
- `system_pct`: Proportion of steps originating from system prompt setup.
- `agent_pct`: Proportion of steps originating from agent thought/action turns.
- `user_pct`: Proportion of steps originating from user/tool response turns.

### `v_behavior_token_economics`
Tracks token consumption and inference cost with explicit coverage reporting:
- `n_total`: Total trials in cohort.
- `n_populated`: Number of trials with non-null token/cost records.
- `coverage_summary`: Formatted as `"X of Y trials"`.
- `populated_pct`: Percentage of trials with token telemetry.
- `avg_input_tokens`, `avg_cache_tokens`, `avg_output_tokens`, `avg_cost_usd`, `total_cost_usd`.

---

## 2. Metric Conventions and Edge Cases

### The Three-Way Outcome Split
Trials are partitioned into three distinct, non-overlapping outcome states:
1. **`passed`**: `primary_reward >= 1.0` and `exception_class IS NULL`.
2. **`scored_zero`**: `primary_reward == 0.0` and `exception_class IS NULL`.
3. **`never_measured`**: `exception_class IS NOT NULL` or `primary_reward IS NULL`.

Harness exceptions are never folded into scored failures. Folding exceptions into zero scores corrupts agent behavioral averages with harness crashes.

### Undefined Efficiency Ratios
When an agent scores 0.0, the ratio of steps to reward points is **undefined** ($0 / 0$ or $N / 0$).
- Undefined ratios are represented strictly as `NULL` (`None`), never as `0.0` or $\infty$.
- Similarly, `seconds_per_step` is `NULL` when `step_count` is 0.

### Sparse Token Economics (The "17 of 92" Rule)
In the current corpus, token counts and costs are populated for only **17 of 92 trials** (all from `codex` canary runs on 2026-08-15 and 2026-08-16). Smoke runs and control runs did not record token usage.
- All token metrics must explicitly report coverage (e.g. `"17 of 92 trials"`, `18.5%`).
- Telemetry must never compute averages over a silent subset without surfacing the denominator.

### Struggle Signal Instrumentation
Across all 92 trials in the current corpus, `repeated_failed_command_count`, `command_failure_count`, and `invalid_trajectory_count` are uniformly 0.
- A column that is uniformly zero is a finding about **instrumentation coverage**, not about agent capability.
- Telemetry parsers have not yet mapped ATIF inner tool exit codes or command repetitions into these summary columns.

### Power Gating & Statistical Comparisons
- Bootstrap 95% confidence intervals are computed using `evallab.cohort.bootstrap_mean_interval`.
- When sample size is below the power threshold ($n < 5$) or when bootstrap intervals overlap, comparisons are labeled `"not distinguishable"` rather than asserting an underpowered point estimate.

---

## 3. CLI Usage

Run behavioral analysis from the command line:

```bash
# Human-readable markdown report to terminal
uv run evallab behavior

# Machine-readable JSON output
uv run evallab behavior --json

# Filter to a specific task or agent
uv run evallab behavior --task terminal-bench/html-js-filter
uv run evallab behavior --agent codex

# Custom Parquet root override
uv run evallab behavior --derived-root /path/to/parquet
```

---

## 4. Python API

```python
from pathlib import Path
from evallab.behavior import generate_behavior_report, render_behavior_report, report_to_dict

report = generate_behavior_report(Path.cwd())

# Terminal text rendering
print(render_behavior_report(report))

# Structured dictionary for analysis pipelines
data = report_to_dict(report)
print(f"Total trials: {data['total_trials']}, Token coverage: {data['token_coverage_summary']}")
```

---

## 5. Evidence-backed behavior episodes

`evallab.behavior_episodes` adds multi-label, step-bounded interpretations without
rewriting Harbor ATIF. Detector input is the normalized event-mart action sequence;
detector output is stored atomically in
`derived/behavior_episodes/behavior_episodes.parquet` and is queryable through the
unified attach surface as `behavior_episodes`.

The calibrated v1 dimensions are `tool_error`, `unchanged_retry`,
`recovered_progress`, and `verification_gap`. `unresolved_error` is the
right-censored negative recovery route, not a fifth calibrated dimension.
`effect_loop_candidate` remains experimental and requires repeated equivalent
observations plus explicit complete evidence that no state changed across the
interval. Missing observation, relevance, or state-coverage data produces an
unknown assessment rather than a positive label.

Definitions, exclusions, counterexamples, detector versions, and calibration
status live in `research/behavior/catalog-v1.yaml`. Calibration in
`evallab.behavior_calibration` reports confusion counts, precision, and recall
separately for each behavior. A dimension without explicit human ground truth is
excluded; it is not silently counted as negative.

Phoenix is only the disposable annotation surface. `evallab.phoenix_annotations`
publishes episodes on an exact evidence span when the ATIF-to-OTLP conversion
provides an unambiguous step mapping, otherwise on the trace root. Reviewed
annotations are imported only when Phoenix reports authenticated UI provenance
(`HUMAN`, `APP`, and a non-empty `user_id`). Harbor ATIF and the Eval Lab evidence
store remain canonical.

## 6. Recorded-run queries and the Python failure atlas

The [trace attach surface](attach-surface.md) exposes transient DuckDB trial and
step views over retained ATIF, Parquet and processed reports. Trial aggregates
use native job/trial identities; a missing trajectory contributes no invented
steps. Proxy tokens require the processor's validated single-trial attribution.
Missing counts, token attribution or annotations remain unknown.

The [failure atlas](../research/explorations/trace-lab/failure-atlas/README.md)
is an inspection report, not an exhaustive causal taxonomy. It retains category
denominators, raw step links, input hashes and intervention hypotheses. Page
opinions carry their measured calibration scope; historical accuracy is not
transferred to a new cohort.

Frozen annotations are separate authorities:

- HAR-119 contains blind agent-rater failure labels.
- HAR-109 contains frozen hand annotations, not authenticated human provenance.
- HAR-128's adjudicated SFT-pass gate concerns cleanliness and cut points. A
  genuine `counted_pass` can be unsuitable for SFT. These annotations are not
  loop-kind ground truth and never rewrite counts.

Rebuild from the current published corpus:

```bash
uv run python research/explorations/trace-lab/failure-atlas/build.py \
  --repo-root . --out-dir research/explorations/trace-lab/failure-atlas
```

`--repo-root` selects the experiment/label input checkout; canonical SQL comes
from the executing code checkout. Use an explicit `--derived-root` to read an
approved shared Parquet tree from an isolated worktree.

For the frozen overnight three-arm evaluation, add
`--g5-cohort <frozen-g5-specs>/cohort.json`. The consumer binds the admitted
cohort and all 60 sibling spec files using the filename-ordered manifest. It
matches native source identities, recorded task/package/arm and frozen spec
fields; a results-home card prefix is not an experiment identity, and the
shared base model does not distinguish stock from GEPA. Retained published
spec bytes remain usable after the original worktree is retired.

G5 gets a separate expected-cell table for stock, tuned and GEPA. Missing or
unbound cells retain null outcomes, never counted failures. Available category
frequencies keep their own evidence denominators and raw-verified exemplars.
G5 identities never enter the historical pattern denominator, even without the
cohort argument. The four HAR-116/G2 calibration receipts remain separate,
hash-bound studies; their accuracy is not transferred to G5.

The [initial consumer proof](../research/experiments/har117-results-home/har131-g5-atlas-consumer-proof.json)
preserves the earlier 249-trial smoke with only 13 bound G5 outcomes. The
current [atlas bundle](../research/explorations/trace-lab/failure-atlas/README.md)
instead covers 296 recorded trials: 212 Python-eligible, including 152
historical and all 60 frozen G5 cells, with 84 exclusions retained. All ten
saved queries ran, and 38 chosen step references passed raw-file hash and
step-identity checks. Five eligible historical trials lack ATIF bytes
(four null-count HAR-104 trials and one infra-excluded HAR-120 trial); none
is a G5 cell or an exemplar. The integrated CLI reproduces the bundle's input
digest and all 60 native verdict/identity bindings, with no reflection payload.

The [original-attempt publication receipt](../research/experiments/har117-results-home/har131-g5-original-publication.json)
separately freezes all 60 original G5 jobs: 6 counted passes, 49 counted failures
and 5 infrastructure exclusions with null verifier rewards. It binds 240
raw-record fact checks and 6,339 source files unchanged during publication-card
repairs. Every complete landing check—including the correct card and INDEX
row—finished within 30 minutes; the maximum was 20 minutes 44.324 seconds.
Research-Harbor's final 16:42Z disposition cancelled the proposed replacements,
so these are also the final 60 physical cells. This receipt is not the
preregistered contrast analysis or a G5 loop-calibration result; all five
infrastructure outcomes remain missing rather than zero-valued failures.

The full atlas may contain held-out evidence and must not be sent to a prompt
proposer. Reflection export requires explicit authorized task/package bindings
and canonical usable-training, countability and frozen-eval exclusions. Only
the separately gated training payload is eligible for that use; missing or
invalid authorization is not an empty successful export.
