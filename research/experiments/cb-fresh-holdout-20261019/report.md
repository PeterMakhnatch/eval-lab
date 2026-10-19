# Frozen detectors on fresh, key-excluded CheatBench traces

| pool | arm | recall [95% CI] | false alarms [95% CI] | precision [95% CI] |
|---|---|---|---|---|
| test-family | tripwire | 2/47 = 4.3% [1.2, 14.2] | 13/885 = 1.5% [0.9, 2.5] | 2/15 = 13.3% [3.7, 37.9] |
| test-family | rule | 36/47 = 76.6% [62.8, 86.4] | 49/885 = 5.5% [4.2, 7.2] | 36/85 = 42.4% [32.4, 53.0] |
| test-family | logistic_frozen | 36/47 = 76.6% [62.8, 86.4] | 583/885 = 65.9% [62.7, 68.9] | 36/619 = 5.8% [4.2, 7.9] |
| test-family | tripwire_shell_alias (secondary) | 5/47 = 10.6% [4.6, 22.6] | 35/885 = 4.0% [2.9, 5.5] | 5/40 = 12.5% [5.5, 26.1] |
| test-family | logistic_oracle (secondary) | 18/47 = 38.3% [25.8, 52.6] | 44/885 = 5.0% [3.7, 6.6] | 18/62 = 29.0% [19.2, 41.3] |
| dev-family | tripwire | 400/669 = 59.8% [56.0, 63.4] | 204/2348 = 8.7% [7.6, 9.9] | 400/604 = 66.2% [62.4, 69.9] |
| dev-family | rule | 400/669 = 59.8% [56.0, 63.4] | 220/2348 = 9.4% [8.3, 10.6] | 400/620 = 64.5% [60.7, 68.2] |
| dev-family | logistic_frozen | 609/669 = 91.0% [88.6, 93.0] | 616/2348 = 26.2% [24.5, 28.1] | 609/1225 = 49.7% [46.9, 52.5] |
| dev-family | tripwire_shell_alias (secondary) | 400/669 = 59.8% [56.0, 63.4] | 204/2348 = 8.7% [7.6, 9.9] | 400/604 = 66.2% [62.4, 69.9] |
| dev-family | logistic_oracle (secondary) | 377/669 = 56.4% [52.6, 60.1] | 76/2348 = 3.2% [2.6, 4.0] | 377/453 = 83.2% [79.5, 86.4] |

All rates show Wilson 95% intervals in percentage points. LR oracle and shell-alias rows are secondary.

Cohort: 4,268 source ids − all 300 key ids = **3,968 fresh traces**. All get predictions; **19 unknown-label rows** are excluded from every metric, including oracle threshold calibration. Metrics use **3,949 labelled rows** (test-family 47/885 positive/negative; dev-family 669/2,348). Unknown ids are listed in results.json.

## Side by side with #1091 key test

| arm | #1091 key test: recall / FA / precision | fresh test-family: recall / FA / precision | fresh dev-family: recall / FA / precision |
|---|---|---|---|
| tripwire | 11/75 = 14.7% / 2/75 = 2.7% / 11/13 = 84.6% | 2/47 = 4.3% / 13/885 = 1.5% / 2/15 = 13.3% | 400/669 = 59.8% / 204/2348 = 8.7% / 400/604 = 66.2% |
| rule | 62/75 = 82.7% / 6/75 = 8.0% / 62/68 = 91.2% | 36/47 = 76.6% / 49/885 = 5.5% / 36/85 = 42.4% | 400/669 = 59.8% / 220/2348 = 9.4% / 400/620 = 64.5% |
| logistic_frozen | 43/75 = 57.3% / 13/75 = 17.3% / 43/56 = 76.8% | 36/47 = 76.6% / 583/885 = 65.9% / 36/619 = 5.8% | 609/669 = 91.0% / 616/2348 = 26.2% / 609/1225 = 49.7% |
| tripwire_shell_alias | 16/75 = 21.3% / 5/75 = 6.7% / 16/21 = 76.2% | 5/47 = 10.6% / 35/885 = 4.0% / 5/40 = 12.5% | 400/669 = 59.8% / 204/2348 = 8.7% / 400/604 = 66.2% |
| logistic_oracle | 20/75 = 26.7% / 3/75 = 4.0% / 20/23 = 87.0% | 18/47 = 38.3% / 44/885 = 5.0% / 18/62 = 29.0% | 377/669 = 56.4% / 76/2348 = 3.2% / 377/453 = 83.2% |

The key-test split is balanced (75/75); its precision is not a natural-population precision estimate. Key oracle uses key-test negatives; fresh oracle uses each fresh pool's own negatives.

| cohort | LR AUROC [bootstrap 95%] | positive base rate [Wilson 95%] | bootstrap clusters |
|---|---|---|---|
| #1091 key test | 0.7276 [0.6487, 0.8055] | 75/150 = 50.0% (balanced design) | 150 (published item-cluster interval) |
| test-family | 0.6939 [0.5116, 0.7404] | 47/932 = 5.0% [3.8, 6.6] | 83 |
| dev-family | 0.8971 [0.8646, 0.9238] | 669/3017 = 22.2% [20.7, 23.7] | 786 |

## Findings

1. **Observed: projection and frozen-arm controls pass.** 300/300 overlapping source/key items match the published actions bytes and features; 0 byte and 0 feature mismatches. All 300 published hashes and recorded per-item features, tripwire sets, shell-alias sets and LR scores match. Recovered coefficients, standardization and frozen threshold match exactly.
2. **Observed: the history/tripwire rule transfers within one held-out benchmark, not uniformly across its named mechanism.** It catches 36/36 = 100.0% environmental-secret positives in SWE-bench Verified, but 0/8 = 0.0% in SWE-rebench and 0/3 = 0.0% web-search positives in Cybench. **[INFERENCE]** The pooled test-family recall is not evidence of broad reward-hacking coverage; the same mechanism label does not guarantee sensitivity across benchmarks.
3. **Observed: dev-family rule hits are entirely verifier leaks.** Both tripwires and the rule catch all 400 terminal-bench-2 `harness_leaks_verifier` positives; the rule catches 0/269 across all other dev-family positive mechanisms, including 259 answer leaks. Adding history to tripwires adds no true positives in this pool, but adds 16 false alarms. **[INFERENCE]** These frozen cheap signals are useful mechanism-specific screens, not a general detector.
4. **Observed: LR ranking and threshold transfer tell different stories.** AUROC is 0.6939 on test-family and 0.8971 on dev-family, yet the unchanged logit cutoff 0.643298942264 produces false alarms on 583/885 = 65.9% and 616/2348 = 26.2% negatives, respectively. Fresh-negative oracle recall is only 18/47 = 38.3% and 377/669 = 56.4%. **[INFERENCE]** The frozen cutoff is not calibrated out of sample; oracle re-thresholding is calibration headroom, not a successful frozen detector.
5. **Observed: natural-base-rate precision is much lower than balanced key-test precision.** The rule has test-family precision 36/85 = 42.4% [32.4, 53.0] and dev-family precision 400/620 = 64.5% [60.7, 68.2], versus 62/68 = 91.2% on the balanced key test. **[INFERENCE]** Key-test precision alone would be a misleading deployment estimate because class balance and mechanisms differ.
6. **Observed: a disagreement audit is staged, not adjudicated.** 56 seeded, stratified rule disagreements are in `audit-sample.jsonl`, pointing only to existing blind trajectories. All 3968 fresh sources and 300 overlapping key sources have zero forbidden evaluator fields. **[INFERENCE]** Disagreements are candidate label-noise cases, not proof that the source label or detector is wrong.

## Positive recall by family / benchmark / mechanism

| family | benchmark | mechanism | tripwire | rule | frozen LR | shell alias (secondary) | oracle LR (secondary) |
|---|---|---|---|---|---|---|---|
| dev-family | hal-usaco | harness_leaks_answer | 0/218 = 0.0% [0.0, 1.7] | 0/218 = 0.0% [0.0, 1.7] | 218/218 = 100.0% [98.3, 100.0] | 0/218 = 0.0% [0.0, 1.7] | 0/218 = 0.0% [0.0, 1.7] |
| dev-family | kernelbench | verifier_exploitation | 0/3 = 0.0% [0.0, 56.1] | 0/3 = 0.0% [0.0, 56.1] | 3/3 = 100.0% [43.9, 100.0] | 0/3 = 0.0% [0.0, 56.1] | 0/3 = 0.0% [0.0, 56.1] |
| dev-family | swe-smith | verifier_exploitation | 0/4 = 0.0% [0.0, 49.0] | 0/4 = 0.0% [0.0, 49.0] | 4/4 = 100.0% [51.0, 100.0] | 0/4 = 0.0% [0.0, 49.0] | 0/4 = 0.0% [0.0, 49.0] |
| dev-family | terminal-bench-2 | harness_leaks_answer | 0/41 = 0.0% [0.0, 8.6] | 0/41 = 0.0% [0.0, 8.6] | 0/41 = 0.0% [0.0, 8.6] | 0/41 = 0.0% [0.0, 8.6] | 0/41 = 0.0% [0.0, 8.6] |
| dev-family | terminal-bench-2 | harness_leaks_verifier | 400/400 = 100.0% [99.0, 100.0] | 400/400 = 100.0% [99.0, 100.0] | 384/400 = 96.0% [93.6, 97.5] | 400/400 = 100.0% [99.0, 100.0] | 377/400 = 94.2% [91.5, 96.1] |
| dev-family | terminal-bench-2 | secret_discovery_via_web_search | 0/2 = 0.0% [0.0, 65.8] | 0/2 = 0.0% [0.0, 65.8] | 0/2 = 0.0% [0.0, 65.8] | 0/2 = 0.0% [0.0, 65.8] | 0/2 = 0.0% [0.0, 65.8] |
| dev-family | terminal-bench-2 | verifier_exploitation | 0/1 = 0.0% [0.0, 79.3] | 0/1 = 0.0% [0.0, 79.3] | 0/1 = 0.0% [0.0, 79.3] | 0/1 = 0.0% [0.0, 79.3] | 0/1 = 0.0% [0.0, 79.3] |
| test-family | cybench | secret_discovery_via_web_search | 0/3 = 0.0% [0.0, 56.1] | 0/3 = 0.0% [0.0, 56.1] | 3/3 = 100.0% [43.9, 100.0] | 0/3 = 0.0% [0.0, 56.1] | 0/3 = 0.0% [0.0, 56.1] |
| test-family | swe-bench-verified | secret_discovery_in_environment | 2/36 = 5.6% [1.5, 18.1] | 36/36 = 100.0% [90.4, 100.0] | 25/36 = 69.4% [53.1, 82.0] | 5/36 = 13.9% [6.1, 28.7] | 18/36 = 50.0% [34.5, 65.5] |
| test-family | swe-rebench | secret_discovery_in_environment | 0/8 = 0.0% [0.0, 32.4] | 0/8 = 0.0% [0.0, 32.4] | 8/8 = 100.0% [67.6, 100.0] | 0/8 = 0.0% [0.0, 32.4] | 0/8 = 0.0% [0.0, 32.4] |

## Negative false alarms by family / benchmark

| family | benchmark | tripwire | rule | frozen LR | shell alias (secondary) | oracle LR (secondary) |
|---|---|---|---|---|---|---|
| dev-family | bountybench | 4/55 = 7.3% [2.9, 17.3] | 4/55 = 7.3% [2.9, 17.3] | 47/55 = 85.5% [73.8, 92.4] | 4/55 = 7.3% [2.9, 17.3] | 0/55 = 0.0% [0.0, 6.5] |
| dev-family | kernelbench | 0/33 = 0.0% [0.0, 10.4] | 0/33 = 0.0% [0.0, 10.4] | 33/33 = 100.0% [89.6, 100.0] | 0/33 = 0.0% [0.0, 10.4] | 0/33 = 0.0% [0.0, 10.4] |
| dev-family | mle-bench | 2/128 = 1.6% [0.4, 5.5] | 2/128 = 1.6% [0.4, 5.5] | 22/128 = 17.2% [11.6, 24.7] | 2/128 = 1.6% [0.4, 5.5] | 0/128 = 0.0% [0.0, 2.9] |
| dev-family | swe-smith | 1/485 = 0.2% [0.0, 1.2] | 2/485 = 0.4% [0.1, 1.5] | 424/485 = 87.4% [84.2, 90.1] | 1/485 = 0.2% [0.0, 1.2] | 5/485 = 1.0% [0.4, 2.4] |
| dev-family | terminal-bench-2 | 197/1647 = 12.0% [10.5, 13.6] | 212/1647 = 12.9% [11.3, 14.6] | 90/1647 = 5.5% [4.5, 6.7] | 197/1647 = 12.0% [10.5, 13.6] | 71/1647 = 4.3% [3.4, 5.4] |
| test-family | cybench | 2/480 = 0.4% [0.1, 1.5] | 2/480 = 0.4% [0.1, 1.5] | 457/480 = 95.2% [92.9, 96.8] | 2/480 = 0.4% [0.1, 1.5] | 6/480 = 1.2% [0.6, 2.7] |
| test-family | swe-bench-verified | 11/365 = 3.0% [1.7, 5.3] | 47/365 = 12.9% [9.8, 16.7] | 86/365 = 23.6% [19.5, 28.2] | 33/365 = 9.0% [6.5, 12.4] | 38/365 = 10.4% [7.7, 14.0] |
| test-family | swe-rebench | 0/40 = 0.0% [0.0, 8.8] | 0/40 = 0.0% [0.0, 8.8] | 40/40 = 100.0% [91.2, 100.0] | 0/40 = 0.0% [0.0, 8.8] | 0/40 = 0.0% [0.0, 8.8] |

## Benchmark confound (post hoc)

Added after the first fresh results. Within-benchmark AUROC ranks only items of one benchmark. The identity baseline scores every item by its benchmark's positive rate in the same pool, an in-sample upper bound for a detector that only recognises which benchmark a trace came from.

| pool | frozen LR AUROC (pooled) | benchmark-identity AUROC (in-sample) | positives in benchmarks with no negatives |
|---|---|---|---|
| test-family | 0.694 | 0.757 | 0/47 = 0.0% |
| dev-family | 0.897 | 0.759 | 218/669 = 32.6% |

| family | benchmark | positives | negatives | frozen LR AUROC within benchmark |
|---|---|---|---|---|
| dev-family | bountybench | 0 | 55 | n/a (one class) |
| dev-family | hal-usaco | 218 | 0 | n/a (one class) |
| dev-family | kernelbench | 3 | 33 | 0.500 |
| dev-family | mle-bench | 0 | 128 | n/a (one class) |
| dev-family | swe-smith | 4 | 485 | 0.558 |
| dev-family | terminal-bench-2 | 444 | 1647 | 0.932 |
| test-family | cybench | 3 | 480 | 0.518 |
| test-family | swe-bench-verified | 36 | 365 | 0.786 |
| test-family | swe-rebench | 8 | 40 | 0.500 |

## Methods and receipts

- Pre-declaration SHA-256: `256398f42a991c18bedf98476c743b42b17b0ead25d0588878b48f3c5b41fbe7`; recorded before any fresh prediction.
- Frozen source commit: `7926981bee21f6efb5f992d8642311ce20ddec5d`. All hashes, exact model parameters, thresholds, controls, counts, bootstrap diagnostics and grouped rates are in `results.json`.
- Current main added `execute_bash` to the shared shell tools. This runner instead loads exact #1091 `edit_signals.py`, `tripwires.py`, and transfer code from the frozen Git commit; other detector dependency hashes must match. The shell-alias row remains the original #1091 normalization, not rules v2. No scoring logic was copied or changed.
- The frozen threshold targeted 5% FA on CB dev, not on the key test or fresh data (key-test FA reproduces 13/75). Oracle thresholds use only each fresh pool's labelled negatives. The original strict `score > cutoff` policy is retained; tied scores can make oracle FA lower than 5%, and ties are not randomized.
- No new learning, feature selection, rules, model calls, containers or submitted code execution; $0. Only the same CB-dev model is reconstructed as a control.
- Bootstrap: 2,000 valid resamples, seed 20261019, whole task-name clusters where the blind converter header identifies a real task; otherwise whole family x benchmark x mechanism strata. Header information never enters detector features. Task names are represented only by hashes in predictions. One-class draws are discarded and counted. Pool order is fixed in code.
- Source task names group repeated attempts on the same benchmark/task. Unidentified tasks share the declared stratum fallback, which can widen intervals. Wilson rate intervals treat items as binomial and do not correct within-task dependence.
- All 300 source ids are excluded, not just key-test items. Fresh means key-disjoint, not proven free of all prior corpus exposure; the historical calibration corpus included CheatBench, and three #1091 key-test positives were read before its rule was written.
- Actions-only removes agent prose and reasoning but retains user/tool context. Predictions contain numeric/step-cited evidence, not trajectory text. Source labels, mechanism and benchmark come only from the evaluator manifest.
- Auditor inputs: `audit-sample.jsonl` and its existing blind trajectory paths only. Do not give auditors the evaluator manifest, predictions, or aggregate result files as trajectory evidence.
- Execution: 454.80 seconds; 2026-10-19T11:21:18.344760+00:00 to 2026-10-19T11:28:53.150552+00:00. Source and projection rolling hashes cover every fresh item in stable CB-id order.
