---
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: experiment
retrieved: 2026-08-30
license_note: Private repository; internal experimental evidence.
type: experiment-report
topic: zai-opencode-action-memory-handle-representation-e0b
status: measured
model: zai-coding-plan/glm-5.3-flash
trajectory_schema: ATIF-v1.7
evidence_class: calibration-only-darwin-public-egress
---

# Z.ai/OpenCode E0b handle-representation calibration

## Result

The live E0b campaign completed **72/72 scored trials with no trial exceptions or retries**. GLM-5.3-Flash passed **42/72 (58.3%)** across 36 deterministic task cells with two repetitions each.

This remains calibration evidence: the trusted-task Darwin lane used public agent egress and a read-only provider-auth mount. The newly merged credential proxy and Linux enforced-isolation lane were not used. No causal-grade or cross-model claim is made.

## Design

- Representations: `opaque`, `indexed`, `range_batch`
- Doses: 4,096 and 16,384 bytes
- Arms: `neutral_padding`, `semantic_distractor`
- Seeds: 42, 1337, 2026
- Repetitions: 2
- Trials: 3 representations × 2 doses × 2 arms × 3 seeds × 2 repeats = 72
- Independent matched assignment units: `(dose, arm, seed)`, n = 12

Within an assignment unit, the generator holds content, target truth, arm, dose, seed, and required read set fixed; only the declared handle-reference representation changes. Repetitions are nested executions and do not increase the 12 independent matched units.

## Outcomes by representation

| Representation | Pass | Pass@2 cells | Exact order | Coverage complete | Prompt tokens | Completion tokens | Retrieval calls |
|---|---:|---:|---:|---:|---:|---:|---:|
| `opaque` | 18/24 | 12/12 | 18/24 | 24/24 | 1,245,554 | 37,396 | 984 |
| `indexed` | 6/24 | 6/12 | 6/24 | 24/24 | 1,175,685 | 23,215 | 984 |
| `range_batch` | 18/24 | 11/12 | 18/24 | 24/24 | 945,235 | 9,465 | 31 |

Reward again equals exact required order in all 72 trials. Coverage was complete in every trial; all 30 failures were ordering failures rather than omissions.

## Independent-unit matched contrasts

Each representation's two repeat outcomes were averaged within each of the 12 base assignment units before comparing representations.

| Contrast | Better | Worse | Tie | Mean paired risk difference | Two-sided exact sign p |
|---|---:|---:|---:|---:|---:|
| indexed − opaque | 0 | 9 | 3 | -0.500 | 0.0039 |
| range_batch − opaque | 4 | 4 | 4 | 0.000 | 1.0000 |

`[OBSERVED]` Indexed handles were worse than opaque handles in nine assignment units and better in none. In this model/task/dose slice, making ordinal indices explicit did not repair sequencing and instead substantially reduced exact-order completion.

`[OBSERVED]` Range-batch and opaque representations had the same aggregate pass count and zero mean paired risk difference. This is **not** evidence of equivalence or non-inferiority at n=12; it establishes that this pilot detected no directional outcome difference.

## Efficiency

Relative to opaque handles, range-batch retrieval changed observed resources by:

- prompt tokens: **-24.1%**
- completion tokens: **-74.7%**
- retrieval tool calls: **-96.8%**
- logical issued handles: 990 versus 984 (**+0.6%**, from a small number of duplicate logical reads)

`[INFERENCE]` Range batching is a promising efficiency intervention because it preserved the observed aggregate success rate while sharply reducing model/tool interaction. A larger seed-matched campaign is required before treating outcome preservation as established.

## Dose/arm detail

| Representation | Dose | Arm | Pass |
|---|---:|---|---:|
| indexed | 4k | neutral | 3/6 |
| indexed | 4k | semantic | 0/6 |
| indexed | 16k | neutral | 2/6 |
| indexed | 16k | semantic | 1/6 |
| opaque | 4k | neutral | 5/6 |
| opaque | 4k | semantic | 5/6 |
| opaque | 16k | neutral | 5/6 |
| opaque | 16k | semantic | 3/6 |
| range_batch | 4k | neutral | 5/6 |
| range_batch | 4k | semantic | 4/6 |
| range_batch | 16k | neutral | 6/6 |
| range_batch | 16k | semantic | 3/6 |

The indexed deficit appears across both doses and arms rather than in one isolated cell. Range-batch's efficiency advantage also appears at both doses, while outcome differences vary by cell.

## Capture and evidence integrity

After deterministic expansion of `get_context_chunks` batch lists and range descriptors into their logical handle sequences, ATIF and benchmark-event retrieval order were concordant in **72/72** trials. Batch retrieval remains one tool call in operational accounting while representing multiple logical handle reads in coverage/order analysis.

The promoted summary records both physical retrieval calls and logical issued handles so analysis cannot confuse tool-call compression with missing retrieval.

## Resource accounting

| Metric | Total |
|---|---:|
| Prompt tokens | 3,366,474 |
| Cached tokens | 2,689,280 |
| Completion tokens | 70,076 |
| ATIF steps | 408 |
| ATIF tool calls | 2,143 |
| Trial exceptions | 0 |
| Retries | 0 |
| `cost_usd` | null |

No dollar-cost claim is made because the subscription lane reports `cost_usd: null`.

## Decision boundary and next run

- **Reject indexed handles as a sequencing repair for this slice.** The observed matched direction is adverse.
- **Advance range-batch as an efficiency candidate, not an outcome improvement.** The next confirmatory design should add independent seeds and declare a non-inferiority margin before execution.
- **Keep semantic/model-based analysis out of decision-bearing E0b statistics.** Mechanical coverage, order, issued-count, duplicate, token, and tool-call facts fully describe the measured result.
- **Run the next canary through the credential proxy**, then repeat selected range-batch/opaque cells on the Linux isolation lane before any causal-grade promotion.
