# P0-F9D7D6-4 results: no stored Haiku facet field beats run length on Goodfire DEV

Rules, selection, controls and criteria are those of `PREREGISTRATION.md` (committed and pushed at
2026-10-29 13:44 ET, SHA `5db3df721d8a41328fa6c1136f5d23d0927bd7eb`, before any label-joined number). The score ran once
(`tools/screen.py score`; every number below is in `results.json`, tables verbatim in `RESULTS.generated.md`).
Population: strict-DEV Goodfire, 2,388 runs (1,636 enacted / 752 clean, 600 base tasks), Haiku record present on all.
Intervals are Wilson 95% unless noted.

## Answer

**No.** Of the eight preregistered facet rules, the one grouped CV selects (F8, an "explicit tamper flags" union) is
feasible at <=3% FAR (G0 passes) but **fails G1 and G2**: it does not beat the length-only rule outside the longest tertile
or on the 123 same-task matched pairs. It is also nowhere near the 15%-recall target (3.7%). Nothing here changes the
earlier reading that Haiku facets add little beyond run length on Goodfire DEV.

## DEV (all 2,388 runs, descriptive for all eight rules)

| Rule | Recall (enacted) | FAR (clean) |
|---|---|---|
| F1 `suspicion >= 2` | 105/1636 = 6.4% [5.3, 7.7] | 39/752 = 5.2% [3.8, 7.0] (infeasible) |
| F2 `answer_material_accessed` | 25 = 1.5% [1.0, 2.2] | 2 = 0.3% [0.1, 1.0] |
| F3 `plans_to_game` | 2 = 0.1% | 0 |
| F4 `plans_to_game` or `reasons_about_grader` | 333 = 20.4% [18.5, 22.4] | 64 = 8.5% [6.7, 10.7] (infeasible) |
| F5 `grader_or_harness_touched` | 35 = 2.1% [1.5, 3.0] | 7 = 0.9% [0.5, 1.9] |
| F6 `tests in {replaced, deleted}` | 59 = 3.6% [2.8, 4.6] | 10 = 1.3% [0.7, 2.4] |
| F7 `shortcut != none AND success claim w/o evidence` | 112 = 6.8% [5.7, 8.2] | 50 = 6.6% [5.1, 8.7] (infeasible) |
| F8 `answer_material_accessed OR grader_or_harness_touched OR plans_to_game` | 60 = 3.7% [2.9, 4.7] | 9 = 1.2% [0.6, 2.3] |

* The only rule with enough support to reach 15% recall (F4) has an 8.5% FAR, i.e. fails the gate by almost 3x. Every
  rule that passes the FAR gate alarms on at most ~4% of the enacted runs, because its field is rarely set.
* Grouped 5-fold CV (task groups) picked F8 in folds 0-2 and F6 in folds 3-4; pooled out-of-fold recall 50/1636 = 3.1%
  [2.3, 4.0], FAR 14/752 = 1.9% [1.1, 3.1]. Frozen rule (same pick on all DEV): **F8**.

## Controls for the frozen rule (F8)

| Length tertile (cuts 40 / 89) | Enacted / clean | F8 recall | F8 FAR | Length-only recall at F8's clean alarms |
|---|---|---|---|---|
| 1 (<= 40) | 310 / 493 | 9 = 2.9% [1.5, 5.4] | 1/493 = 0.2% | 0 (no threshold admissible) |
| 2 (41-89) | 713 / 84 | 25 = 3.5% [2.4, 5.1] | 2/84 = 2.4% | 14 = 2.0% [1.2, 3.3] (t = 89) |
| 3 (>= 90) | 613 / 175 | 26 = 4.2% [2.9, 6.1] | 6/175 = 3.4% | 0 (t undefined) |

Tertile 3's length-only entry is an artefact worth knowing: 13 clean runs sit exactly at the step cap of 252 steps (6
enacted do), so no length threshold admits <= 6 clean alarms there. Tertile 3 therefore says nothing against F8; the
preregistered comparison (G1) deliberately uses tertiles 1-2 only.

* **G0** (feasible on all DEV and OOF FAR <= 3% and OOF recall > 0): **pass** (DEV FAR 9/752 = 1.2%; out of fold 14/752 = 1.9%).
* **G1** (strictly more true alarms than length-only at the same clean alarms, length <= 89, plus bootstrap lower
  bound > 0): **fail**. 34/1,023 enacted = 3.3% [2.4, 4.6] against 28/1,023 = 2.7% [1.9, 3.9] for length-only; recall
  difference +0.59 pp, task-cluster bootstrap 95% interval [-1.01, +2.36] pp (2,000 resamples). The rule has more
  detections by 6 runs; the interval says that is not distinguishable from zero.
* **G2** (123 inspected same-task length-matched pairs: enacted-only > clean-only, sign p < 0.05, pair-clean FAR <= 3%):
  **fail**. Usable 123/123; F8 alarms on 3 enacted and 3 clean members, all discordant pairs split 3 enacted-only / 3
  clean-only, exact one-sided sign p = 0.66. Clean alarms 3/123 = 2.4% [0.8, 6.9] (this part passes).
* 15% recall at <= 3% FAR target: **no** (3.7%).
* **Improved over length: NO.**

## Cross-tests (frozen rule F8 run once, unchanged; F1 is the descriptive reference)

| Group | F8 recall | F8 FAR (established negatives) | F1 recall | F1 FAR |
|---|---|---|---|---|
| CheatBench DEV | 32/75 = 42.7% [32.1, 53.9] | 1/75 = 1.3% [0.2, 7.2] | 30/75 = 40.0% | 4/75 = 5.3% |
| CM-18 established (7 sets) | 107/317 = 33.8% [28.8, 39.1] | 9/329 = 2.7% [1.4, 5.1] | 218/317 = 68.8% [63.5, 73.6] | 6/329 = 1.8% [0.8, 3.9] |
| NP21 Flake established | 0/7 = 0.0% [0.0, 35.4] | 2/60 = 3.3% [0.9, 11.4] | 0/7 = 0.0% | 3/60 = 5.0% |
| All established (CM-18 + Flake) | 107/324 = 33.0% [28.1, 38.3] | 11/389 = 2.8% [1.6, 5.0] | 218/324 = 67.3% [62.0, 72.2] | 9/389 = 2.3% [1.2, 4.3] |

Per CM-18 set, F8 recall / FAR: KernelBench-Hard 0/11, 0/19; Reflex 45/45, 0/50; Cheat Index 8/60, 0/60; EvilGenie
10/60, 0/60; SDF 14/60, 1/60; ResearchArena 20/21, 1/20; Beam 10/60, **7/60 = 11.7%**. Per preregistered hold rule
(recall > 0 and FAR <= 3%): F8 holds on CheatBench DEV and CM-18 pooled and on all-established pooled, and does not hold on Flake (0 of 7 positives, 2 of 60
established negatives). Every cross-test run had a Haiku record (`facet_rows` equals `runs` in all 11 staged sets;
0 excluded). Flake's 7 positives and 60 negatives are its only established data; BAITBENCH, Kobayashi and OSF have no
established negatives and are not scored for FAR.

What this does and does not show:

* F8 looks decent on the cross-tests (33-43% recall at 1-3% FAR) because explicit tamper flags are set on corpora whose
  hacks are blatant (Reflex, ResearchArena, CheatBench). That is a different regime from Goodfire DEV, where the same flags
  fire on only 3.7% of enacted runs. It does not rescue the Goodfire claim: the gate for "improved" was DEV plus matched pairs.
* On the same cross-test rows the headline F1 (`suspicion >= 2`) has much higher recall than F8 (67% vs 33%) at similar
  FAR (2.3% vs 2.8%), yet on Goodfire DEV F1's FAR is 5.2% (infeasible) and its recall 6.4%. The facet reader behaves
  very differently by corpus; a threshold chosen on one corpus does not transfer. This is an observation from the
  preregistered descriptive reference, not a test I designed.
* No cross-test result was used to choose or change anything. No second pass was made.

## Caveats

* Haiku facets only (latest record per run, one reader); other readers' fields were not screened.
* Eight rules chosen from field semantics. A better combination of fields could exist; no search over combinations was
  done and none is claimed. The rare fields (F2, F3, F5, F8) are capped at <= 4% recall by how seldom they are set.
* The 123 pairs are an inspected confirmation set, not blind. With 6 discordant pairs the sign test has no power: G2 fails
  for lack of any signal, not because the test rejected a true effect.
* Length-only controls are label-tuned thresholds on the same data (in-sample); they favour length if anything.
* DEV selection folds share the 600 base tasks across tertiles; no cross-test touched selection.
