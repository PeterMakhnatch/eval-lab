# Bespoke MAC Bench at equal nominal agent-hours

**Question.** What does a talking team score with the agent-hours of 1 full single-agent run? Does the public data answer the same question at equal model spend?

## Findings

1. **OBSERVED.** At 4 nominal agent-hours, teams of 2, 4 and 8 trail solo by 0.0483, 0.1067 and 0.1565 points. They lead in 13/36, 8/36 and 8/36 cells, respectively.
2. **OBSERVED.** The first qualifying mark comes strictly before 4 agent-hours in 4/36, 3/36 and 0/36 cells. Another 10/36, 6/36 and 8/36 first qualify at 4 agent-hours.
3. **OBSERVED.** Bespoke's interpolated times imply fewer than 4 agent-hours in 14/36, 9/36 and 8/36 cells. Interpolation, not another measurement, accounts for the extra cases.
4. **OBSERVED.** At size 8, the mean difference is +0.0458 on the 9 silent-control cells and −0.2239 on the 27 best-of-k cells. Those are team-minus-solo differences, not communication effects.
5. **OBSERVED.** Holdout teams lead solo in all 9 equal-hour comparisons (3 sizes × 3 models). Their mean leads range from 0.0054 to 0.0136 points by size.
6. **OBSERVED.** Dubai ATC has the largest task-mean lead at size 8 (+0.2812 over 3 models). Its early and final marks grade different sets of flights.
7. **OBSERVED.** Seed to Profit has the largest task-mean loss at every size. At sizes 4 and 8 its score is 0 in all 18 equal-hour team runs; the final solo mean is 0.4445.
8. **OBSERVED.** Bespoke's published control curve gives 0.2096 at 30 minutes and 0.4370 at 4 hours. The available plotted control rows give 0.1448 and 0.3382 across 29 cells.
9. **OBSERVED.** Opus gains 0.0499, 0.0100 and 0.0155 points per additional solo-cost unit on Bespoke's pooled cost axis. Marginal return does not fall at every doubling.
10. **OBSERVED.** The solo curve gains 0.0742, 0.0539 and 0.1156 points from hour 2 to hour 4 for Opus, Sol and Argon. The capture does not give those hours' incremental spend.
11. **OBSERVED.** Final points per model dollar fall at every doubling in 36/36 task-model cells on the captured prices. This does not compare scores at equal dollars.
12. **INFERRED.** The public curves cannot replace an enforced equal-cap, same-size Holdout test. None of the retained data gives cumulative dollars at the marks.

## Sources and calculation

**OBSERVED.** `L` is `~/Developer/research-context/multi-agent`. The reader takes these 4 Markdown captures from `L/sources/posts/`:

| File | Use | SHA-256 |
|---|---|---|
| `2026-11-18-mac-bench-public-result-data.md` | `runs`, `overtime`, `time`, `bysize`, `cost`, `scores`, `taskgains` from the [report](https://benchmarks.bespokelabs.ai/multi-agent-coordination-bench/) | `2c1331c8df45253aae006fd1efb2f3430546df3814a95b93b75688c45af7db78` |
| `2026-11-18-multi-agent-coordination-bench.md` | Methods and the 12 rollout tables with rounded final dollars, lines 2469–3374 | `92529ee216704667f87155d67a751bfff4b65ab6372543d025ff39fe30db0e6b` |
| `2026-11-18-sota-compressor-task-data.md` | [SOTA Compressor](https://benchmarks.bespokelabs.ai/multi-agent-coordination-bench/analysis/sota-compressor.html) task-page `runs` and `stats` | `4df4a8c84559cb1811b84d08df5f6a679a272c6710467bdb7169211d0e8b0b88` |
| `2026-11-18-seed-to-profit-public-rollout.md` | [Seed to Profit index](https://benchmarks.bespokelabs.ai/multi-agent-coordination-bench/public-rollout-data/index.json) at line 32; it gives final dollars and scores | `ca777d535d5c8c8befb476442ac4b05a81d4aa0d98aa601303b74e7d73e8971e` |

**OBSERVED.** The 12 readable task pages are at `~/Developer/research-context/sources/links/benchmarks.bespokelabs.ai/2026-11-19-<task>.md`. They retain methods and chart captions, not the embedded per-run arrays. The separate SOTA capture and Seed to Profit index supply the extra numeric detail above. See `L/benchmarks/mac-bench-bespoke-deep-dive.md:69–147` for task and comparator definitions. See `L/2026-11-23-agent-count-scaling-designs-and-evidence.md`, section 2, for the prior equal-wall-time coordination analysis. The analysis below does not repeat it.

**OBSERVED.** The chart has 517 rows in 12 tasks. A cell is a task and model, with that model's native harness. Compute each setting's mean over its plotted runs; do not pair runs by number. Each of the 36 cells gets equal weight, not each run. Most settings have 3 runs. Mindustry has 5 solo runs per model, Dubai ATC has 8 solo runs and 1 silent run per model, and Cave Rescue has 2 Opus team-8 runs. Control row counts vary further. The chart does not contain all supplemental solo runs.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 research/experiments/mac-matched-budget/analysis/public_equal_compute.py --json
```

**OBSERVED.** `equal-compute.json` in `~/Developer/muster-runs/day-2026-11-24/mac-public/` contains the output. It has each cell's scores, run counts and parity estimates. It also has subgroup summaries, final score per dollar by condition, and source rows for the dollar joins.

## 1. Equal nominal agent-hours

**OBSERVED.** Compare solo at the final mark with teams of 2 at 2 hours, 4 at 1 hour and 8 at 30 minutes. Each comparison uses nominal `k × wall hours = 4 agent-hours`. The last row uses the available chart rows labeled “8 agents without coordination.” Match the solo cells to those same tasks and models. For the interval, resample **tasks** with replacement, keeping their observed model cells together. The 95% percentile interval uses 10,000 draws, seed `20261124`. It describes variation across these tasks, not uncertainty from rerunning each condition or coverage of a defined task population. The silent subgroup has only 3 tasks.

| Setting | Cells | Solo final mean | Arm mark mean | Mean delta [95% task bootstrap] | Ahead |
|---|---:|---:|---:|---:|---:|
| Team 2 | 36 | .3497 | .3014 | −.0483 [−.1152, −.0049] | 13/36 |
| Team 4 | 36 | .3497 | .2430 | −.1067 [−.1836, −.0410] | 8/36 |
| Team 8 | 36 | .3497 | .1932 | −.1565 [−.2466, −.0526] | 8/36 |
| Plotted control 8 | 29 | .3021 | .1448 | −.1573 [−.2621, −.0390] | 4/29 |

**OBSERVED.** Group tasks by what the publisher uses as its final control. Silent: Holdout, Dubai ATC and Mindustry. Best-of-k: the other 9 tasks, including Power Outage; Cave Rescue Opus uses best-of-4 for the final comparison. Group membership is not a claim that the plotted control rows implement the reported selector.

| Task group | Team 2 Δ [95%], wins | Team 4 Δ [95%], wins | Team 8 Δ [95%], wins | Plotted control 8 Δ [95%], wins |
|---|---|---|---|---|
| Silent (3 tasks) | −.0069 [−.0445, +.0186], 5/9 | −.0038 [−.1325, +.1100], 5/9 | +.0458 [−.1574, +.2812], 6/9 | +.0323 [−.1596, +.2546], 4/9 |
| Best-of-k (9 tasks) | −.0621 [−.1474, −.0074], 8/27 | −.1410 [−.2259, −.0822], 3/27 | −.2239 [−.2948, −.1625], 2/27 | −.2426 [−.3269, −.1790], 0/20 |

**OBSERVED.** Cross-check against `overtime`. It has no size-2 or size-4 curve, task spread, or win count. The talking-team deltas agree with the row computation to the displayed precision, with a 0.0001 rounding difference for Opus.

| Publisher row | Solo final | Team 8 at 30 min | Δ | Control 8 at 30 min | Δ |
|---|---:|---:|---:|---:|---:|
| All | .3497 | .1932 | −.1565 | .2096 | −.1401 |
| Opus | .3871 | .2351 | −.1520 | .2065 | −.1806 |
| Sol | .3820 | .2461 | −.1359 | .3310 | −.0510 |
| Argon | .2800 | .0984 | −.1816 | .0914 | −.1886 |

**OBSERVED.** Keep both control totals. The publisher's control average is .2096 at 30 minutes versus .1448 for the 29 plotted cells. At final it is .4370 versus .3382 for those cells. The families and coverage differ. For SOTA alone, even the same 3 model cells give .7038 from plotted final controls, against .6406 from the task-page solo maxima. The latter agrees with `taskgains` (`one + more[0]`). The SOTA task page's actual selector curve at 30 minutes gives .5293/.3852/.0000 (Opus/Sol/Argon), below each model's solo final. The chart control row means are not a substitute for the published selector.

## 2. Agent-hours to the solo final score

**OBSERVED.** The target is the plotted solo final mean for that task-model cell. Read the first team mean at or above it, and the previous mark. Multiply both endpoints by team size. Use 0 as the earliest bound when the first mark qualifies. `NR` means no mark through the nominal 4-hour wall limit qualifies; the time stays unknown. These are brackets for a crossing detected at a mark. When scores can fall, earlier crossings between marks can be missed. The measure does not require a team to stay above the target.

| Task group | Size | First mark <4 agent-hours | At 4 | After 4 | NR | Publisher estimate <4 |
|---|---:|---:|---:|---:|---:|---:|
| All | 2 | 4/36 | 10/36 | 17/36 | 5/36 | 14/36 |
| All | 4 | 3/36 | 6/36 | 19/36 | 8/36 | 9/36 |
| All | 8 | 0/36 | 8/36 | 22/36 | 6/36 | 8/36 |
| Silent | 2 | 3/9 | 3/9 | 3/9 | 0/9 | 6/9 |
| Silent | 4 | 2/9 | 3/9 | 3/9 | 1/9 | 5/9 |
| Silent | 8 | 0/9 | 6/9 | 3/9 | 0/9 | 6/9 |
| Best-of-k | 2 | 1/27 | 7/27 | 14/27 | 5/27 | 8/27 |
| Best-of-k | 4 | 1/27 | 3/27 | 16/27 | 7/27 | 4/27 |
| Best-of-k | 8 | 0/27 | 2/27 | 19/27 | 6/27 | 2/27 |

**OBSERVED.** `time` agrees on all 19 `NR` cells across sizes and lies inside all 89 qualifying brackets. Local linear interpolation differs from the publisher estimates by less than 8 seconds of wall time in those 89 cells. That check assumes zero score at time 0; it does not observe a score then. Size 8 has no mark before the 4-agent-hour boundary, so 0/36 does not mean no team could reach it sooner. The publisher's 30/36 size-8 figure uses a 4-hour **wall** limit (32 nominal agent-hours), not 4 agent-hours.

### Cell table: equal-hour deltas and parity brackets

**OBSERVED.** Score deltas are points above or below that cell's solo final mean. `Control 8` is the plotted row mean at 30 minutes. A dash means no rows; a numeric 0 in the data is a score, not missing data. Parity brackets list agent-hours for sizes 2 / 4 / 8. The task order starts with the 3 silent-control tasks.

| Task | Model | Solo final | Δ team 2 | Δ team 4 | Δ team 8 | Δ control 8 | Parity agent-hours: 2 / 4 / 8 |
|---|---|---:|---:|---:|---:|---:|---|
| Holdout | Opus | .1319 | +.0057 | +.0155 | +.0215 | +.0055 | 2–4 / 2–4 / 0–4 |
| Holdout | Sol | .1289 | +.0042 | +.0017 | +.0061 | +.0058 | 2–4 / 2–4 / 0–4 |
| Holdout | Argon | .1240 | +.0062 | +.0164 | +.0132 | −.0054 | 2–4 / 2–4 / 0–4 |
| Dubai ATC | Opus | .2080 | −.0117 | +.1683 | +.1799 | −.0441 | 0–1 / 0–2 / 0–4 |
| Dubai ATC | Sol | .2353 | +.0429 | +.2297 | +.4945 | +.6161 | 0–1 / 0–2 / 0–4 |
| Dubai ATC | Argon | .1419 | +.0245 | −.0681 | +.1690 | +.1918 | 1–2 / NR / 0–4 |
| Mindustry | Opus | .3062 | −.0913 | −.2298 | −.2798 | −.2853 | 4–6 / 8–12 / 16–24 |
| Mindustry | Sol | .1303 | −.0372 | −.1019 | −.1138 | −.1185 | 4–6 / 8–12 / 8–16 |
| Mindustry | Argon | .0838 | −.0050 | −.0658 | −.0786 | −.0751 | 4–6 / 8–12 / 8–16 |
| Seed to Profit | Opus | .4201 | −.3598 | −.4201 | −.4201 | −.4201 | 6–8 / 12–16 / 24–32 |
| Seed to Profit | Sol | .4489 | −.4403 | −.4489 | −.4489 | −.4489 | NR / NR / NR |
| Seed to Profit | Argon | .4645 | −.3361 | −.4645 | −.4645 | −.4645 | 6–8 / 12–16 / 24–32 |
| SOTA Compressor | Opus | .6874 | +.0073 | −.0775 | −.1117 | −.1210 | 2–4 / 8–12 / 8–16 |
| SOTA Compressor | Sol | .5312 | −.0624 | +.0114 | −.2668 | −.1874 | 4–6 / 2–4 / 16–24 |
| SOTA Compressor | Argon | .4798 | −.0233 | −.1923 | −.4798 | −.3847 | 4–6 / 4–8 / 8–16 |
| Mars Return | Opus | .2603 | +.0178 | −.0730 | −.1927 | −.2027 | 2–4 / 4–8 / 16–24 |
| Mars Return | Sol | .1838 | −.0514 | +.0287 | −.0530 | −.1502 | 4–6 / 2–4 / 4–8 |
| Mars Return | Argon | .1903 | −.1292 | −.1528 | −.1451 | −.1082 | NR / NR / NR |
| 0 A.D. | Opus | .4400 | +.0157 | −.0269 | +.0237 | −.0994 | 2–4 / 4–8 / 0–4 |
| 0 A.D. | Sol | .4872 | −.0079 | −.0967 | −.0807 | −.1347 | NR / NR / NR |
| 0 A.D. | Argon | .2317 | −.0586 | −.0767 | −.1592 | −.2014 | NR / 12–16 / NR |
| Power Outage | Opus | .2204 | +.0394 | −.1535 | −.1897 | −.1895 | 2–4 / 8–12 / 4–8 |
| Power Outage | Sol | .4426 | −.0193 | −.1643 | −.3171 | −.3704 | 4–6 / NR / NR |
| Power Outage | Argon | .1561 | −.0070 | −.0952 | −.1352 | −.1509 | 4–6 / 8–12 / 8–16 |
| Cave Rescue | Opus | .5734 | −.0277 | −.3132 | −.3458 | — | 4–6 / 8–12 / 8–16 |
| Cave Rescue | Sol | .4952 | −.0229 | −.2130 | −.3939 | −.4700 | NR / 4–8 / NR |
| Cave Rescue | Argon | .1868 | +.1396 | −.0570 | +.0050 | −.1868 | 1–2 / 0–2 / 0–4 |
| Chicago Skyline Survey | Opus | .3244 | +.0106 | −.0667 | −.2630 | −.2246 | 2–4 / 8–12 / 8–16 |
| Chicago Skyline Survey | Sol | .3664 | −.0330 | −.1155 | −.1551 | −.1680 | 6–8 / NR / 16–24 |
| Chicago Skyline Survey | Argon | .2531 | −.0642 | −.1108 | −.1479 | −.1689 | 6–8 / 8–12 / 8–16 |
| RNA Secondary Structure | Opus | .5838 | +.0135 | +.0014 | −.0378 | — | 2–4 / 2–4 / 8–16 |
| RNA Secondary Structure | Sol | .6179 | −.0112 | −.0481 | −.1188 | — | 4–6 / NR / 16–24 |
| RNA Secondary Structure | Argon | .6179 | −.0165 | −.1163 | −.3344 | — | 4–6 / NR / 16–24 |
| Spectrum to Molecule | Opus | .4893 | +.0292 | −.0596 | −.2092 | — | 2–4 / 4–8 / 16–24 |
| Spectrum to Molecule | Sol | .5163 | −.0226 | −.0475 | −.1835 | — | 6–8 / 12–16 / 24–32 |
| Spectrum to Molecule | Argon | .4297 | −.2564 | −.2578 | −.4214 | — | 6–8 / 8–12 / 16–24 |

**OBSERVED.** On the mean across the 3 models, winners at size 2 are Cave Rescue (+.0297), Dubai ATC (+.0186), Holdout (+.0054) and Power Outage (+.0044). At size 4 they are Dubai ATC (+.1100) and Holdout (+.0112). At size 8 they are Dubai ATC (+.2812) and Holdout (+.0136). Seed to Profit loses most at each size (−.3787/−.4445/−.4445). The next-largest mean losses are Spectrum to Molecule at size 2 (−.0833), Cave Rescue at size 4 (−.1944), and SOTA Compressor at size 8 (−.2861). On the plotted control rows, only Dubai ATC (+.2546) and Holdout (+.0020) lead on the 3-model task mean.

## 3. Marginal return on the publisher's cost axis

**OBSERVED.** Subtract adjacent final scores in `bysize` and divide by adjacent `cost` differences: 1.1, 2.2 and 4.5 solo-cost units. The cost axis uses the geometric mean of within-cell price ratios across 36 cells. It is the same for all 3 models. These slopes are descriptive ratios of summaries, not model-specific price slopes or estimates from an intervention. For the task splits, substitute the plotted score means and retain the same cost axis. JSON gives the same calculation for each task; the shares of the real costs need not match it.

| Model or task group | 1→2 | 2→4 | 4→8 | Solo score gain, hour 2→4 | Solo slope if constant spend rate (INFERRED) |
|---|---:|---:|---:|---:|---:|
| Opus | .04991 | .00995 | .01553 | .0742 | .1484 |
| Sol | .00036 | .00577 | .00238 | .0539 | .1078 |
| Argon | .01182 | .00673 | .00242 | .1156 | .2312 |
| Silent (9 cells) | .03242 | .01523 | .01699 | .0328 | .0655 |
| Best-of-k (27 cells) | .01685 | .00487 | .00338 | .0974 | .1948 |

**INFERRED.** Extending 1 solo from hour 2 to 4 adds 2 agent-hours. Adding a second agent for the first 2 hours also adds 2 nominal agent-hours. Under constant spend rates, the former costs 0.50 of the 4-hour solo cost, and applying the publisher's ratio to 2-hour runs gives 0.55 for the latter. Neither assumption is measured. The observed team-size slopes use final scores, not a 2-hour price comparison.

## 4. Final score per model dollar, not equal dollars

**OBSERVED.** All 517 plotted rows join to final cost in the report tables by task, model, setting and run number. The printed final scores agree within 0.005 points. Use the more precise matching totals for 36 SOTA rows (`c`, also final `ah`) and 45 Seed to Profit rows (`metrics.costUsd`). The other 436 prices are rounded to dollars. SOTA's 3 plotted control rows are among those 436; keep the task-page oracle separate. The other 10 task pages have no retained per-run arrays. The report gives their final prices, not per-run agent-hours. None of the 12 tasks has retained cumulative spend at the score marks. The Seed rollout index has final per-agent counters, not a spend series. Do not scale final spend by elapsed time or infer model dollars from those token counters.

**OBSERVED.** For each condition and cell, divide the sum of final scores by the sum of final list-price model dollars. The table averages those cell ratios with equal weights and shows points per **$100**. It does not include sandbox or simulator costs. For each task the denominator is 3 model cells per column except where shown. JSON carries each cell's score, price, number of runs and ratio.

| Task or control group | Solo | Team 2 | Team 4 | Team 8 | Plotted control 8 |
|---|---:|---:|---:|---:|---:|
| All (36 cells; control 29) | 1.549 | .804 | .404 | .211 | .177 |
| Silent (9 cells) | .709 | .451 | .255 | .190 | .199 |
| Best-of-k (27 cells; plotted control 20) | 1.829 | .922 | .454 | .218 | .167 |
| Holdout | .804 | .529 | .372 | .292 | .290 |
| Dubai ATC | .465 | .281 | .110 | .056 | .113 |
| Mindustry | .858 | .543 | .285 | .223 | .195 |
| Seed to Profit | 1.881 | .895 | .438 | .225 | .286 |
| SOTA Compressor | 2.902 | 1.623 | .834 | .411 | .342 |
| Mars Return | .659 | .293 | .194 | .074 | .105 |
| 0 A.D. | 1.068 | .517 | .260 | .120 | .121 |
| Power Outage | .842 | .510 | .231 | .132 | .092 |
| Cave Rescue | .917 | .423 | .227 | .123 | .000 (2 cells) |
| Chicago Skyline Survey | 1.275 | .702 | .354 | .175 | .167 |
| RNA Secondary Structure | 3.703 | 1.762 | .826 | .372 | — |
| Spectrum to Molecule | 3.216 | 1.572 | .719 | .334 | — |

**OBSERVED.** Solo has a higher final ratio than team 2, 4 or 8 in each of the 36 matching cells, and higher than the plotted controls in all 29 available cells. For SOTA Opus, the solo mean is .6874 at $17.99 and 3.063 final agent-hours (3 runs). Team 2 ends at .7232, $36.32 and 6.713 final agent-hours (3 runs), or 1.991 points per $100. Team 8 ends at .8136, $171.10 and 29.360 final agent-hours (3 runs). The SOTA task page separately gives Opus oracle best-of-8 at .7034 and $134.45 (`stats.opus.best_of_cost["8"]`), not the plotted row's .8167 at $275.

## 5. What the comparison cannot show

| Task family | OBSERVED mark definition | INFERRED limit |
|---|---|---|
| Mars Return, 0 A.D., Power Outage | Each mark grades the saved main branch on the same hidden worlds, matches or scenarios. | A score measures the submitted version. It does not count work that agents have not integrated. |
| Chicago, Spectrum, RNA, SOTA | Chicago grades submitted plans during the run. Spectrum and RNA grade stored code; RNA estimates the commit for early marks. SOTA grades main and can take its final mark after 4 hours, by 4 hours 15 minutes. | Early zeros can reflect a missing valid submission, not an absence of work. The SOTA equal-hour match is nominal. |
| Holdout and Mindustry | The world advances during play. Holdout grades the highest construction progress so far and adds 1 survivor per seat. Mindustry counts delivered items and divides the same 8 units among seats. | Equal agent-hours do not hold elapsed game time, swarm labor or control assignment fixed. Mindustry does not add bodies as k increases. |
| Dubai ATC | Early marks cover fewer flights and use a different effective safety penalty. The final mark grades 379 due flights. | An early lead or crossing does not mean that the team can deliver that score over the full airport shift. More agents can cover more positions simultaneously. |
| Cave Rescue | A mark replays the rest of the storm with no new orders. Rescue reports, kits and lost drones can change the score in either direction. | A qualifying early mark is not guaranteed to persist. It is not the live count of completed rescues. |
| Seed to Profit | Pre-final marks grade the live company; the first hour is launch. The final score adds a test on 24 new companies. | Equal-hour comparisons at sizes 4 and 8 are within launch. They do not measure the final two-part objective at a smaller budget. |

- **INFERRED.** A checkpoint inside a 4-hour launch is not a launch with a shorter announced deadline. In the first 30 minutes, initialization and integration can consume much of the allotted time. The captures do not isolate this cost.
- **INFERRED.** Nominal seat-hours are not measured active hours, tokens, dollars, or total infrastructure compute. Extra seats can change parallel tool capacity and in-world capabilities. These data compare teams given `1/k` of the solo wall time. They do not compare against a single agent given 8 hours or twice the actual model budget inside 4 hours.
- **INFERRED.** With usually 3 runs per condition, a cell win is descriptive. The task bootstrap does not make those wins precise. Best-of-k uses a hidden-score oracle that can select a different solo at each mark. The missing and inconsistent plotted control rows do not support a uniform communication comparison.

## What this means for a paid matched-budget experiment

- **OBSERVED.** The public curves already answer the descriptive question: teams usually trail solo at equal nominal agent-hours on these published marks.
- **OBSERVED.** They do not give equal-dollar marks or a public `team-2` versus `silent-2` comparison for Holdout.
- **INFERRED.** Holdout solo changes survivor count and starting inventory. It is not the primary control for communication.
- **INFERRED.** The [Holdout design](DESIGN.md) still tests a separate question: the effect of messages with equal seats, assigned cap and deadline.
- **INFERRED.** Keep the [power pilot](POWER.md) only if a 30-minute result can change a decision. Use it to measure variance, ties and whether the cap binds.
- **INFERRED.** Report realized spend separately. Do not call equal caps equal dollars if the arms miss the prespecified tolerance.
