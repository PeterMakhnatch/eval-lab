# Post hoc: item-discrimination scores (defined for all tasks)

61 tasks scored. Added after the primary results; 3 scores × 3 targets were tried, so read intervals as exploratory.

## Target: pm_ambiguity (38/60 positive)

| score | AUROC dev | AUROC test | AUROC all [bootstrap 95%] |
|---|---|---|---|
| low_discrimination | 0.62 | 0.57 | 0.60 [0.46, 0.75] |
| low_strong_weak_gap | 0.59 | 0.58 | 0.60 [0.45, 0.74] |
| fail_rate | 0.73 | 0.61 | 0.64 [0.49, 0.79] |

Threshold from dev (precision>=0.8): low_discrimination ≥ -0.486

| split | flagged | precision | recall |
|---|---|---|---|
| dev | 7 | 6/7 = 86% [49%, 97%] | 6/13 = 46% [23%, 71%] |
| test | 16 | 13/16 = 81% [57%, 93%] | 13/25 = 52% [33%, 70%] |
| all | 23 | 19/23 = 83% [63%, 93%] | 19/38 = 50% [35%, 65%] |

## Target: any_wrong_fail (39/60 positive)

| score | AUROC dev | AUROC test | AUROC all [bootstrap 95%] |
|---|---|---|---|
| low_discrimination | 0.62 | 0.61 | 0.63 [0.48, 0.77] |
| low_strong_weak_gap | 0.59 | 0.62 | 0.63 [0.47, 0.77] |
| fail_rate | 0.73 | 0.66 | 0.68 [0.54, 0.82] |

Threshold from dev (precision>=0.8): low_discrimination ≥ -0.486

| split | flagged | precision | recall |
|---|---|---|---|
| dev | 7 | 6/7 = 86% [49%, 97%] | 6/13 = 46% [23%, 71%] |
| test | 16 | 14/16 = 88% [64%, 97%] | 14/26 = 54% [35%, 71%] |
| all | 23 | 20/23 = 87% [68%, 95%] | 20/39 = 51% [36%, 66%] |

## Target: epoch_fn (9/60 positive)

| score | AUROC dev | AUROC test | AUROC all [bootstrap 95%] |
|---|---|---|---|
| low_discrimination | 0.55 | 0.68 | 0.64 [0.44, 0.83] |
| low_strong_weak_gap | 0.44 | 0.69 | 0.60 [0.39, 0.82] |
| fail_rate | 0.55 | 0.73 | 0.67 [0.45, 0.87] |

Threshold from dev (max_dev_f1): low_discrimination ≥ -0.757

| split | flagged | precision | recall |
|---|---|---|---|
| dev | 17 | 3/17 = 18% [6%, 41%] | 3/3 = 100% [44%, 100%] |
| test | 26 | 5/26 = 19% [9%, 38%] | 5/6 = 83% [44%, 97%] |
| all | 43 | 8/43 = 19% [10%, 33%] | 8/9 = 89% [57%, 98%] |

