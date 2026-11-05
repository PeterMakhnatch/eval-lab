# Production J3 review ordering

This experiment measures the production line export on the frozen 690-run DEV replay.
It changes queue order, not keep/review/drop decisions.
No held-out scoring or new model calls run.

`measure.py` runs 3 production batches and their deep checks:

1. Default J3-off arrival order with no ordering cache.
2. J3-off decision stage with an explicit ordering-only cache.
3. The same ordering cache with `audit.keep_top_n = 10`.

The script compares all decision fields except the new priority metadata.
Labels enter the analysis only after all production batches finish.
The script checks the exported review order against the measured order.
It also checks that the default keep audit is empty.

## Ordering and fallback

Complete, input-bound, route-attested J3 readings sort first by descending score.
Equal scores retain arrival order.
Unavailable readings follow scored readings in arrival order.
With no cache and the J3 stage off, every row retains arrival order.
The audit uses the same order over kept runs.
It does not remove them from `kept.jsonl` or change decisions.
This targeted audit supports spot checks, not an unbiased estimate of the keep-set cheat rate.

## Observed DEV result

All 3 production batches pass all 8 line checks.
Ordering and the optional audit change 0 decision rows after excluding only the 2 priority fields.
The fixed replay contains 690 runs: 332 cheats and 358 honest runs.
The review export contains 366 runs, including 111 cheats.

| Target cheat recall | Cheats to find | Arrival reviews | J3 reviews | Reviews saved [95% paired task-cluster CI] |
|---|---:|---:|---:|---:|
| 50% | 56 | 231 | 124 | 107 [71, 152] |
| 80% | 89 | 299 | 285 | 14 [-5, 43] |
| 90% | 100 | 323 | 308 | 15 [-24, 28] |

The intervals at 80% and 90% include no benefit.
The comparator is the current review-export arrival order, not Station's separate information-value mode.
The J3 queue has 354 scored rows and 12 fallback rows.
Without a cache, all 366 rows use the fallback and require 231 / 299 / 323 reviews.

The kept set contains 72 runs, including 9 cheats.
Strict production validation accepts 62 cached scores and uses arrival fallback for 10 runs.
Its ordered audit requires 10 / 40 / 41 reviews for 50% / 80% / 90% of kept cheats.
The optional top-10 audit includes 5 of the 9 kept cheats.
The default audit artifact is absent.
The earlier research ranking is not the production attestation check; use these measured coverage counts.


## Reproduce

```bash
uv run python research/experiments/ev1105-queueimpl/measure.py \
  --private /private/tmp/ev1105/queueimpl/measurement
```

The default inputs are the frozen replay-v1 set and the retained native J3 cache.
`--data`, `--cache`, and `--labels` can select their preserved copies.
The label snapshot supplies DEV task clusters for analysis only.
The script never uses held-out rows.

`results/aggregates.json` stores aggregates and input digests, not trajectory text.
Intervals use the predecessor's 2,000 task-cluster bootstrap draws, seed `1106`.
Use raw paired intervals for reviews saved on the sampled task population.
The scaled intervals express reviews at the original queue size.

## Cost boundary

Ordering cached readings makes 0 new calls and costs $0.
The optional live J3 stage remains default-off.
Its scope is non-dropped runs, not only review exports.
Historical window accounting is not a new live measurement.
The [j3line receipt](../ov1106-j3line/results/line.json) records 552 windows over 438 eligible runs.
It records 5,199,592 input tokens and 61,507 output tokens.
The mean is 1.26 calls, 11,871 input tokens, and 140 output tokens per eligible run.
The review subset contains 465 historical windows, 4,571,234 input tokens, and 53,395 output tokens.
That is 1.27 calls, 12,490 input tokens, and 146 output tokens per reviewed run.
Charging the whole 438-run stage to the 366 review rows gives 1.51 calls per review.
That denominator gives 14,207 input tokens and 168 output tokens per review.
These historical totals include unavailable readings. They do not assume that new calls succeed.
These are subscription tokens, not a paid API price estimate.
