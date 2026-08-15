# Trajectory intelligence: metrics and failure signals

Role: DATA-STRATEGY. Date: 2026-08-15. These are deterministic descriptive
metrics over the ATIF Parquet projection. They are screening instruments, not
causal explanations; a raw-trajectory review remains the adjudication step.

## Measurement surface

The queries in `research/analysis/queries.sql` use four ATIF tables
(`trajectories`, `steps`, `tool_calls`, `observations`) and `trial_facts`.
Identity is `(job_id, trial_id, document_id)`; step order is `step_id`. Missing
token, cost, reward, or exit-code values stay null and are reported as missing,
never coerced to success or zero usage.

## Core metrics

### Loop Index (LI)

For a trial with `N` tool calls and `D` distinct
`(function_name, arguments_sha256)` signatures:

```text
LI = (N - D) / N, for N > 0; otherwise NULL
```

LI ranges from 0 (no repeated signature) toward 1 (the same call repeats).
Because the projection hashes arguments, it detects exact semantic payload
repetition without loading potentially sensitive content. Repeating a safe
read can be rational; LI is a triage rank, not a failure label. Compare within
task/scaffold and inspect repeated non-zero command exits before concluding a
loop occurred.

### Tool Efficiency Ratio (TER)

```text
TER = linked observations whose command_exit_code is NULL or 0 / tool calls
```

A result is linked only when `observations.source_call_id` matches
`tool_calls.tool_call_id`. TER therefore combines execution success and trace
completeness. The query reports unlinked calls separately: a low TER with many
unlinked calls is primarily an instrumentation warning, while a low TER with
linked non-zero exits is an execution-quality signal. Trials with no tool calls
receive NULL, not 0.

### Context Bloat Velocity (CBV)

CBV is `regr_slope(prompt_tokens, llm_step_ordinal)` over steps that carry a
prompt-token count, measured in prompt tokens per LLM-bearing step. The query
also reports the number of measured points and first/last counts. Fewer than
two points produce NULL. Positive CBV is normal for append-only contexts; the
comparison of slopes within matched tasks/scaffolds is the useful signal.

CBV is not context-window occupancy. Adapters differ in whether a step metric
means per-call input, cumulative input, or is omitted, so cross-adapter claims
require confirming metric semantics first.

## Failure buckets

These buckets deliberately separate harness trouble from agent behavior.

| Bucket | Operational candidate rule | Interpretation boundary |
|---|---|---|
| Flaky Verifier | Same `task_digest` + `verifier_digest` has both reward 1 and reward <1 across exception-free trials | Candidate only; agent/model differences and stochasticity may explain the split |
| Tool Hallucination | Tool call has no observation linked by `tool_call_id` | Could be adapter loss, cancellation, or invalid tool selection; raw trace decides |
| Timeout | `exception_class` or `exception_phase` contains `timeout` | Harness outcome, not a scored agent failure; retain phase |
| Surrender | No exception, reward <1 or null, ≤3 steps, and zero tool calls | Early-stop candidate; controls and tasks requiring no tools must be excluded during review |

The existing `TrialAnalysisOutput` taxonomy remains the reviewed label surface.
These SQL buckets populate review queues; they do not overwrite human-reviewed
analysis or change rewards.

## Additional diagnostics

- Repeated failed commands joins exact call signatures to non-zero exits.
- Context spikes use step-to-step prompt-token deltas, making compaction resets
  visible as negative deltas and large tool-output ingestion visible as jumps.
- Missing token/cost coverage is reported by model before token-efficiency or
  cost comparisons are made.
- Failure-bucket counts use mutually non-exclusive flags; one trial may time
  out after repeated tool failures.

## Comparison protocol

1. Filter to one provenance zone and compatible adapter metric semantics.
2. Match task, verifier, environment, scaffold, prompt digest, and model.
3. Require multiple attempts and report the distribution, not one run.
4. Rank with LI/TER/CBV and bucket queries.
5. Review raw ATIF at the earliest implicated step and record a cited
   `TrialAnalysisOutput` label.
6. Publish the operational threshold, missingness, and number reviewed.

The literature basis and limitations are summarized in
`docs/research/literature-survey.md`; this document defines only what the lab
can compute reproducibly from its current projection.
