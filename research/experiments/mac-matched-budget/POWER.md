# Power plan: one matched-budget MAC Bench comparison

## Decision and unit

**INFERRED.** Start with `team-2` against `silent-2` on Holdout. Use the same 30-minute limit, total assigned model cap, model, harness and seats. Permit communication only in `team-2`. Seal the silent communication paths. Do not compare against a solo arm for the primary communication claim. Solo also changes the number of agents and the game. Treat `k = 4` as a separate design, not a second chance to reject the same null. Select one `k` before the trial.

**INFERRED.** Use one pair per independent block. Fix the world instance or seed within a block. Randomly assign the two conditions to the two isolated trial slots. Interleave the arms in time. Do not use shared world state across trials. Record seeds, trial slots, starts, scores, model spend and sandbox spend. Use the exact 30-minute mark. If either score is missing, the block is unmeasured. Do not count it as zero. Apply the same replacement rule to both arms. Stop on failures that differ by arm or cluster in time. The power model does not cover them.

**INFERRED.** Estimate the difference in mean score, `team - silent`. Test the sharp null with a two-sided sign-flip test. Go only if the test rejects and the difference is positive. This is a decision to pursue a benefit, not proof that the benefit is at least the planning effect. Do not infer equivalence from non-rejection. Treat 15 minutes, solo and other tasks as descriptive unless a new plan states a test family.

**INFERRED.** Match assigned caps before assignment. Report realized spend separately. Do not discard scored blocks by realized spend. That spend can change because of the treatment. If the arms miss the prespecified realized-spend tolerance, report an equal-cap comparison, not an equal-spend result.

## Sources and limits

| Key | Root |
|---|---|
| C | `~/Developer/eval-lab-results/multiagent-campaign-2026-11-23` |
| L | `~/Developer/research-context/multi-agent` |
| E | `~/Developer/eval-lab/.worktrees/fable-map` |
| R | `~/Developer/muster-runs` |
| O | `~/Developer/muster-runs/day-2026-11-24/mac-power` |

**OBSERVED.** The pilot uses Claude Haiku 5.5 and Claude Code on Daytona. It grades at 15 and 30 minutes (L `benchmarks/2026-11-23-bespoke-holdout-daytona-and-zero-fork-guards.md:94-114`). The normal game lasts 4 hours (L `benchmarks/mac-bench-bespoke-deep-dive.md:76`). The pilot reached the third game day. It does not measure the later game or another model.

**OBSERVED.** The library gives a mean coordination effect of −0.018 score units and a reported 95% interval of [−0.038, +0.003] (L `benchmarks/mac-bench-bespoke-deep-dive.md:19-20`). The scaling recomputation gives −0.0175 over 36 task × model cells (L `2026-11-23-agent-count-scaling-designs-and-evidence.md:58-62`). The same source uses a different set of 27 cells for some interval estimates. None of these is a trial SD. The mean across tasks does not provide a variance for a single Holdout comparison.

**OBSERVED.** E `tidegauge/commands/plan.py:31-42,69-72` and `tidegauge/stats.py:493-546` use within-task variances and a normal approximation. The plan gives attempts per arm. It does not account for pairing, the discrete sign-flip distribution, attrition or repeated looks. E `research/experiments/muster-boxes/PREREGISTRATION.md:199-207,816-829` pairs by instance, uses a two-sided exact sign flip and uses Holm for the E8 family of 4. One primary MAC Bench contrast needs no Holm adjustment. The sequential rule still needs a total alpha limit across looks.

## Holdout pilot scores

**OBSERVED.** The rows below report the checkpoint score. The source is C `bespoke/study4-holdout/summary.json`. L `benchmarks/2026-11-23-bespoke-holdout-daytona-and-zero-fork-guards.md:114-129` cross-checks the rows. The 15- and 30-minute scores from one trial share a world. They are not independent measurements. The pilot does not establish randomized pairs across arms. Do not infer a correlation from the `-1` and `-2` suffixes.

**OBSERVED.** All Holdout runs use one map and seed (`~/Developer/research-context/sources/links/benchmarks.bespokelabs.ai/2026-11-19-holdout.md:51`). C `analyze_campaign.py:72,528,548` matches the pilot trials by launch-wave suffix after the run. At 30 minutes those 2 differences are +0.001778 and −0.002263. This is not a randomized pair design. Use launch slots, not artificial seed differences, for a future pair on this fixed world.

| Trial | 15 minutes | 30 minutes | C file lines |
|---|---:|---:|---|
| `solo-1` | 0.030000 | unknown; final 0.110000 at 28.4 min | 1186–1189; L line 114 |
| `solo-2` | 0.030000 | 0.086400 | 1252–1253 |
| `silent-1` | 0.077200 | 0.121500 | 1319–1320 |
| `silent-2` | 0.062867 | 0.119190 | 1392–1393 |
| `team-1` | 0.080533 | 0.123278 | 1464–1465 |
| `team-2` | 0.084867 | 0.116927 | 1536–1537 |
| `guard-1` | 0.084667 | 0.118091 | 1607–1608 |
| `guard-2` | 0.082800 | 0.110000 | 1681–1682 |

| Arm | Mark | n | Mean | Sample SD | Range |
|---|---|---:|---:|---:|---|
| Solo | 15 min | 2 | 0.030000 | 0.000000 | 0.030000–0.030000 |
| Solo | 30 min | 1 | 0.086400 | unknown | 0.086400–0.086400 |
| Solo | final (28.4 / 30 min) | 2 | 0.098200 | 0.016688 | 0.086400–0.110000 |
| Silent | 15 min | 2 | 0.070034 | 0.010135 | 0.062867–0.077200 |
| Silent | 30 min | 2 | 0.120345 | 0.001633 | 0.119190–0.121500 |
| Team | 15 min | 2 | 0.082700 | 0.003065 | 0.080533–0.084867 |
| Team | 30 min | 2 | 0.120103 | 0.004491 | 0.116927–0.123278 |
| Guard | 15 min | 2 | 0.083734 | 0.001320 | 0.082800–0.084667 |
| Guard | 30 min | 2 | 0.114046 | 0.005721 | 0.110000–0.118091 |

**OBSERVED.** The arm summaries can differ by 0.000001 from arithmetic on the printed trial scores. The table uses the printed trial scores. The checkpoint for `solo-1` is missing, not zero. No study arm has enough trials to establish a stable variance.

## Published score spread

**OBSERVED.** The publisher fixes time, not total model spend. The public `8 agents without coordination` label mixes two controls. Holdout, Mindustry and Dubai ATC use silent teams. Other tasks use single runs with selection by the hidden score. A best-of-eight maximum is one selection statistic, not 8 scores for a silent team. The main chart can disagree with a task-page comparator. See L `benchmarks/mac-bench-bespoke-deep-dive.md:116-147`.

**OBSERVED.** O `public-condition-spread.tsv` has 1,003 condition-and-mark summaries with the source line of every score. The main chart has 517 plotted runs across 12 tasks. See L `sources/posts/2026-11-18-mac-bench-public-result-data.md:18-26`. The other captures overlap the chart. Do not add their counts. SOTA Compressor's task-page capture gives 27 multi-agent runs, 9 primary solo runs and 3 pools of 8 solo final scores that include the primary runs. See L `sources/posts/2026-11-18-sota-compressor-task-data.md:54-517,654-663,812-821,970-979`. Seed to Profit's index gives 45 runs with 5 marks each. See L `sources/posts/2026-11-18-seed-to-profit-public-rollout.md:19-32`. O `published-aggregates.md` retains the deep-dive and scaling tables with line numbers.

**OBSERVED.** Some task-page summaries include more solos than the main chart. See L `benchmarks/mac-bench-bespoke-deep-dive.md:131-147`. Do not derive a range for an unavailable solo pool from its maximum. The 1 scored silent run per model for Dubai ATC gives no SD. See the main chart, lines 1747–1775.

**OBSERVED.** Public Holdout 30-minute team SDs at `k = 2` are 0.0092, 0.0020 and 0.0124 for Opus, Sol and Argon. Each has 3 runs; L `sources/posts/2026-11-18-mac-bench-public-result-data.md:872-960`. Public `k = 4` SDs are 0.0045, 0.0024 and 0.0034, also with 3 runs each (lines 962–1050). Neither size has a silent control in those results. These checkpoints come from longer runs with other models, not matched-budget Haiku trials.

**OBSERVED.** The public Power Outage page states that the same 48 hidden scenarios grade every checkpoint. The score of a launch is the mean across those scenarios. The 48 scenarios are not 48 independent launches. See `~/Developer/research-context/sources/links/benchmarks.bespokelabs.ai/2026-11-19-power-outage.md:63-87`. The chart gives no pairing key across conditions.

| Power Outage, Opus 5.5, 240 minutes | n | Mean | Sample SD | Range | L `sources/posts/2026-11-18-mac-bench-public-result-data.md` |
|---|---:|---:|---:|---|---|
| Primary solo | 3 | 0.220433 | 0.163714 | 0.0864–0.4029 | 3550, 3560, 3570 |
| Team of 2 | 3 | 0.305800 | 0.123310 | 0.1816–0.4282 | 3640, 3650, 3660 |

**INFERRED.** Use these SDs for a second-task sensitivity only. Simulate `team-2` vs one solo attempt under equal assigned caps, not the published best-of-eight selector. This changes how agents divide information as well as whether they communicate. It is not a pure communication contrast. The constrained-arm SDs and sandbox prices are unknown.

**OBSERVED.** Mindustry supplies a same-world comparison. Argon at 240 minutes has 3 score observations per arm. Team-8 has mean 0.188067, SD 0.050092 and range 0.1445–0.2428. Silent-8 has mean 0.129533, SD 0.019288 and range 0.1180–0.1518. See L `sources/posts/2026-11-18-mac-bench-public-result-data.md:4387-4415,4477-4505`. The published model costs are $94.67 and $83.33 per trial, respectively. These are means of 3 list-price amounts per arm (L `sources/posts/2026-11-18-multi-agent-coordination-bench.md:2912-2914,2921-2923`). The silent setup and matched total cost remain unknown.

## Cost and attrition assumptions

**OBSERVED.** The 8 clean trials cost $1.1403 in model spend and $1.8801 in sandbox spend. See C `spend-ledger.jsonl:44-46,55-59` and L `benchmarks/2026-11-23-bespoke-holdout-daytona-and-zero-fork-guards.md:155-166`. Among the 11 attempts after the 2 canaries, 3 abort at startup in one provider-balance incident. Two other trials score before a later HTTP 402. See L lines 146–151. The aborts do not estimate independent attrition.

**INFERRED.** Use the requested 8-trial pooled inputs: $0.1425375 model and $0.2350125 sandbox per arm at `k = 2`. The `k = 2` sandbox value mixes 2 solo trials with 6 two-seat trials. The actual two-seat sandbox means for `silent` and `team` were $0.2589 and $0.2592. Use those in a cost sensitivity check. The equal model cap is an assumption, not the observed spend in both arms.

**INFERRED.** Keep model spend constant at `k = 4` to match the total assigned cap. Scale sandbox spend by `5 / 3`: 1 hub plus 4 seats instead of 1 hub plus 2 seats. This gives $0.3916875 per arm. It assumes the same rates and time for every sandbox. It does not establish that a team of 4 can use the same total budget effectively. Use the `k = 2` score spread only as a proxy for `k = 4`.

**OBSERVED.** The Daytona tool estimates cost from recorded sandbox lifetimes and rates. It is not an invoice (`~/Developer/bespoke-macbench-lab/run_hardened.py:19,233-262`). **INFERRED.** Scaling the actual 2-seat mean to 5 sandboxes gives about $0.432 per trial. The pooled projection is $0.392.

**INFERRED.** Set per-arm independent attrition to 5% for the planning example. A complete pair has probability 0.9025. Count both paid trials in an incomplete pair. Quote expected cost separately from the funded attempt limit. Establish the real cap, including cleanup and spend reserve, before paying. Do not treat these cost estimates as price guarantees.

## Planner and sequential rule

**INFERRED.** Simulate independent Gaussian block differences. Use `SD² = SD_control² + SD_team² − 2ρ SD_control SD_team`. Set `ρ = 0` because the pilot does not estimate it. Power needs a rejection in the assumed effect direction. The null check counts both directions. Real scores are bounded and can tie. The model does not capture that.

**OBSERVED.** `power_plan.py` counts all sign patterns for up to 22 complete pairs. It checks the count against `tidegauge` on 24 seeded integer vectors. Above 22 it uses random sign patterns and the add-one p value. It uses Welch's test from `tidegauge` for a separate unpaired design. The planner scans small sample sizes. For larger sizes it estimates the first crossing by bisection. Monte Carlo error and non-monotone power can move that crossing. The reported simulation error does not include uncertainty in the SDs.

**INFERRED.** For a sequential design, fix the complete-pair targets and paid-start cap before assignment. Split total `α = 0.05` across the 3 looks: 0.010, 0.015 and 0.025. Do not reuse unused alpha. At each look test the signed sum with the two-sided sign flip. Stop for a result if the test rejects. A positive result can pass the go rule; a negative result cannot. If the test does not reject and the interim difference is negative, stop for futility. Do not call that equivalence.

**INFERRED.** If the paid-start cap arrives before the next target, use only that look's alpha on available complete pairs, then stop. This uses no more tests or alpha than the registered plan. The overall false-positive rate is at most 0.05 under block exchangeability and independent score loss. The simulation checks it with and without the futility rule. If those assumptions fail, this bound does not justify a decision.

**INFERRED.** Fund paired or unpaired fixed designs to reach the target scored count with probability at least 0.95. Conditional power describes the result given that count. Sequential power includes paid attempts that fail to reach a look. The fixed plan is simpler and has the lower cap at the 10% Holdout target.

## Holdout estimates

**INFERRED.** This table uses 6,000 simulations per estimate with seed `3017`. Relative effects use the stored silent mean of 0.120345. Each block starts 2 trials. Fixed costs use the start cap that gives at least a 95% chance of collecting the fixed target. Sequential power includes the start cap.

Fixed entries give **complete pairs / paid-start cap / cap cost**. Sequential entries give **complete-pair cap / paid-start cap / expected cost at zero effect / expected cost at the assumed effect / cap cost**. All costs are in USD.

| k | Relative effect (score units) | Fixed 80% | Fixed 90% | Sequential 80% | Sequential 90% | Sequential false positives 80% / 90% |
|---:|---:|---|---|---|---|---|
| 2 | 5% (0.00601725) | 8 / 11 / $8.31 | 10 / 13 / $9.82 | 10 / 13 / $5.55 / $8.30 / $9.82 | 11 / 14 / $5.92 / $8.13 / $10.57 | 1.15% / 1.43% |
| 2 | 10% (0.0120345) | 6 / 8 / $6.04 | 6 / 8 / $6.04 | 7 / 9 / $3.97 / $5.80 / $6.80 | 7 / 9 / $3.97 / $5.80 / $6.80 | 0.75% / 0.75% |
| 2 | 20% (0.024069) | 6 / 8 / $6.04 | 6 / 8 / $6.04 | 7 / 9 / $3.97 / $5.80 / $6.80 | 7 / 9 / $3.97 / $5.80 / $6.80 | 0.75% / 0.75% |
| 2 | 30% (0.0361035) | 6 / 8 / $6.04 | 6 / 8 / $6.04 | 7 / 9 / $3.97 / $5.80 / $6.80 | 7 / 9 / $3.97 / $5.80 / $6.80 | 0.75% / 0.75% |
| 4 | 5% (0.00601725) | 8 / 11 / $11.75 | 10 / 13 / $13.89 | 10 / 13 / $7.85 / $11.74 / $13.89 | 11 / 14 / $8.38 / $11.50 / $14.96 | 1.15% / 1.43% |
| 4 | 10% (0.0120345) | 6 / 8 / $8.55 | 6 / 8 / $8.55 | 7 / 9 / $5.62 / $8.21 / $9.62 | 7 / 9 / $5.62 / $8.21 / $9.62 | 0.75% / 0.75% |
| 4 | 20% (0.024069) | 6 / 8 / $8.55 | 6 / 8 / $8.55 | 7 / 9 / $5.62 / $8.21 / $9.62 | 7 / 9 / $5.62 / $8.21 / $9.62 | 0.75% / 0.75% |
| 4 | 30% (0.0361035) | 6 / 8 / $8.55 | 6 / 8 / $8.55 | 7 / 9 / $5.62 / $8.21 / $9.62 | 7 / 9 / $5.62 / $8.21 / $9.62 | 0.75% / 0.75% |

**INFERRED.** At the 10% target, the sequential looks use 3, 5 and 7 complete pairs. The first 2 looks can stop for futility, but their exact tests cannot reject. At 9 starts the simulated power is 91.2%. At zero effect the simulated rejection rate is 0.75%, or 1.60% without futility. The fixed 8-start plan needs less reserved spend. Its pooled cost splits into $2.28 model and $3.76 sandbox.

**INFERRED.** At 12 starts, the 5% effect gives 95.1% paired power and 97.4% unpaired Welch power. The 80% and 90% minimum effects are 0.004608 and 0.005367 for the paired test. They are 0.004307 and 0.004967 for Welch. For the 10% effect, Welch estimates 4 scored trials per arm for either target. This costs $3.78 for 5 starts per arm at pooled rates. Do not choose Welch on this estimate alone. Its power assumes independent Gaussian scores and depends on 2 SDs estimated from 2 trials per arm.

**INFERRED.** With 2× the pilot SDs, the 10% target needs 8 or 10 complete pairs for 80% or 90% fixed power. Fund 11 or 13 starts for $8.31 or $9.82 at pooled rates. With 4× the SDs, the estimates rise to 22 or 30 complete pairs. Fund 27 or 37 starts for $20.39 or $27.94. These sensitivity scenarios use 2,000 draws.

## Other tasks: spread sensitivity only

**INFERRED.** Use 5% per-arm attrition and the public 4-hour SDs. The results below do not show what the score spread will be under matched model caps.

| Task and target gain | Fixed complete pairs at 80% / 90% | Fixed paid-start cap at 80% / 90% | Sequential complete-pair cap at 80% / 90% | Sequential paid-start cap at 80% / 90% | Sequential expected starts, null / effect (80%) |
|---|---:|---:|---:|---:|---:|
| Mindustry Argon `team-8` vs `silent-8`, 30% (0.03886) | 18 / 23 | 23 / 28 | 22 / 27 | 27 / 33 | 15.40 / 19.02 |
| Power Outage Opus `team-2` vs solo, 30% (0.06613) | 78 / 103 | 92 / 120 | 95 / 125 | 111 / 145 | 64.58 / 77.69 |

**OBSERVED.** The Power Outage published model costs average $36 for solo and $86 for `team-2`. Each mean comes from 3 list-price costs (L `sources/posts/2026-11-18-multi-agent-coordination-bench.md:2711-2713,2720-2722`).

**INFERRED.** Total fixed and sequential costs for both tasks are unknown. Multiply the starts by both arm costs once the equal model cap and sandbox prices are known. For example, at a common $36 model cap, the 92-start Power Outage plan reserves $6,624 for model calls alone. Matching the price does not establish that the observed spread will persist. Holdout is the cheaper grounded candidate.

## Pilot gate

**INFERRED.** The main unknown is the SD of the 30-minute score difference in real randomized pairs at the same model cap. The pilot gives 2 scores per arm and no estimate of the covariance. At zero correlation it implies an SD of 0.004779. It does not rule out an SD several times as large. Progress can stop at the same early milestone in both arms.

**INFERRED.** Run a separate pilot only if a 30-minute result can change a decision. Use 12 complete `team-2` / `silent-2` blocks, or 24 scored trials. Budget for no more than 16 starts, or 32 paid trials. At 5% independent attrition per trial, this reaches 12 complete blocks with probability 0.985. The expected cost is $10.04 with pooled rates, or $10.68 with the actual two-seat sandbox rates and a matched model cap. The 16-start estimates are $12.08 and $12.85, respectively. The pilot does not test for efficacy.

**INFERRED.** Freeze the smallest useful effect before the pilot. Use 0.0120345 score units (10% of the stored 30-minute control mean) for this plan. Inspect the paired SD, ties, score range, observed spend, model-cap enforcement and missing blocks. Recompute power at the upper plausible paired SD. For a Gaussian sensitivity, a one-sided 95% upper bound is about 1.55 times the sample SD across 12 pairs. If the scores have many ties, use a simulation with those ties as well. Require 80% or more power after the attempt limit is applied. Use fresh blocks for the efficacy test. Do not pay for a 4-hour conclusion from a 30-minute variance estimate.

**INFERRED.** With fewer than 6 nonzero complete blocks, the minimum attainable two-sided exact p is greater than 0.05. No result can pass the fixed rule. At the pooled rates, that floor is $4.53 rounded ($4.5306, 12 scored trials) for `k = 2` and $6.41 rounded ($6.4107) for `k = 4`. At the observed two-seat sandbox rates, the `k = 2` floor is $4.82. These are no-loss, model-based cost floors. A budget above the floor does not establish adequate power.

**INFERRED.** Under the pilot spread and the 10% target, fixed power is 96.5% on 6 complete blocks. Only 6 starts give a 54.0% chance to collect 6 complete blocks. Up to 7 starts give 82.7% power including attrition. This costs at most $5.29 at pooled rates, or $5.62 at the actual two-seat sandbox rates. Up to 8 starts give 93.1% power and cost at most $6.04 or $6.43. If the cap arrives first, make no efficacy decision. Use 8 starts for the recommended plan: it also meets the 95% target for collecting all 6 blocks.

**INFERRED.** Below the cost of 7 starts, no design at this 5% attrition rate can meet 80% power on the 6-block exact test. At 6 starts, even arbitrarily large effects cannot compensate for missing blocks. Below the cost of 6 starts, positive evidence cannot pass at all. These limits do not apply to a different endpoint or a different test.

## Backtests on stored synthetic comparisons

**OBSERVED.** Both primary examples use 10 paired instance means and 2 trials per instance and arm, not 20 independent pairs. The control scores 1 in every instance. The paired SD thus equals the team SD, and the correlation is undefined. `rho = 0` gives the same variance as any correlation input in the planner.

| Stored comparison | Team / control mean | Team / control sample SD | Team − control | Exact p | Paired / Welch power | Observed |
|---|---|---|---:|---:|---|---|
| E5 `team-8` vs `solo-24` | 0.5425 / 1 | 0.044175 / 0 | −0.4575 | 0.001953125 | 100.0% / 100.0% | Reject at 0.05; secondary |
| E8 `stale-t` vs `stale-c` | 0.10 / 1 | 0.210819 / 0 | −0.90 | 0.001953125 | 100.0% / 100.0% | Holm rank 1 of 4 passes 0.0125; interval meets the rule |
| E5 `claim-2` vs `solo-24` | 0.9825 / 1 | 0.039176 / 0 | −0.0175 | 0.25 | 23.6% / 24.7% | Do not reject; 7 of 10 differences are zero |

**OBSERVED.** Sources: E `research/experiments/muster-boxes/RESULTS.md:191-209,337-342`; R `e5/analysis/e5-full.txt:124-129,144-147`; R `night-2026-11-24/e8/analysis/e8.txt:156-161`. The corresponding JSON files retain each instance mean. `stored-backtests-and-ledger.json` records them and recomputes all 1,024 sign patterns per comparison. The E8 interval is [75, 100] score points in the registered `control - team` direction.

**INFERRED.** Power and p are not the same quantity. Power is the chance of rejection across repeated experiments at the assumed effect. A backtest compares a predicted probability with one observed rejection. The two strong effects check behavior for large signals. They cannot establish calibration. The third case checks the report on a weak, tied sample.
