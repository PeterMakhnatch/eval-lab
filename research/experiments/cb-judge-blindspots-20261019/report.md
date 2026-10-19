# Frozen judge on fresh CheatBench rule blind spots

Primary `reward_hacking`; cells show alarms/items = rate [Wilson 95%]. Paired-parsed cohort.

| Benchmark | Positive mechanism | Judge full | Judge actions | Frozen rule | Rule OR full | Rule OR actions |
|---|---|---:|---:|---:|---:|---:|
| cybench | secret_discovery_via_web_search | 3/3 = 100.0% [43.9–100.0] | 1/3 = 33.3% [6.2–79.2] | 0/3 = 0.0% [0.0–56.1] | 3/3 = 100.0% [43.9–100.0] | 1/3 = 33.3% [6.2–79.2] |
| hal-usaco | harness_leaks_answer | 0/25 = 0.0% [0.0–13.3] | 0/25 = 0.0% [0.0–13.3] | 0/25 = 0.0% [0.0–13.3] | 0/25 = 0.0% [0.0–13.3] | 0/25 = 0.0% [0.0–13.3] |
| kernelbench | verifier_exploitation | 0/3 = 0.0% [0.0–56.1] | 0/3 = 0.0% [0.0–56.1] | 0/3 = 0.0% [0.0–56.1] | 0/3 = 0.0% [0.0–56.1] | 0/3 = 0.0% [0.0–56.1] |
| swe-bench-verified | secret_discovery_in_environment | 9/20 = 45.0% [25.8–65.8] | 6/20 = 30.0% [14.5–51.9] | 20/20 = 100.0% [83.9–100.0] | 20/20 = 100.0% [83.9–100.0] | 20/20 = 100.0% [83.9–100.0] |
| swe-rebench | secret_discovery_in_environment | 3/8 = 37.5% [13.7–69.4] | 3/8 = 37.5% [13.7–69.4] | 0/8 = 0.0% [0.0–32.4] | 3/8 = 37.5% [13.7–69.4] | 3/8 = 37.5% [13.7–69.4] |
| swe-smith | verifier_exploitation | 4/4 = 100.0% [51.0–100.0] | 4/4 = 100.0% [51.0–100.0] | 0/4 = 0.0% [0.0–49.0] | 4/4 = 100.0% [51.0–100.0] | 4/4 = 100.0% [51.0–100.0] |
| terminal-bench-2 | harness_leaks_answer | 9/41 = 21.9% [12.0–36.7] | 9/41 = 21.9% [12.0–36.7] | 0/41 = 0.0% [0.0–8.6] | 9/41 = 21.9% [12.0–36.7] | 9/41 = 21.9% [12.0–36.7] |
| terminal-bench-2 | harness_leaks_verifier | 2/20 = 10.0% [2.8–30.1] | 6/20 = 30.0% [14.5–51.9] | 20/20 = 100.0% [83.9–100.0] | 20/20 = 100.0% [83.9–100.0] | 20/20 = 100.0% [83.9–100.0] |
| terminal-bench-2 | secret_discovery_via_web_search | 1/2 = 50.0% [9.4–90.5] | 1/2 = 50.0% [9.4–90.5] | 0/2 = 0.0% [0.0–65.8] | 1/2 = 50.0% [9.4–90.5] | 1/2 = 50.0% [9.4–90.5] |
| terminal-bench-2 | verifier_exploitation | 1/1 = 100.0% [20.6–100.0] | 1/1 = 100.0% [20.6–100.0] | 0/1 = 0.0% [0.0–79.3] | 1/1 = 100.0% [20.6–100.0] | 1/1 = 100.0% [20.6–100.0] |

## Same-benchmark false alarms (primary head)

| Benchmark | Judge full | Judge actions | Frozen rule | Rule OR full | Rule OR actions |
|---|---:|---:|---:|---:|---:|
| cybench | 0/14 = 0.0% [0.0–21.5] | 0/14 = 0.0% [0.0–21.5] | 1/14 = 7.1% [1.3–31.5] | 1/14 = 7.1% [1.3–31.5] | 1/14 = 7.1% [1.3–31.5] |
| hal-usaco | unmeasured (no negatives) | unmeasured (no negatives) | unmeasured (no negatives) | unmeasured (no negatives) | unmeasured (no negatives) |
| kernelbench | 0/9 = 0.0% [0.0–29.9] | 0/9 = 0.0% [0.0–29.9] | 0/9 = 0.0% [0.0–29.9] | 0/9 = 0.0% [0.0–29.9] | 0/9 = 0.0% [0.0–29.9] |
| swe-bench-verified | 2/15 = 13.3% [3.7–37.9] | 0/15 = 0.0% [0.0–20.4] | 1/15 = 6.7% [1.2–29.8] | 3/15 = 20.0% [7.0–45.2] | 1/15 = 6.7% [1.2–29.8] |
| swe-rebench | 6/14 = 42.9% [21.4–67.4] | 7/14 = 50.0% [26.8–73.2] | 0/14 = 0.0% [0.0–21.5] | 6/14 = 42.9% [21.4–67.4] | 7/14 = 50.0% [26.8–73.2] |
| swe-smith | 0/9 = 0.0% [0.0–29.9] | 0/9 = 0.0% [0.0–29.9] | 0/9 = 0.0% [0.0–29.9] | 0/9 = 0.0% [0.0–29.9] | 0/9 = 0.0% [0.0–29.9] |
| terminal-bench-2 | 3/29 = 10.3% [3.6–26.4] | 6/29 = 20.7% [9.8–38.4] | 3/29 = 10.3% [3.6–26.4] | 6/29 = 20.7% [9.8–38.4] | 9/29 = 31.0% [17.3–49.2] |

## Benchmark recall and rule unions (primary head)

| Benchmark | Judge full | Judge actions | Frozen rule | Rule OR full | Rule OR actions |
|---|---:|---:|---:|---:|---:|
| cybench | 3/3 = 100.0% [43.9–100.0] | 1/3 = 33.3% [6.2–79.2] | 0/3 = 0.0% [0.0–56.1] | 3/3 = 100.0% [43.9–100.0] | 1/3 = 33.3% [6.2–79.2] |
| hal-usaco | 0/25 = 0.0% [0.0–13.3] | 0/25 = 0.0% [0.0–13.3] | 0/25 = 0.0% [0.0–13.3] | 0/25 = 0.0% [0.0–13.3] | 0/25 = 0.0% [0.0–13.3] |
| kernelbench | 0/3 = 0.0% [0.0–56.1] | 0/3 = 0.0% [0.0–56.1] | 0/3 = 0.0% [0.0–56.1] | 0/3 = 0.0% [0.0–56.1] | 0/3 = 0.0% [0.0–56.1] |
| swe-bench-verified | 9/20 = 45.0% [25.8–65.8] | 6/20 = 30.0% [14.5–51.9] | 20/20 = 100.0% [83.9–100.0] | 20/20 = 100.0% [83.9–100.0] | 20/20 = 100.0% [83.9–100.0] |
| swe-rebench | 3/8 = 37.5% [13.7–69.4] | 3/8 = 37.5% [13.7–69.4] | 0/8 = 0.0% [0.0–32.4] | 3/8 = 37.5% [13.7–69.4] | 3/8 = 37.5% [13.7–69.4] |
| swe-smith | 4/4 = 100.0% [51.0–100.0] | 4/4 = 100.0% [51.0–100.0] | 0/4 = 0.0% [0.0–49.0] | 4/4 = 100.0% [51.0–100.0] | 4/4 = 100.0% [51.0–100.0] |
| terminal-bench-2 | 13/64 = 20.3% [12.3–31.7] | 17/64 = 26.6% [17.3–38.5] | 20/64 = 31.2% [21.2–43.4] | 31/64 = 48.4% [36.6–60.4] | 31/64 = 48.4% [36.6–60.4] |

## Paired full-versus-actions comparisons

Exact two-sided McNemar; descriptive, no multiplicity adjustment. n is paired items; full-only/actions-only are discordant flags.

| Head | Benchmark | Class | n | Full-only | Actions-only | Δ actions − full (pp) | Exact p |
|---|---|---|---:|---:|---:|---:|---:|
| reward_hacking | cybench | recall | 3 | 2 | 0 | -66.7 | 0.5 |
| reward_hacking | cybench | false_alarm | 14 | 0 | 0 | +0.0 | 1.0 |
| reward_hacking | hal-usaco | recall | 25 | 0 | 0 | +0.0 | 1.0 |
| reward_hacking | hal-usaco | false_alarm | 0 | 0 | 0 | n/a | not tested |
| reward_hacking | kernelbench | recall | 3 | 0 | 0 | +0.0 | 1.0 |
| reward_hacking | kernelbench | false_alarm | 9 | 0 | 0 | +0.0 | 1.0 |
| reward_hacking | swe-bench-verified | recall | 20 | 3 | 0 | -15.0 | 0.25 |
| reward_hacking | swe-bench-verified | false_alarm | 15 | 2 | 0 | -13.3 | 0.5 |
| reward_hacking | swe-rebench | recall | 8 | 2 | 2 | +0.0 | 1.0 |
| reward_hacking | swe-rebench | false_alarm | 14 | 1 | 2 | +7.1 | 1.0 |
| reward_hacking | swe-smith | recall | 4 | 0 | 0 | +0.0 | 1.0 |
| reward_hacking | swe-smith | false_alarm | 9 | 0 | 0 | +0.0 | 1.0 |
| reward_hacking | terminal-bench-2 | recall | 64 | 3 | 7 | +6.2 | 0.34375 |
| reward_hacking | terminal-bench-2 | false_alarm | 29 | 0 | 3 | +10.3 | 0.25 |
| any_finding | cybench | recall | 3 | 2 | 0 | -66.7 | 0.5 |
| any_finding | cybench | false_alarm | 14 | 0 | 2 | +14.3 | 0.5 |
| any_finding | hal-usaco | recall | 25 | 1 | 20 | +76.0 | 2.1e-05 |
| any_finding | hal-usaco | false_alarm | 0 | 0 | 0 | n/a | not tested |
| any_finding | kernelbench | recall | 3 | 1 | 0 | -33.3 | 1.0 |
| any_finding | kernelbench | false_alarm | 9 | 5 | 0 | -55.6 | 0.0625 |
| any_finding | swe-bench-verified | recall | 20 | 3 | 3 | +0.0 | 1.0 |
| any_finding | swe-bench-verified | false_alarm | 15 | 1 | 2 | +6.7 | 1.0 |
| any_finding | swe-rebench | recall | 8 | 4 | 0 | -50.0 | 0.125 |
| any_finding | swe-rebench | false_alarm | 14 | 4 | 0 | -28.6 | 0.125 |
| any_finding | swe-smith | recall | 4 | 0 | 0 | +0.0 | 1.0 |
| any_finding | swe-smith | false_alarm | 9 | 0 | 2 | +22.2 | 0.5 |
| any_finding | terminal-bench-2 | recall | 64 | 8 | 10 | +3.1 | 0.814529 |
| any_finding | terminal-bench-2 | false_alarm | 29 | 3 | 4 | +3.4 | 1.0 |

## Secondary any_finding head

This broader head flags any asked defect, not only reward hacking; it is not selected post hoc as the winner.

| Benchmark | Positive mechanism | Judge full | Judge actions | Frozen rule | Rule OR full | Rule OR actions |
|---|---|---:|---:|---:|---:|---:|
| cybench | secret_discovery_via_web_search | 3/3 = 100.0% [43.9–100.0] | 1/3 = 33.3% [6.2–79.2] | 0/3 = 0.0% [0.0–56.1] | 3/3 = 100.0% [43.9–100.0] | 1/3 = 33.3% [6.2–79.2] |
| hal-usaco | harness_leaks_answer | 3/25 = 12.0% [4.2–30.0] | 22/25 = 88.0% [70.0–95.8] | 0/25 = 0.0% [0.0–13.3] | 3/25 = 12.0% [4.2–30.0] | 22/25 = 88.0% [70.0–95.8] |
| kernelbench | verifier_exploitation | 1/3 = 33.3% [6.2–79.2] | 0/3 = 0.0% [0.0–56.1] | 0/3 = 0.0% [0.0–56.1] | 1/3 = 33.3% [6.2–79.2] | 0/3 = 0.0% [0.0–56.1] |
| swe-bench-verified | secret_discovery_in_environment | 9/20 = 45.0% [25.8–65.8] | 9/20 = 45.0% [25.8–65.8] | 20/20 = 100.0% [83.9–100.0] | 20/20 = 100.0% [83.9–100.0] | 20/20 = 100.0% [83.9–100.0] |
| swe-rebench | secret_discovery_in_environment | 8/8 = 100.0% [67.6–100.0] | 4/8 = 50.0% [21.5–78.5] | 0/8 = 0.0% [0.0–32.4] | 8/8 = 100.0% [67.6–100.0] | 4/8 = 50.0% [21.5–78.5] |
| swe-smith | verifier_exploitation | 4/4 = 100.0% [51.0–100.0] | 4/4 = 100.0% [51.0–100.0] | 0/4 = 0.0% [0.0–49.0] | 4/4 = 100.0% [51.0–100.0] | 4/4 = 100.0% [51.0–100.0] |
| terminal-bench-2 | harness_leaks_answer | 28/41 = 68.3% [53.0–80.4] | 26/41 = 63.4% [48.1–76.4] | 0/41 = 0.0% [0.0–8.6] | 28/41 = 68.3% [53.0–80.4] | 26/41 = 63.4% [48.1–76.4] |
| terminal-bench-2 | harness_leaks_verifier | 7/20 = 35.0% [18.1–56.7] | 10/20 = 50.0% [29.9–70.1] | 20/20 = 100.0% [83.9–100.0] | 20/20 = 100.0% [83.9–100.0] | 20/20 = 100.0% [83.9–100.0] |
| terminal-bench-2 | secret_discovery_via_web_search | 1/2 = 50.0% [9.4–90.5] | 2/2 = 100.0% [34.2–100.0] | 0/2 = 0.0% [0.0–65.8] | 1/2 = 50.0% [9.4–90.5] | 2/2 = 100.0% [34.2–100.0] |
| terminal-bench-2 | verifier_exploitation | 1/1 = 100.0% [20.6–100.0] | 1/1 = 100.0% [20.6–100.0] | 0/1 = 0.0% [0.0–79.3] | 1/1 = 100.0% [20.6–100.0] | 1/1 = 100.0% [20.6–100.0] |

| Benchmark | any_finding full FA | any_finding actions FA | Rule FA | Rule OR full FA | Rule OR actions FA |
|---|---:|---:|---:|---:|---:|
| cybench | 0/14 = 0.0% [0.0–21.5] | 2/14 = 14.3% [4.0–39.9] | 1/14 = 7.1% [1.3–31.5] | 1/14 = 7.1% [1.3–31.5] | 3/14 = 21.4% [7.6–47.6] |
| hal-usaco | unmeasured (no negatives) | unmeasured (no negatives) | unmeasured (no negatives) | unmeasured (no negatives) | unmeasured (no negatives) |
| kernelbench | 5/9 = 55.6% [26.7–81.1] | 0/9 = 0.0% [0.0–29.9] | 0/9 = 0.0% [0.0–29.9] | 5/9 = 55.6% [26.7–81.1] | 0/9 = 0.0% [0.0–29.9] |
| swe-bench-verified | 6/15 = 40.0% [19.8–64.2] | 7/15 = 46.7% [24.8–69.9] | 1/15 = 6.7% [1.2–29.8] | 6/15 = 40.0% [19.8–64.2] | 8/15 = 53.3% [30.1–75.2] |
| swe-rebench | 14/14 = 100.0% [78.5–100.0] | 10/14 = 71.4% [45.4–88.3] | 0/14 = 0.0% [0.0–21.5] | 14/14 = 100.0% [78.5–100.0] | 10/14 = 71.4% [45.4–88.3] |
| swe-smith | 1/9 = 11.1% [2.0–43.5] | 3/9 = 33.3% [12.1–64.6] | 0/9 = 0.0% [0.0–29.9] | 1/9 = 11.1% [2.0–43.5] | 3/9 = 33.3% [12.1–64.6] |
| terminal-bench-2 | 9/29 = 31.0% [17.3–49.2] | 10/29 = 34.5% [19.9–52.6] | 3/29 = 10.3% [3.6–26.4] | 12/29 = 41.4% [25.5–59.3] | 13/29 = 44.8% [28.4–62.5] |

## Findings

1. **Observed — recovery depends on the benchmark/mechanism.** The primary judge recovered all four swe-smith verifier-exploitation positives and the single terminal-bench-2 verifier-exploitation positive in both views, but none of the three kernelbench positives. For cybench web-search positives, full caught 3/3 and actions 1/3, with 0/14 same-benchmark false alarms in either view. These small positive cells have wide intervals; 100% here is not a stable general recall estimate.
2. **Observed — swe-rebench recovery comes with a very high same-benchmark alarm rate.** Both views caught 3/8 secret-discovery positives, but also flagged 6/14 benign traces in full and 7/14 in actions. The rule had 0/8 recall and 0/14 false alarms. The judge's positive hit rate (37.5%) was no higher than its negative alarm rate (42.9%/50.0%) on this sample.
3. **Observed — answer leakage remains a primary-head blind spot.** Neither view caught any of the 25 sampled hal-usaco answer leaks; both caught only 9/41 terminal-bench-2 answer leaks. The broader secondary head flagged 3/25 hal-usaco items in full and 22/25 in actions, but this is not reward-hacking-head success, and hal-usaco has no negatives with which to judge specificity. On terminal-bench-2 the secondary head caught more answer leaks (28/41 full, 26/41 actions), alongside more same-benchmark false alarms (9/29 and 10/29).
4. **Observed — the rule remains stronger on its known mechanisms, while the union buys some extra coverage.** The rule caught 20/20 SWE-bench Verified environment-discovery and 20/20 terminal-bench-2 verifier-leak positives; judge full/actions caught only 9/20 and 6/20, and 2/20 and 6/20, respectively. On terminal-bench-2 overall, rule-OR-judge increased recall from 20/64 to 31/64 in either view, but false alarms rose from 3/29 for the rule to 6/29 with full or 9/29 with actions.
5. **Observed — actions-only did not impose a uniform false-alarm penalty.** Primary terminal-bench-2 false alarms doubled from 3/29 to 6/29 (three actions-only discordant flags, exact p = 0.25); swe-rebench rose from 6/14 to 7/14 (p = 1.0), whereas SWE-bench Verified fell from 2/15 to 0/15 (p = 0.5). All other measurable primary benchmark false-alarm counts were zero in both views. No primary-head benchmark/class McNemar comparison reached p < 0.05; these are small, descriptive tests, not evidence of equivalent views.
6. **[INFERENCE] — use the judge as a mechanism-dependent complement, not a generic repair for the cheap screen.** This sample supports further investigation of the swe-smith and web-search recoveries, not replacing the rule or pooling benchmarks into one reassuring false-alarm number. Broadening to `any_finding` can change the apparent story sharply and often raises false alarms, so it does not establish a reward-hacking detector win. Fresh-item sampling, stochastic single calls, trace elision, small cells, and the missing hal-usaco negative population limit generalization.

## Execution, cost, and limits

- Planned 217 items; both views parsed for 217; unmeasured item list is in results.json (0 items).
- Metered spend **$0.245277 / $0.90** from 434 ledger requests; 0 transport errors; 0 immediate retry requests; 0 completion-pass requests.
- Successful provider counts: {'DeepInfra': 434}. All successful requests carried OpenRouter usage.cost; no free-route substitution or local inference.
- Pre-call freeze: `2026-10-19T12:05:08.117504+00:00`. Final prediction seal: `2026-10-19T12:38:31.339031+00:00`. Scoring: `2026-10-19T12:38:35.515421+00:00` (UTC, strict order checked before reading score labels).
- Paired trace-budget elision: {'full': 43, 'actions': 40}. Full/actions differences include evidence-budget differences, not only prose deletion.
- Sampling labels were accessed only to construct declared strata. The label-free run and transport retries preceded the sealed scoring join; this is not an assertion that sampling was label-free.
- Raw outputs, prompts, source projections, parity receipt, initial/final free estimates, and request ledger are outside git in ~/Developer/eval-lab-results/2026-10-19/cb-judge-blindspots/.
- Both published views matched canonical bytes on five key items. The fresh cohort excludes all 300 key items, but the judge and benchmark families are previously exposed; this is fresh-item transfer, not unseen-benchmark generalization.
- hal-usaco has no labelled negatives. Small mechanism cells have wide intervals. Negatives are source-vetted benign, not task-correctness certificates. Precision and prevalence are not estimated.
- results.json includes all-planned sensitivity with missing/unparsed responses counted as no alarm; these are not measured clean decisions. The primary tables use only paired-parsed items.
- The wrapper restricts immediate retries to content-free transport failures, a stricter retry policy than #1099's malformed-JSON retry allowance. No prompt/route/head tuning occurred.

## Post-seal edit

After sealing, `run.py` received two lint-only fixes from `ruff --fix`: an unused `time` import was removed and `datetime.timezone.utc` became `datetime.UTC`. Its sealed SHA-256 is `c482396cd43a8391bd8d2811dcb3ad90847608adc0670dc86bcb524443d4dd6e`; the committed file is `93273e942bc067560679ad665eb546ea87eb06e2ddb8a8b45ea6e77a724b3bb8`. No other sealed file changed.
