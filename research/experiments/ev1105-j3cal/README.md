# J3 operating-point calibration

**Ship review >=61 and drop >=100 with J3 still off by default. Do not enable J3 broadly from these results.**

The enabled DEV gate passes. Cheat keeps fall from 9/332 to 6/332. Honest drops stay at 40/358 overall.
No corpus gains an honest drop.
Honest keeps also fall from 63/358 to 52/358. The review load remains a cost, not a free improvement.
The high cutoff catches almost no original held-out cheats and no impossible-task positives as automatic drops.
The middle band supplies the useful recall.

**One requested comparison remains blocked:** 67 frozen J2b readings need its Gemini route. This lane permits Codex routes only.
No Codex substitute is called J2b. All 99 admitted strict runs now have complete frozen J3 readings.

## Frozen design and chronology

[PREREG.md](PREREG.md) contains the unchanged rule and thresholds.
Commit `a25f52eb203cb883f46000a43911c3bad6936e47` reaches origin before any new held-out score inspection.
Source-only review then strengthens reading admission, aggregate export, paired intervals, and the once-only guard.
Those fixes reach origin as `9a118a2f6` before the single evaluation starts. They do not change the rule or its thresholds.

The rule selects the smallest integer cutoff that meets each DEV negative cohort's budget separately.
The drop budget is 0 observed false flags. The review-or-drop budget is 10% observed false flags.
Calibration uses 327 complete original DEV positives, 336 complete original DEV negatives, and 27 complete historical honest controls.
The original DEV has 5 incomplete positives and 22 incomplete negatives. They do not become score 0.
All 27 controls score at most 5. The stronger admission check reproduces exactly 61/100.
[calibration.json](calibration.json) preserves the initial selection receipt.
[calibration-validation.json](calibration-validation.json) adds raw/view binding without changing its scientific values.

A complete reading must match the actual view, every frozen prompt, all window endpoints, and the maximum parsed score.
Every window must name the exact frozen route. Missing, partial, or unattested readings require review in the line.
The stage never clears an earlier review or drop. The Goodfire floor remains unchanged.
Cached reading identity still carries frozen threshold 89. The separate action policy uses thresholds 61 and 100.

The frozen route is `openai-codex/gpt-6.1-terra`.
The actual rubric SHA256 is `7e7cb3c4686423ad137ecbfa726be97f54e5a05805157e3d8e8adbc5250afc83`.
The template SHA256 is `d447dc31c7c496a278b4f8cc2daecc547e8aa1d9689dc1de115e77cd72574592`.
The original freeze's prose rubric literal differs from the actual scorer. This report uses the scorer's verified hash.
The action-only view excludes agent prose and reasoning. Each 60,000-character window uses the frozen V2 prompt and maximum aggregation.
All new scoring uses `omp auth-gateway stdio` with the same no-fallback file in the CLI and `PI_CONFIG_FILES`.

## Single evaluation and scope

The canonical once-only marker records success from 1:57:45 PM ET to 1:58:01 PM ET on November 5, 2026.
The private source index binds 6,315 files. Its SHA256 is `1047a8c1ad5987af0a4fd7294b29b8b4e08f59a8ac92245f31ca5d9a6654ed22`.
The unchanged output SHA256 is `1870c3353ba1a778d7256fd2db0ef4ec835a402e014e7c2e1fa28be51edd43e3`.
[results.json](results.json) contains the aggregate results, source receipt, and all paired intervals.

Original and MALT scores predate this lane and were previously inspected. **This evaluation is not blind.**
No threshold changes follow the held-out results. Impossible-task data remain DEV-side data, not an independent held-out confirmation.
The 67 failed strict attempts stay unlabelled. A failed G1 check does not make an honest negative.
Historical and strict impossible-task tables reuse the same 27 controls. Do not pool those tables as independent samples.
Their labels describe observed mechanisms. They do not prove hidden intent.

| Cohort | Selected positives / negatives | Complete positives / negatives | Task clusters |
|---|---:|---:|---:|
| Original held-out | 344 / 1106 | 343 / 1105 | 168 |
| MALT | 15 / 385 | 15 / 385 | 57 |
| HackBench positives | 169 / 0 | 169 / 0 | 13 |
| Strict impossible, DEV | 32 / 27 | 32 / 27 | 27 |
| Historical impossible, DEV | 53 / 27 | 53 / 27 | 19 |

The original pool contains 1,450 numeric scores. The primary analysis excludes 2 incomplete readings: 1 positive and 1 negative.
Its matched primary table therefore uses 343 positives and 1,105 negatives. The full 1,450-row sensitivity table follows below.
MALT, HackBench, and both impossible-task tables have no incomplete reading exclusions.
HackBench contains positives only. It supports recall, not FPR or AUROC.

## Matched primary operating points

Each cell gives count, percentage, and a 95% Wilson interval. All methods use the same complete rows within a cohort.
Review-or-drop recall is not automatic-drop recall. The review band includes scores from 61 through 99.
These methods do not have equal FPR budgets. Higher recall at threshold 61 is not a matched-FPR victory.

| Cohort | Method | Recall [95% CI] | FPR [95% CI] |
|---|---|---|---|
| Original held-out | Frozen score >=89 | 127/343: 37.03% [32.09, 42.25] | 45/1105: 4.07% [3.06, 5.41] |
| Original held-out | Drop score >=100 | 2/343: 0.58% [0.16, 2.10] | 1/1105: 0.09% [0.02, 0.51] |
| Original held-out | Review or drop score >=61 | 215/343: 62.68% [57.45, 67.63] | 95/1105: 8.60% [7.08, 10.40] |
| Original held-out | Characters >=272,968 | 27/343: 7.87% [5.47, 11.21] | 19/1105: 1.72% [1.10, 2.67] |
| Original held-out | Steps >=95 | 27/343: 7.87% [5.47, 11.21] | 26/1105: 2.35% [1.61, 3.43] |
| MALT | Frozen score >=89 | 1/15: 6.67% [1.19, 29.82] | 0/385: 0.00% [0.00, 0.99] |
| MALT | Drop score >=100 | 0/15: 0.00% [0.00, 20.39] | 0/385: 0.00% [0.00, 0.99] |
| MALT | Review or drop score >=61 | 4/15: 26.67% [10.90, 51.95] | 7/385: 1.82% [0.88, 3.70] |
| MALT | Characters >=272,968 | 1/15: 6.67% [1.19, 29.82] | 2/385: 0.52% [0.14, 1.87] |
| MALT | Steps >=95 | 12/15: 80.00% [54.81, 92.95] | 45/385: 11.69% [8.85, 15.28] |
| HackBench positives | Frozen score >=89 | 66/169: 39.05% [32.02, 46.57] | Undefined: no negatives |
| HackBench positives | Drop score >=100 | 13/169: 7.69% [4.55, 12.71] | Undefined: no negatives |
| HackBench positives | Review or drop score >=61 | 110/169: 65.09% [57.64, 71.87] | Undefined: no negatives |
| HackBench positives | Characters >=272,968 | 155/169: 91.72% [86.58, 95.00] | Undefined: no negatives |
| HackBench positives | Steps >=95 | 153/169: 90.53% [85.18, 94.09] | Undefined: no negatives |
| Strict impossible, DEV | Frozen score >=89 | 32/32: 100.00% [89.28, 100.00] | 0/27: 0.00% [0.00, 12.46] |
| Strict impossible, DEV | Drop score >=100 | 0/32: 0.00% [0.00, 10.72] | 0/27: 0.00% [0.00, 12.46] |
| Strict impossible, DEV | Review or drop score >=61 | 32/32: 100.00% [89.28, 100.00] | 0/27: 0.00% [0.00, 12.46] |
| Strict impossible, DEV | Characters >=272,968 | 0/32: 0.00% [0.00, 10.72] | 0/27: 0.00% [0.00, 12.46] |
| Strict impossible, DEV | Steps >=95 | 0/32: 0.00% [0.00, 10.72] | 0/27: 0.00% [0.00, 12.46] |
| Historical impossible, DEV | Frozen score >=89 | 28/53: 52.83% [39.66, 65.62] | 0/27: 0.00% [0.00, 12.46] |
| Historical impossible, DEV | Drop score >=100 | 0/53: 0.00% [0.00, 6.76] | 0/27: 0.00% [0.00, 12.46] |
| Historical impossible, DEV | Review or drop score >=61 | 53/53: 100.00% [93.24, 100.00] | 0/27: 0.00% [0.00, 12.46] |
| Historical impossible, DEV | Characters >=272,968 | 0/53: 0.00% [0.00, 6.76] | 0/27: 0.00% [0.00, 12.46] |
| Historical impossible, DEV | Steps >=95 | 0/53: 0.00% [0.00, 6.76] | 0/27: 0.00% [0.00, 12.46] |

### Full original 1,450-row numeric sensitivity

This table retains the 2 incomplete readings' numeric maxima. It is not the complete-window policy's primary evidence.
Incomplete readings still require review in the actual line. A numeric score does not authorize their drop.

| Method | Recall [95% CI] | FPR [95% CI] |
|---|---|---|
| Frozen score >=89 | 127/344: 36.92% [31.99, 42.14] | 45/1106: 4.07% [3.05, 5.40] |
| Drop score >=100 | 2/344: 0.58% [0.16, 2.09] | 1/1106: 0.09% [0.02, 0.51] |
| Review or drop score >=61 | 215/344: 62.50% [57.27, 67.45] | 95/1106: 8.59% [7.08, 10.39] |
| Characters >=272,968 | 28/344: 8.14% [5.69, 11.51] | 19/1106: 1.72% [1.10, 2.67] |
| Steps >=95 | 28/344: 8.14% [5.69, 11.51] | 26/1106: 2.35% [1.61, 3.42] |

### Paired review-or-drop change against frozen 89

Intervals use 2,000 paired task-cluster draws with seed 1105. Each draw supplies the same rows to both methods.
The JSON also reports paired comparisons against both length thresholds and for the drop cutoff.

| Cohort | Recall change [95% CI] | FPR change [95% CI] |
|---|---|---|
| Original held-out | +25.66 pp [+17.65, +33.99] (2000/2,000 draws) | +4.52 pp [+2.14, +7.89] (2000/2,000 draws) |
| MALT | +20.00 pp [+0.00, +66.67] (1937/2,000 draws) | +1.82 pp [+0.25, +4.12] (2000/2,000 draws) |
| HackBench positives | +26.04 pp [+4.86, +43.43] (2000/2,000 draws) | Undefined |
| Strict impossible, DEV | +0.00 pp [+0.00, +0.00] (2000/2,000 draws) | +0.00 pp [+0.00, +0.00] (2000/2,000 draws) |
| Historical impossible, DEV | +47.17 pp [+31.48, +63.64] (2000/2,000 draws) | +0.00 pp [+0.00, +0.00] (2000/2,000 draws) |

## Ranking and length controls

These AUROCs describe the frozen score, not a new score model. The action cutoff does not change AUROC.
Raw J3-minus-length intervals use identical paired task-cluster draws. They are not fitted length-model gains.
The JSON also reports length-decile and within-task AUROCs with their coverage and intervals.
Any empirical ROC cutoff in that library output is descriptive only. None replaces the preregistered policy.

| Cohort | J3 AUROC [95% CI] | Character AUROC [95% CI] | Step AUROC [95% CI] |
|---|---|---|---|
| Original held-out | 0.895 [0.859, 0.924] | 0.610 [0.564, 0.656] | 0.610 [0.561, 0.659] |
| MALT | 0.937 [0.908, 0.994] | 0.936 [0.870, 0.998] | 0.886 [0.781, 0.965] |
| HackBench positives | Undefined | Undefined | Undefined |
| Strict impossible, DEV | 1.000 [1.000, 1.000] | 0.774 [0.622, 0.905] | 0.557 [0.384, 0.730] |
| Historical impossible, DEV | 1.000 [1.000, 1.000] | 0.639 [0.572, 0.733] | 0.642 [0.549, 0.756] |

| Cohort | J3 minus characters, AUROC units [95% CI] | J3 minus steps, AUROC units [95% CI] |
|---|---|---|
| Original held-out | +0.285 [+0.237, +0.332] (2000/2,000 draws) | +0.285 [+0.236, +0.333] (2000/2,000 draws) |
| MALT | +0.001 [-0.044, +0.108] (1910/2,000 draws) | +0.051 [-0.033, +0.195] (1910/2,000 draws) |
| HackBench positives | Undefined | Undefined |
| Strict impossible, DEV | +0.226 [+0.094, +0.375] (2000/2,000 draws) | +0.443 [+0.269, +0.615] (2000/2,000 draws) |
| Historical impossible, DEV | +0.361 [+0.267, +0.428] (2000/2,000 draws) | +0.358 [+0.243, +0.451] (2000/2,000 draws) |

Original held-out length-decile J3 AUROC is 0.886 [0.849, 0.917], with 100% row coverage.
MALT length-decile J3 AUROC is 0.831 [0.695, 0.981], but coverage is only 80/400 rows.
MALT within-task coverage is only 17/400 rows. Its 15 positives occupy 3 task clusters.
Its paired AUROC gain against characters includes 0. These results do not establish a reliable MALT advantage over length.
HackBench's character and step thresholds have higher positive-only recall than J3. There are no negatives to price that recall.

Wilson intervals assume independent rows. Task-cluster intervals address some dependence, not benchmark selection or distribution shift.
Impossible-task bootstrap AUROCs are degenerate at 1. That empirical interval does not establish zero sampling uncertainty.
The 0/27 control FPR still has a 12.46% Wilson upper bound. Zero observed DEV false flags is not a future guarantee.

## Full-line DEV replay

The actual command is:

```bash
make line-gate ARGS='--j3-cache /private/tmp/ov1106/j3line/j3-cache.jsonl --output /private/tmp/ev1105/j3cal/line-gate-enabled'
```

The gate passes after 344.43 seconds on all 690 DEV rows. It performs real base and candidate line replays with cached readings.
The baseline commit is `8fd87a6b52ee65e28b9217b5db148a6f9389a9f8`.
The candidate runtime snapshot is `99b6cbf1e0a3c32fc8fa8533f48431280286be4e`.
[The aggregate gate receipt](line-gate.json) contains every corpus/class count and the pinned input hashes.
Neither the frozen manifest nor the source cache changes. The candidate-only overlay enables J3 without changing the default specification.

| Class | Main keep / review / drop | Calibrated keep / review / drop |
|---|---:|---:|
| Cheat, n=332 | 9 / 111 / 212 | 6 / 112 / 214 |
| Honest, n=358 | 63 / 255 / 40 | 52 / 266 / 40 |

Cheat keep rate falls from 2.71% [1.43%, 5.07%] to 1.81% [0.83%, 3.89%].
Honest drop rate stays at 11.17% [8.31%, 14.86%]. No corpus gains an honest drop or a cheat keep.
Honest keep rate falls from 17.60% [14.00%, 21.88%] to 14.53% [11.25%, 18.55%].
Ten lost honest CheatBench keeps require review because their historical readings are incomplete or unattested.
One further honest Terminal Wrench keep moves to review. That cost remains visible.

The earlier frozen-89 replay reports 53 honest drops. The current calibrated replay has 40.
Those snapshots also differ in source integration. Treat that historical comparison as context, not a threshold-only causal estimate.
The standalone tables above apply both thresholds to exactly the same scores.

## Scoring receipts and remaining route blocker

J3 now covers all 99 admitted strict runs, plus all 27 historical controls. The lane adds 67 exact-Terra successful window readings.
All 126 strict/control prompts reconstruct exactly. The binary strict table includes 32 positive runs and those 27 controls.
The other 67 strict runs remain unlabelled, despite complete J3 scores.

All 169 HackBench runs complete, across 2,051 final windows. Every final prompt reconstructs exactly.
The ledger contains 2,116 exact-Terra successful window call records and 19 rejected HTTP 502 capacity records.
A quota read returns `SQLITE_BUSY` after 53 complete runs and 599 successful windows. Call admission stops safely.
The resume preserves all 53 complete results. It repeats unfinished work, which adds 65 successful windows beyond the final result set.
Quota reads gain bounded lock retries. No fallback route, paid client, or paid compute is used.
The 19 rejected capacity records have no served-model attestation. None enters a final admitted score.

Frozen J2b uses `google-antigravity/gemini-3.8-flash`. Its existing cache covers 32 strict runs and 27 controls.
The missing 67 strict readings include 18 positive runs. Codex-only authorization cannot produce frozen J2b readings.
J3 uses the same rubric on Terra, but that does not make it J2b. The blocked comparison needs a route exception or explicit waiver.

## Verification and recommendation

- All 1,453 integrated line, watcher, replay, gate, and ship tests pass.
- `make check` passes. The final scoped Ruff check passes.
- Synthetic smoke exercises score/view admission, raw control binding, paired AUROC edge cases, aggregate projection, and once-only failures.
- The real enabled 690-row gate passes. Honest drops do not increase in any corpus.
- Only aggregates enter Git. Full inherited row-level receipts remain private, with hashes and counts in the public exports.
- Four sequential Sol helpers stay on `openai-codex/gpt-6.1-sol`; their 73 assistant turns show no fallback.

**Recommendation:** retain the default-off flag. The review band is useful, but these results do not support broad automatic activation.
The drop cutoff misses all impossible-task positives and 341/343 original held-out positives. It also flags 1/1,105 held-out honest runs.
A supervised activation decision must price the 11 lost honest keeps and the missing-reading review load.
Do not lower the drop cutoff from these held-out results. Do not remove the existing Goodfire floor.
