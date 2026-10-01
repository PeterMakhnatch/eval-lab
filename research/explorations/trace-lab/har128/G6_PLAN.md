# HAR-128 G6: behaviour across the three eval arms (written before G5 ran)

This plan was frozen before any G5 run existed. The three arms are stock, tuned (LoRA adapter) and GEPA (stock plus addendum), each run on the 20 frozen eval tasks once (G1 v2, sha256 3b997fdc…). The pass-rate test belongs to PREREG.md (HAR-133). This file covers the behaviour comparison and the trace explanation of every pass difference.

## Blinding

1. I ask Engineering on HAR-126 for the 60 trial paths, with the arm replaced by an opaque id.
2. Hand labels follow `RATER_GUIDE_v2.md` (two raters), and the deterministic metrics below are computed on the blinded ids. Both are frozen (sha256 plus `FROZEN_AT`) before the arm map is read.
3. Only then is the arm map joined.

## Metrics (one row per trial, all deterministic, from `process-job` output as merged)

| metric | source |
|---|---|
| passed / counted | `counts.verdict` (`counted_pass`, `counted_fail`, `excluded`) |
| stop | `stop_reason`, plus lf2 loop-break where recorded |
| completion claims | `handshake.confirmed`, `handshake.turns_after_first_prompt`, `handshake.echo_task_complete_turns` |
| loops | `token_flow.loop_onset.step_id` (present / absent), `loop_suspicion.score` |
| format errors | `shape_counts.unparseable`, plus `rejection_causes` counts per kind |
| time to first edit | first agent step where `token_flow._is_edit` is true and `_is_ephemeral_only` is false; the same rule as `last_useful_edit`, scanned forward |
| work after last edit | `token_flow.tokens_after_last_edit` |
| tokens | `tokens_proxy.total_tokens` (proxy-settled; native sums are not used) |
| agent steps | `agent_steps` |

## Comparisons

- Per arm: the median and IQR of each numeric metric, and the rate of each binary one.
- **Paired, per task:**
  - tuned − stock and GEPA − stock differences;
  - for each binary metric, the number of tasks where it flips in each direction.
- **Descriptive only.** With n = 20 and a single attempt, no behaviour difference is called significant here. PREREG.md owns inference.
- **Every task where pass/fail differs between arms** gets a trace explanation: first failure, blame and loop kind from the frozen hand labels, plus the decisive steps quoted from each arm's trajectory.

## Tools scored out of sample on the same runs (HAR-128 part 2)

- **Tools:** Eval Lab (as merged), Scout rules, Scout LLM (glm-5.3) only if spend allows, and Docent `anthropic/claude-opus-5-5` (hosted, $0).
- **Fields:** the same ones as HAR-119.
- **Scoring:** agreement with each rater and with the agreed cells, with 95% Wilson intervals.
- **Prompts:** frozen as in PR #610; no changes after the label freeze.
