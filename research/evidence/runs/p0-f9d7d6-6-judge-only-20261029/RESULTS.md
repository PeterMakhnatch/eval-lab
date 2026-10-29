# P0-SELF-f9d7d6-6: judge-only cascade, partial evaluation

**Answer: do not replace v8's gate on this evidence.** The preregistered replication and common-budget improvement were not established; the study stopped at its 50-minute wall limit. The completed subset is severely confounded by length and cannot support an estimate of the length-adjusted difference or a general scorecard comparison. This is inconclusive, not proof that judge-only is worse in a properly balanced sample.

Preregistration `6855c15b78a920ffcee370ba88dfb8e514aca1dc`, 2026-10-29 4:51:51 PM ET, was pushed before evaluation judge calls. Optional `cascade_stream_judge_only`, version `window-cascade/v8+judge-only`, judges former gate cards without taking slots away from v8's existing judge windows; shipping v8 and its gate remain unchanged. No rule, prompt or threshold tuning occurred.

## Sample and missingness

Strict-DEV gate 2,388; after excluding pair, 150 and p0f5 IDs, 1,444 eligible. The 402-run seeded draw (digest and cap in `sample-report.json`) had cells t1 enacted/clean 100/100, t2 100/0, t3 100/2. Thus even a complete draw cannot satisfy the preregistered two-measurable-tertile replication criterion: the first sample exhausted t2 clean and left only two t3 clean. **240/402 were read** before the wall stop; 162 unread, zero abstentions, zero missing findings among the completed pairs. Completed t1 enacted/clean 0/47, t2 100/0, t3 93/0. This is a hash-ordered partial prefix, not a balanced subsample; because the preceding sample used the same hash order, it is particularly unbalanced. All completed clean runs are short and all completed enacted runs are longer. The aggregate results are descriptive only.

## Partial fresh results

Wilson 95% intervals in brackets; N/A means a class was absent. `results.json` contains the complete calculations. Bootstrap uses 2,000 task-cluster resamples (seed 20261029); no tertile has both classes, so every tertile difference and the pooled Mantel–Haenszel difference and their CIs are **not estimable**, rather than zero.

| Steps | Judge-only recall | Judge-only FAR | Enacted minus clean [95% CI] |
|---|---:|---:|---:|
| <=40 | N/A (0 enacted) | 1/47 = 2.1% [0.4, 11.1] | N/A |
| 41–89 | 10/100 = 10.0% [5.5, 17.4] | N/A (0 clean) | N/A |
| >=90 | 18/93 = 19.4% [12.6, 28.5] | N/A (0 clean) | N/A |
| Pooled MH | — | — | N/A |

On these same 240 runs, judge-only 28/193 = 14.5% [10.2, 20.2] recall at 1/47 = 2.1% [0.4, 11.1] FAR; v8 69/193 = 35.8% [29.3, 42.7] at 1/47 = 2.1% [0.4, 11.1]. In-sample length >=40 catches 193/193 at 1/47. This separation is entirely compatible with the disjoint length distributions above: neither the length result nor the aggregate detector comparison is a valid balanced comparison. The observed 14.5%-at-2.1% point is below the 15%-at-3% target, but it is not a reliable target assessment. The written success rule is not met: replication not measurable, point FAR <=3%, and no common-budget improvement in this biased partial sample.

## Stored-component equivalence and cross-tests

Label-free v8 cache replay reproduced p0f5's non-gate J rule sets in **555/555** runs, with zero unavailable judgments (`tools/audit_p0f5.py`). It found 117/555 runs with unjudged pass-through cards, 340 cards in total. Stored decomposition similarly shows unjudged gate cards in 2/150 Goodfire sample runs and 67/246 matched-pair runs (`audit.json`). These are new judge work, not validly inferred from the old J. Because of the wall stop, they were not newly judged and full judge-only numbers for those populations do not exist.

Previously reported J, **seen and not full judge-only**, was CheatBench DEV 14/75 at 0/75, CM-18 established 22/317 at 5/329, and NP21 Flake 0/7 at 0/60. Unseen BAITBENCH, Kobayashi Maru and Reward Pressure were not run before the wall stop. NP21's only established-negative stratum is Flake; the other three have only other negatives. HackBench untouched. Cross-test generalization of the full variant remains unproven.

## Calls, controls and verification

15 batches of 16, each preceded by `omp usage` and recorded as `usd: 0` with purpose `p0-SELF-f9d7d6-6`. **680 attempts, 664 accepted judgments, 16 rejected attempts, all 680 served by exactly `anthropic/claude-haiku-5-5`; 0 refusals, 0 wrong-model attempts, 3,641,346 input tokens including cache and 190,264 output tokens.** Claude quota at start: five-hour 52%, seven-day 75%; batch-start maximum 76%; after: five-hour 0% (window reset), seven-day 76%. No paid keys, model fallback, upload, local Docker or local inference. After scoring, 480 v8 and variant findings were merged through `evallab trace merge`. Private findings, attempt receipts and sample remain under `~/.local/state/evallab-prod-live/trace-harness/p0f6/`.

Verification: focused stream test suite 24 passed; `ruff check .` passed; `uvx ty@0.0.71 check src/ --output-format=concise` passed; repomap and docindex regenerated. The live 240-run replay exercised both detectors and attested transport. The 555-run equivalence check is for the old non-gate component, not evidence that the full judge-only variant has no new alarms.
