# Swarm QA from stored verdicts

**Question.** At the same dollars, what finds more true defects in evaluation tasks, graders, and traces? Compare 1 stronger model, merged runs of a cheap model, and agents that talk.

**INFERRED.** Try a cheap union as a screen, with review of the extra alarms. Do not use these data to choose an equal-dollar policy. They do not show more verified defects for each dollar at the same false-alarm rate.

**OBSERVED.** This is an offline recount, not a new model run. The independent audit checked counts and prices, not the underlying labels. A hit matches a stored positive label. A control alarm flags a labeled control. All alarms include hits. A labeled control is not proof of a clean item. The cohorts use different labels, so do not pool them.

## Cohorts and label sources

| Cohort | Items used | Who made the labels |
|---|---|---|
| TR-A | 114 priced calibration traces: 39 positives, 75 controls | Eval Lab action Gold. 2 Gemini 3.8 Flash readers agreed on mechanisms and executed-action citations for Terminal Wrench and CheatBench positives. Eval Lab R1/R2 reviewed Goodfire actions. The lab used submitted and restored grades for constructed impossible tasks. The priced slice has 35 Terminal Wrench and 4 constructed positives. |
| TR-E | 223 evaluation traces: 83 positives, 140 controls | The same action-Gold protocol. This slice includes Terminal Wrench, CheatBench, Goodfire, and impossible-task positives. |
| DT1 | 150 exposed Goodfire DEV traces: 80 positives, 70 controls. 148 complete all 3 DeepSeek draws; 144 also complete Opus. | Goodfire CM-6 uses publisher-derived enacted and clean labels. These labels do not certify a specific window or a reward change. |
| RA | 92 traces complete across all 4 readers: 57 positives, 35 controls | RA6 uses Goodfire source labels and Eval Lab keys from CheatBench publisher labels. Source-labeled attempts can count as positives. |
| MIX | 12 selected TB4 DEV tasks: 46 keys, no certified clean tasks | Preference Model published the issues. Eval Lab assigned directions and adjudicated mechanism matches. The artifact names `CrossModelCouncilBench`, not an individual. 10 keys are `confirmed`; 36 are `pm_reported`. |

## Recount tables

**OBSERVED.** Recorded USD means stored charges for the compared calls. It excludes review time and other unpriced work. RA USD uses historical list prices and logged tokens. RA subscription receipts report zero charges, not a comparable price. Unknown does not mean zero.

### Trace Review: TR-A and TR-E

| Cohort and arm | Hits / positives | All alarms | Control alarms | Recorded USD |
|---|---:|---:|---:|---:|
| TR-A Mini evidence-first | 13/39 | 13 | 0/75 | 0.631859 |
| TR-A Flash evidence-first | 17/39 | 21 | 4/75 | 0.201742 |
| TR-A Flash score-first | 14/39 | 18 | 4/75 | 0.200251 |
| TR-A Flash union, ≥1 of 2 | 20/39 | 26 | 6/75 | 0.401993 |
| TR-A both Flash calls, ≥2 of 2 | 11/39 | 13 | 2/75 | 0.401993 |
| TR-A Flash evidence-first → Mini | 10/39 | 10 | 0/75 | 0.392692 |
| TR-A Flash score-first → Mini | 8/39 | 8 | 0/75 | 0.290417 |
| TR-A Flash union → Mini | 11/39 | 11 | 0/75 | 0.632770 |
| TR-E Mini v1 | 20/83 | 26 | 6/140 | 1.323690 |
| TR-E Flash v2 | 33/83 | 43 | 10/140 | 0.374812 |
| TR-E Flash v2 → Mini v1 | 17/83 | 18 | 1/140 | 0.642692 |

TR-A uses threshold 66 on all 3 scores. The lab set 66 from the original Flash evidence-first calibration scores. Calibration gives 6/149 control alarms at 66 and 8/149 at 65. The threshold predates the ablation, but the ablation did not preregister this comparison. TR-E uses each pipeline's frozen threshold: 11 for Mini v1 and 66 for Flash v2. The versions differ; TR-E does not isolate a model effect.

| TR-A selection at threshold 66 | Items | Mini hits; control alarms | Flash union hits; control alarms |
|---|---:|---:|---:|
| Priced | 114 | 13/39; 0/75 | 20/39; 6/75 |
| All complete Mini-scheduled | 143 | 15/51; 0/92 | 25/51; 7/92 |
| Complete but not priced | 29 | 2/12; 0/17 | 5/12; 1/17 |

The calibration manifest has 234 traces in 100 task groups. The dry-run cost rule selected a hash-ordered prefix of 57 groups with 144 traces. 1 trace lacks a complete Flash evidence-first score. All 29 excluded complete traces have a Mini cache hit. Some also fail a Flash cost check. The filter does not create the recall lead at 66. It can still change the size of that lead. Mini did not score all 234 traces.

| TR-A in-sample control-alarm count | Mini best hits; control alarms | Flash union best hits; control alarms |
|---|---:|---:|
| Exactly 0 | 14/39; 0/75 | 0/39; 0/75 |
| At most 6 | 14/39; 0/75 | 20/39; 6/75 |
| Exactly 10 | 22/39; 10/75 | 18/39; 10/75 |
| At most 10 | 22/39; 10/75 | 20/39; 6/75 |

No Mini threshold gives exactly 6 control alarms. Thresholds 11–40 give 14/39 at zero. Thresholds 6–10 maximize F1 and Youden's J on this slice. They give 22/39 at 10 control alarms. The sweep reuses the labels. It does not validate new thresholds.

### DT1: DeepSeek repeats and Opus

Each merge cell is `hits / positives; all alarms (control alarms)`. USD for a draw subset applies to all its voting rules. There are 70 labeled controls in each row. Policy C keeps 2 deterministic positive pass-throughs for every arm.

| Cohort and arm | Traces | Hits / positives | All alarms | Control alarms | Recorded USD |
|---|---:|---:|---:|---:|---:|
| DeepSeek r1 | 150 | 16/80 | 21 | 5 | 0.20899840 |
| DeepSeek r2 | 148 | 15/78 | 19 | 4 | 0.19460740 |
| DeepSeek r3 | 150 | 16/80 | 21 | 5 | 0.23364014 |
| Opus, complete | 146 | 14/76 | 19 | 5 | Unknown |
| Opus, paired | 144 | 14/74 | 19 | 5 | Unknown |
| r1 → Opus, paired | 144 | 11/74 | 13 | 2 | Unknown; 0.201166 for r1 only |

| Draw subset, 148 traces | Trace ≥1 | Trace ≥2 | Trace ≥3 | Same window ≥2 | Same window ≥3 | Recorded USD |
|---|---|---|---|---|---|---:|
| r1+r2 | 19/78; 25 (6) | 12/78; 15 (3) | — | 12/78; 14 (2) | — | 0.401864 |
| r1+r3 | 22/78; 29 (7) | 10/78; 13 (3) | — | 9/78; 12 (3) | — | 0.439230 |
| r2+r3 | 20/78; 27 (7) | 11/78; 13 (2) | — | 9/78; 10 (1) | — | 0.426581 |
| r1+r2+r3 | 22/78; 30 (8) | 17/78; 21 (4) | 8/78; 10 (2) | 16/78; 20 (4) | 7/78; 8 (1) | 0.633838 |

| Draw subset, 144 traces paired with Opus | Trace ≥1 | Trace ≥2 | Trace ≥3 | Recorded USD |
|---|---|---|---|---:|
| r1+r2 | 18/74; 24 (6) | 12/74; 15 (3) | — | 0.386446 |
| r1+r3 | 21/74; 28 (7) | 10/74; 13 (3) | — | 0.426929 |
| r2+r3 | 19/74; 26 (7) | 10/74; 12 (2) | — | 0.411044 |
| r1+r2+r3 | 21/74; 29 (8) | 16/74; 20 (4) | 8/74; 10 (2) | 0.612209 |

### RA: Haiku repeats and Sonnet

| Arm | Suspicion ≥1: hits; all alarms (control alarms) | Suspicion ≥2: hits; all alarms (control alarms) | Modeled list price, USD |
|---|---|---|---:|
| Sonnet | 18/57; 20 (2/35) | 3/57; 3 (0/35) | 23.389386 |
| Terra | 23/57; 28 (5/35) | 6/57; 6 (0/35) | 7.133029 |
| Haiku | 30/57; 42 (12/35) | 5/57; 6 (1/35) | 1.1387883 |
| Haiku retest | 29/57; 35 (6/35) | 5/57; 6 (1/35) | 1.1405846 |
| Haiku union | 37/57; 49 (12/35) | 6/57; 8 (2/35) | 2.2793729 |
| Both Haiku calls | 22/57; 28 (6/35) | 4/57; 4 (0/35) | 2.2793729 |
| Haiku → Sonnet | 10/57; 12 (2/35) | 2/57; 2 (0/35) | 16.9778043 at ≥1; 2.5536503 at ≥2 |

Same trace IDs do not mean equal input sizes. The 2 Haiku readers match successful map prompts on 51/51 traces with map calls. Sonnet matches on 49/51. No receipt shows byte equality across all calls.

### MIX: coverage of stored keys

| Arm or merge | Distinct matched keys | Claims |
|---|---:|---:|
| Sol (`codex`) | 9/46 | 30 |
| Kimi | 12/46 | 34 |
| Sonnet | 13/46 | 35 |
| Muse | 14/46 | 36 |
| Offline union of 4 | 20/46 | 135 |
| Offline ≥2, ≥3, ≥4 votes | 14/46, 10/46, 4/46 | Not a label-free selection |
| Live merger | 14/46 | 35 selected |

Comparable USD and control alarms are unknown for every MIX arm. The offline union covers 13/29 wrong-pass keys and 7/17 unfair-fail keys. The votes use key identities that a real screener would not have. The live merger is not a shared-board arm.

### Shared misses

| Pair and slice | Both hit | First only | Second only | Both miss |
|---|---:|---:|---:|---:|
| TR-A Flash evidence-first / score-first, 39 positives | 11 | 6 | 3 | 19 |
| TR-E Mini / Flash, 83 positives | 17 | 3 | 16 | 47 |
| DT1 r1 / r2, 78 positives | 12 | 4 | 3 | 59 |
| DT1 r1 / r3, 78 positives | 10 | 6 | 6 | 56 |
| DT1 r2 / r3, 78 positives | 11 | 4 | 5 | 58 |
| RA Haiku / retest, 57 positives at ≥1 | 22 | 8 | 7 | 20 |
| RA Haiku / retest, 57 positives at ≥2 | 4 | 1 | 1 | 51 |

Both Flash calls flag 2/75 TR-A controls. Each flags 2 more alone. Both clear 69/75.

## 8 findings

1. **OBSERVED.** At the reported thresholds, the Flash, DeepSeek, and Haiku unions each flagged more labeled positives than their single comparator: 20 vs 13 of 39, 21 vs 14 of 74, and 37 vs 18 of 57.
2. **OBSERVED.** The unions also flagged more labeled controls: 6 vs 0 of 75, 7 vs 5 of 70, and 12 vs 2 of 35.
3. **OBSERVED.** In TR-A, recorded costs were $0.40 for the Flash union and $0.63 for Mini; in RA, modeled list prices were $2.28 for the Haiku union and $23.39 for Sonnet.
4. **OBSERVED.** Both draws in each DeepSeek pair missed the same 56–59 of 78 labeled positives. Adding r2 to the best pair, r1+r3, added 0 hits and 1 control alarm. Adding r3 to r1+r2 adds 3 hits. The best pair is selected after the results.
5. **OBSERVED.** On 12 selected DEV tasks, the offline union of 4 models matched 20 of 46 stored defect keys, versus 14 for the best single model.
6. **OBSERVED.** The stored QA comparisons in this report do not show 5 or 8 comparable cheap runs, a shared-board QA arm, or all QA arms under one enforced dollar cap.
7. **OBSERVED.** At an exact match of 0 control alarms, tuned Mini hits 14/39 and the tuned Flash union hits 0/39. At a cap of 10, Mini hits 22/39 and Flash hits 20/39. These are in-sample fits.
8. **INFERRED.** Test union screening as a review queue, not as an automatic rejection rule. Stored cascades intersect blind verdicts. They do not measure a reviewer who sees the cheap model's claims.

## QA attempts that execution judged (second pass, audited)

A second worker mapped the stored QA attempts whose outcome a grader run decided: an agent proposes a wrong submission, the original grader gives reward 1 and the repaired or hardened grader gives 0. An independent auditor recounted the numbers below from the stored files with its own code. All match. The auditor replaced 5 of 6 summary sentences and found 1 cohort the worker missed (TS-2). Files: `~/Developer/muster-runs/day-2026-11-24/exec-qa/{report.md,analyze.py,analysis.json,tables.md,audit/auditor-report.md,audit/recount.py}`. The scripts are not in this repository.

A hit is a task with at least 1 credited grader differential: numeric `original_reward=1` and `repaired_reward=0`, the route attested, the stored response hash matching. It is a stored result, not a new native replay.

| TS-3 panel (`~/Developer/eval-lab-results/2026-10-25/ts3-hide-tests-two-turn/`) | 1 route: mean [range] | 2 routes | 3 routes | All routes |
|---|---:|---:|---:|---:|
| 30 historical tasks, 6 routes, tests hidden, turn 1 | 3.67 [1–6] | 5.40 [2–9] | 6.45 [4–9] | 9 |
| 30 historical tasks, 6 routes, tests visible, turn 1 | 7.00 [5–9] | 10.00 [8–13] | 11.65 [9–14] | 14 |
| 13 DEV tasks, 3 routes, tests hidden, turn 1 | 4 [4–4] | 5.33 [4–6] | 6 | 6 |
| 13 DEV tasks, 3 routes, tests visible, turn 1 | 9.67 [9–10] | 11 | 11 | 11 |
| 13 DEV tasks, 3 routes, runner feedback and retrieval, turn 2 | 9.67 [9–10] | 11.33 [11–12] | 12 | 12 |

Single routes on the 30 hidden tasks: DeepSeek V4.1 Flash 6, Spark 1.3 5, Spark Open 4, Muse Wonder 4, Kimi K3.1 Pro 2, Sonnet 5.5 1. The denominator includes 29 unattested cells, which get no credit.

| Priced rows (model telemetry in USD, not a bill; graders not priced) | Sonnet 5.5 | DeepSeek V4.1 Flash | DeepSeek + 1 more cheap route |
|---|---|---|---|
| TS-3 hidden, 13 tasks scorable on all 4 priced routes | 0/13, $0.52 | 4/13, $0.13 | 4/13, $0.29 (+ Spark Open) |
| TS-3 visible, 7 tasks | 2/7, $0.51 | 7/7, $0.10 | 7/7, $0.11 (+ Spark 1.3) |
| TS-2 bare prompt, 14 tasks (`…/2026-10-25/ts2-muse-spark-comparison/`) | 3/14, $0.97 | 7/14, $0.19 | 8/14, $0.22 (+ Spark 1.3) |
| TS-2 retrieval, 14 tasks of the 28-task roster | 7/14, $1.09 | 7/14, $0.14 | 10/14, $0.17 (+ Spark 1.3) |

| Adaptive queues: cumulative tasks with a hit after attempt 1, 2, 3 | Hits | Proposals at each attempt |
|---|---|---|
| Muse wave 5, 39 task versions | 21 → 36 → 36 | 39 → 16 → 1 |
| Muse round 2, 10 | 4 → 10 | 10 → 6 |
| TS-1 Kimi plain, 20 | 4 → 7 | 20 → 16 |
| TS-1 Kimi retrieval, 20 | 5 → 7 | 20 → 15 |
| Hardening loop round 2, 33 (2 passes on the attacked grader, a different endpoint) | 5 → 7 → 7 | 32 → 24 → 22 |

The auditor's sentences:

1. **OBSERVED.** "On the 30 historical TS tasks with tests hidden at turn 1, the 6 single routes averaged 3.67 tasks with a credited grader differential. The 15 route pairs averaged 5.40 tasks, and the union of all 6 covered 9."
2. **OBSERVED.** "In that hidden panel, the mean added coverage is 1.73 tasks for the second route and 1.05 for the third. The next gains are 0.88, 0.83 and 0.83 tasks." The best stored pair already covers all 9.
3. **OBSERVED, descriptive.** "On the 30 historical TS tasks, the visible turn-1 condition averages 7.00 credited hits per route, versus 3.67 with tests hidden." On 123 matched task-route cells: 37 visible against 18 hidden hits.
4. **OBSERVED.** "The Muse wave-5 queue rises from 21/39 to 36/39 cumulative verified differentials after 16 tasks receive a second attempt with grader feedback." There is no matched control: this is a policy prefix, not an isolated effect of feedback.
5. **OBSERVED.** "On 13 scorable hidden TS tasks, DeepSeek records 4 hits and $0.13 in model telemetry. Sonnet records 0 hits and $0.52. Adding Spark Open to DeepSeek adds no hits on those tasks." In TS-2 the second cheap route does add coverage (10 against 7 of 14 with retrieval).
6. **OBSERVED.** "In the stored evidence I checked, I found no execution-scored comparison of several cheap attempts against 1 stronger attempt under the same enforced dollar cap on tasks with verified absence of prior exposure."

**INFERRED.** In these stored attempts 3 things go with more verified grader defects: a retry after the grader's feedback, tests that the agent can see, and a second route of a different kind. A higher price does not. None of this is a comparison at equal dollars: the arms differ in tool use and turns, the tasks are selected, and the costs are telemetry. The worker's planning estimate for the missing comparison (2 one-shot DeepSeek Flash draws against 1 Sonnet draw on fresh private tasks, a $0.15 ceiling for each arm and task): 42 paired tasks for a 20-point margin, about $2.72 at stored medians and $12.60 at the ceiling, before fixtures and review. It needs the sealed tasks of `DESIGN.md` Section 0 first. No paid run is approved.

## Limits and provenance

**INFERRED.** Three label risks block a production decision:

1. Trace labels do not always track executed or reward-changing defects. Eval Lab's later Goodfire review reports 11 executed shortcuts among 80 publisher positives. Its strict outcome test reports 6. That review also uses models.
2. Label readers, window selectors, and scored models can share errors. A verbatim quote proves text presence. It does not prove that the action broke a rule or changed a reward.
3. Prior exposure and incomplete keys can reward agreement with known answers. TR sources informed J3 development. In MIX, 13/46 keys are `tuned` and 14/46 are `inspected_only`. Unmatched claims may name real defects.

| Evidence | Paths |
|---|---|
| TR | `~/Developer/trace-review/evals/{split,scores,v2-scores,policy,v2-policy}.json` and `ablations/2026-11-15-field-order/{scores,dry-run}.json` under that folder |
| DT1 | `~/.local/state/evallab-prod-live/run-atlas/comparisons/{cm20-deepseek,cm19}/` and `~/Developer/research-context/bridge/2026-10-29-ds-squad/raw/DT-1/receipts/` |
| DT1 Opus receipt | `research/evidence/runs/cm19-opus-windows-20261028/results/result.json` in this repository (the reader takes it from the primary checkout `~/Developer/eval-lab`) |
| RA | `~/.local/state/evallab-prod-live/run-atlas/comparisons/ra6-readers/` |
| MIX | `~/Developer/eval-lab-results/2026-10-24/cross-model-swarm-bench/` |
| Label methods | `research/experiments/{ev1110-gold-grow/RESULT.md,ev1105-goodfire/README.md}`, `research/answer-keys/cheatbench-actions-v1/README.md`, and `src/evallab/gold/seed.py` in this repository |
| Worker and audit | `~/Developer/muster-runs/day-2026-11-24/swarm-qa/report.md` and `audit/{auditor-report.md,recount.txt}` in that folder |

Run from the study worktree. The reader does not write to the inputs. The output lists the SHA-256 of each input file. Missing files produce a problem list and a nonzero exit.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -I -B research/experiments/swarm-qa-stored-verdicts/analysis/stored_merges.py
PYTHONDONTWRITEBYTECODE=1 python3 -I -B research/experiments/swarm-qa-stored-verdicts/analysis/stored_merges.py --json
```

The stored outputs are in `~/Developer/muster-runs/day-2026-11-24/swarm-qa/landed/`. See [DESIGN.md](DESIGN.md) for the next measurement. No paid run is approved.
