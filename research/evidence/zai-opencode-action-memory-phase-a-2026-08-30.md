---
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: experiment
retrieved: 2026-08-30
license_note: Private repository; internal experimental evidence.
type: experiment-report
topic: zai-opencode-action-memory-phase-a
author: eval-lab
status: measured
model: zai-coding-plan/glm-5.3-flash
trajectory_schema: ATIF-v1.7
evidence_class: calibration-only-darwin-public-egress
---

# Z.ai/OpenCode Action Memory phase-A calibration

## Result

The live Z.ai/OpenCode campaign completed **36/36 scored trials with no trial exceptions or retries**. GLM-5.3-Flash passed **19/36 (52.8%)**. Harbor pass@2 was **14/18 task cells (77.8%)**.

This is calibration evidence, not causal-grade promotion: the trusted-task Darwin lane used public agent egress and a read-only provider-auth mount. It does not satisfy Linux enforced isolation or proxy-grade credential isolation.

A first launch named `zai-overnight-action-phase-a-20260830` failed before model execution because the Harbor subprocess could not import the repository Z.ai adapter. It produced no trial results and is excluded from every model denominator. The corrected job was `zai-overnight-action-phase-a-v2-20260830`.

## Design

- Model: `zai-coding-plan/glm-5.3-flash`
- Agent: OpenCode 1.18.25 through `ZaiOpenCodeAgent`
- Harbor: 0.21
- Benchmark: Action Memory dose ladder
- Doses: 4,096 / 16,384 / 65,536 bytes
- Arms: `neutral_padding` / `semantic_distractor`
- Seeds: 42 / 1337 / 2026
- Repetitions: 2 per dose-arm-seed cell
- Concurrency: 3
- Total: 18 task cells, 36 scored trials

The task generator holds dose and seed matched across arms. Repetition is an execution repeat over the same deterministic task package, not a new independent task.

## Outcomes by dose and arm

| Dose | Arm | Pass | Coverage complete | Exact required order | Prompt tokens |
|---:|---|---:|---:|---:|---:|
| 4k | neutral | 5/6 | 6/6 | 5/6 | 210,440 |
| 4k | semantic | 2/6 | 6/6 | 2/6 | 218,353 |
| 16k | neutral | 5/6 | 6/6 | 5/6 | 359,678 |
| 16k | semantic | 4/6 | 6/6 | 4/6 | 440,801 |
| 64k | neutral | 2/6 | 3/6 | 2/6 | 2,437,906 |
| 64k | semantic | 1/6 | 5/6 | 1/6 | 2,430,984 |

Across matched seed-repeat pairs, semantic minus neutral outcomes were:

| Dose | Semantic better | Semantic worse | Tie | Mean paired difference |
|---:|---:|---:|---:|---:|
| 4k | 1 | 4 | 1 | -0.500 |
| 16k | 1 | 2 | 3 | -0.167 |
| 64k | 0 | 1 | 5 | -0.167 |

`[OBSERVED]` Semantic-arm outcomes were lower overall: neutral passed 12/18 and semantic passed 7/18. `[INFERENCE]` This does not establish a monotone semantic-interference dose response: the largest observed paired gap was at 4k, not 64k, and each dose has only six matched pairs.

## Repeat stability

Of the 18 deterministic dose-arm-seed cells:

- stable pass on both repetitions: 5
- stable fail on both repetitions: 4
- mixed pass/fail: 9

`[OBSERVED]` Half of the cells changed outcome across two executions of the same task. `[INFERENCE]` A single attempt is therefore a poor estimate of this lane's per-cell reliability. Pass@2 improves the task-cell result, but it measures rescue by repetition rather than first-attempt reliability.

## Retrieval mechanism

Reward was exactly equivalent to order fidelity in this campaign:

- reward 1.0 and exact required read order: 19/36
- reward 0.0 and non-exact order: 17/36
- exceptions to that equivalence: 0

The 17 failures divide mechanically into:

- **13 complete-but-reordered retrievals**
- **4 incomplete-coverage retrievals**

Additional observed faults:

- 6 trials issued an unknown handle or received application-level `not_found`
- 5 trials duplicated at least one read
- 32/36 covered every expected handle
- only 19/36 preserved the exact required sequence

At 4k and 16k, every failure had complete unique coverage and failed on order alone. At 64k, coverage, unknown-handle, duplication, and ordering faults co-occurred. The result strengthens the case for measuring these dimensions separately and for the E0b handle-representation intervention; it does not support a single generic context-capacity mechanism.

## Trajectory fidelity boundary

ATIF tool-call issuance matched benchmark-event order in 34/36 trials. Two failed 64k semantic trials had many more benchmark-side reads than direct ATIF MCP calls:

- `action-64k-semantic_distractor-s__6vDNEHZ`: 264 benchmark reads, 7 duplicates
- `action-64k-semantic_distractor-s__8aYeUds`: 522 benchmark reads, 264 duplicates, one unknown handle

`[INFERENCE]` Those trials used an execution path that the ATIF projection did not expand into one event per benchmark read. Benchmark events remain the retrieval ground truth for them; trajectory-only ordering analysis must refuse or mark capture incompleteness.

## Resource accounting

| Metric | Total |
|---|---:|
| Prompt tokens | 6,098,162 |
| Cached tokens | 5,341,120 |
| Completion tokens | 138,349 |
| ATIF steps | 328 |
| ATIF tool calls | 3,664 |
| Trial exceptions | 0 |
| Retries | 0 |
| `cost_usd` | null |

Prompt use was 87.1% of the phase-A 7,000,000-token ceiling. Because the subscription lane reports `cost_usd: null`, no dollar-cost claim is made.

## Evidence handling

The promoted bundle must use promotion schema v2 and omit raw job/trial logs, OpenCode streams/runtime databases, sessions, auth files, and symlinks. The machine-readable summary beside this report contains per-trial rewards, tokens, coverage/order/unknown/duplicate facts, matched contrasts, and the excluded pre-model launch. Raw task and agent runtime state remain outside version control.
