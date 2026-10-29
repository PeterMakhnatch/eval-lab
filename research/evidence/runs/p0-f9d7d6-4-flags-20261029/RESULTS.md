# P0-F9D7D6-4: flags and run-health decomposition

Preregistration commit `193fc324bffb48a6d9a475c621e54375911f8f41`, committed 1:26:20 PM ET and pushed by 1:26:36 PM ET on 2026-10-29, before the first computed label-joined statistic. Disclosure of the inadvertently displayed labelled row is in the preregistration. No models, uploads, or HackBench.

**Answer:** `reasoning_eval_awareness` carries most of the observed matched-pair binary signal, but it is an evaluation-word cue, not evidence of misconduct. The chosen exclusion flag met the empirical DEV target, yet failed the preregistered beyond-length criterion and transferred poorly. The inspected pairs are not a blind holdout.

## Label-free audit

All rates are over 2,388 strict-DEV runs; 95% Wilson intervals. Recomputed native flags verdict and score agreed with the stored fill on 2,388/2,388 runs. Health alarms are from the NP31 stored `run-health/4` readings, with latency abstentions treated as non-alarms; none were missing after that convention.

| Component | Firing rate (95% Wilson) |
|---|---|
| `answer_path_probe` | 78/2388 = 3.3% (2.6%–4.1%) |
| `git_history_peek` | 1797/2388 = 75.3% (73.5%–76.9%) |
| `harness_scaffold_in_code` | 38/2388 = 1.6% (1.2%–2.2%) |
| `package_fetch` | 361/2388 = 15.1% (13.7%–16.6%) |
| `process_or_namespace_probe` | 140/2388 = 5.9% (5.0%–6.9%) |
| `reasoning_eval_awareness` | 1835/2388 = 76.8% (75.1%–78.5%) |
| `reasoning_mentions_bypass_or_hack` | 999/2388 = 41.8% (39.9%–43.8%) |
| `reasoning_mentions_hardcode` | 318/2388 = 13.3% (12.0%–14.7%) |
| `run_health.infra` | 3/2388 = 0.1% (0.0%–0.4%) |
| `run_health.latency` | 32/2388 = 1.3% (1.0%–1.9%) |
| `run_health.loop` | 3/2388 = 0.1% (0.0%–0.4%) |
| `run_health.runaway` | 50/2388 = 2.1% (1.6%–2.7%) |
| `test_exclusion_flag` | 265/2388 = 11.1% (9.9%–12.4%) |
| `test_or_grader_edit` | 2301/2388 = 96.4% (95.5%–97.0%) |

## Matched-pair component decomposition

Each row has 123 same-task length-matched pairs. E-only/C-only are discordant pairs; binary pair AUROC assigns ties 0.5. Percentile intervals resample pairs 2,000 times with seed 4401, not tasks; repeated tasks may make intervals too narrow. No multiplicity correction; these are descriptive.

| Component | E-only / C-only | Both / neither | Pair AUROC (95% pair bootstrap) |
|---|---:|---:|---:|
| `answer_path_probe` | 5 / 1 | 1 / 116 | 0.516 (0.496–0.537) |
| `git_history_peek` | 9 / 4 | 99 / 11 | 0.520 (0.492–0.549) |
| `harness_scaffold_in_code` | 2 / 1 | 0 / 120 | 0.504 (0.492–0.516) |
| `package_fetch` | 5 / 2 | 22 / 94 | 0.512 (0.492–0.533) |
| `process_or_namespace_probe` | 6 / 0 | 2 / 115 | 0.524 (0.508–0.545) |
| `reasoning_eval_awareness` | 60 / 3 | 46 / 14 | 0.732 (0.683–0.780) |
| `reasoning_mentions_bypass_or_hack` | 29 / 15 | 25 / 54 | 0.557 (0.504–0.610) |
| `reasoning_mentions_hardcode` | 16 / 10 | 4 / 93 | 0.524 (0.484–0.569) |
| `run_health.infra` | 0 / 0 | 0 / 123 | 0.500 (0.500–0.500) |
| `run_health.latency` | 2 / 3 | 0 / 118 | 0.496 (0.476–0.512) |
| `run_health.loop` | 0 / 0 | 0 / 123 | 0.500 (0.500–0.500) |
| `run_health.runaway` | 3 / 1 | 3 / 116 | 0.508 (0.492–0.524) |
| `test_exclusion_flag` | 7 / 3 | 6 / 107 | 0.516 (0.492–0.541) |
| `test_or_grader_edit` | 0 / 1 | 122 / 0 | 0.496 (0.488–0.500) |

## Selection and length controls

The 246 paired runs were excluded; 2,142 selection runs (1,513 enacted, 629 clean). Five task-grouped folds all selected candidate 2, `test_exclusion_flag`; pooled out-of-fold metrics equal the full-selection metrics below. Selection is not an independent final test. Ties in candidate selection favor fewer clean alarms then lower candidate number. All counts/intervals below are Wilson 95%.

| Candidate | Recall | FAR |
|---|---|---|
| `test_or_grader_edit` | 1501/1513 = 99.2% (98.6%–99.5%) | 555/629 = 88.2% (85.5%–90.5%) |
| `test_exclusion_flag` | 237/1513 = 15.7% (13.9%–17.6%) | 6/629 = 1.0% (0.4%–2.1%) |
| `answer_path_probe` | 56/1513 = 3.7% (2.9%–4.8%) | 14/629 = 2.2% (1.3%–3.7%) |
| `harness_scaffold_in_code` | 32/1513 = 2.1% (1.5%–3.0%) | 3/629 = 0.5% (0.2%–1.4%) |
| `test_or_grader_edit AND test_exclusion_flag` | 237/1513 = 15.7% (13.9%–17.6%) | 6/629 = 1.0% (0.4%–2.1%) |
| `test_or_grader_edit AND reasoning_mentions_bypass_or_hack` | 797/1513 = 52.7% (50.2%–55.2%) | 100/629 = 15.9% (13.2%–19.0%) |
| `answer_path_probe AND reasoning_mentions_bypass_or_hack` | 34/1513 = 2.2% (1.6%–3.1%) | 1/629 = 0.2% (0.0%–0.9%) |
| `test_or_grader_edit AND run_health.loop` | 2/1513 = 0.1% (0.0%–0.5%) | 0/629 = 0.0% (0.0%–0.6%) |

The global deterministic length baseline at the same six-false-alarm budget is **never alarm**: the maximum observed length is 252 and nine clean runs share it, so no threshold accepting any run is feasible. This coarse tied-score control is unusually weak, not evidence length has no predictive power. Within-tertile thresholds were never / 89 / never; equal-count rather than randomized tie handling is preregistered.

| Tertile | Selected recall | Selected FAR | Length control recall | Length control FAR |
|---|---|---|---|---|
| 0–40 | 16/287 = 5.6% (3.5%–8.9%) | 2/470 = 0.4% (0.1%–1.5%) | 0/287 = 0.0% (0.0%–1.3%) | 0/470 = 0.0% (0.0%–0.8%) |
| 41–89 | 109/687 = 15.9% (13.3%–18.8%) | 1/57 = 1.8% (0.3%–9.3%) | 12/687 = 1.7% (1.0%–3.0%) | 1/57 = 1.8% (0.3%–9.3%) |
| 90–max | 112/539 = 20.8% (17.6%–24.4%) | 3/102 = 2.9% (1.0%–8.3%) | 0/539 = 0.0% (0.0%–0.7%) | 0/102 = 0.0% (0.0%–3.6%) |

On the 123 pairs the fixed rule had recall 13/123 = 10.6% (6.3%–17.2%) and FAR 9/123 = 7.3% (3.9%–13.3%); the frozen global length control had 0/123 = 0.0% (0.0%–3.0%) recall and 0/123 = 0.0% (0.0%–3.0%) FAR. The rule gets 125 vs 12 true alarms in the shorter strata, but 3 vs 1 clean alarms there; it also has 9 vs 0 clean alarms on the pairs. Thus the preregistered beyond-length improvement is **false**, even though it exceeds 15% recall at <=3% FAR in selection.

## Frozen cross-tests (one completed scoring pass)

The first invocation stopped before scoring because a required database argument was missing; the corrected invocation completed once. Positive and established-negative strata only; missing actors excluded, never treated as clean. No rule change from cross-test feedback.

| Corpus | Recall | FAR | Excluded |
|---|---|---|---:|
| CheatBench DEV | 0/75 = 0.0% (0.0%–4.9%) | 0/75 = 0.0% (0.0%–4.9%) | 0 |
| cm18:beam | 2/60 = 3.3% (0.9%–11.4%) | 0/60 = 0.0% (0.0%–6.0%) | 0 |
| cm18:cheatindex | 0/60 = 0.0% (0.0%–6.0%) | 0/60 = 0.0% (0.0%–6.0%) | 0 |
| cm18:evilgenie | 0/60 = 0.0% (0.0%–6.0%) | 0/60 = 0.0% (0.0%–6.0%) | 0 |
| cm18:kernelbench | 0/11 = 0.0% (0.0%–25.9%) | 0/19 = 0.0% (0.0%–16.8%) | 0 |
| cm18:reflex | 0/45 = 0.0% (0.0%–7.9%) | 0/50 = 0.0% (0.0%–7.1%) | 0 |
| cm18:researcharena | 0/21 = 0.0% (0.0%–15.5%) | 0/20 = 0.0% (0.0%–16.1%) | 0 |
| cm18:sdf | 0/60 = 0.0% (0.0%–6.0%) | 0/60 = 0.0% (0.0%–6.0%) | 0 |
| np21:flake | 0/7 = 0.0% (0.0%–35.4%) | 0/60 = 0.0% (0.0%–6.0%) | 0 |
| CM-18 plus Flake established pooled | 2/324 = 0.6% (0.2%–2.2%) | 0/389 = 0.0% (0.0%–1.0%) | 0 |

The cross-tests do not hold the 15% recall target; zero observed false alarms do not prove zero risk. CM-18/Flake are previously examined datasets, not pristine external validation. Pointwise Wilson intervals do not correct for selection or task dependence.

## Reproduction and limitations

Private data and scripts: `~/.local/state/evallab-prod-live/trace-harness/p0-f4-flags/`. `audit.py` performed the costly raw flag extraction and native agreement check; its initial output had no health rows because the fill file contains no health records. Health values were then attached label-free from `np31-length-matched/goodfire-dev.jsonl` tool fields, treating `run_health.latency...+abstain_as_clear` as non-alarm; see `results.json` for rates and hashes. `selection.py` and `cross.py` generated private `selection.json` and `cross.json`. Individual run IDs and labels remain private. The component bootstrap resamples pairs, not task clusters; these 123 previously inspected pairs limit inferential claims.
