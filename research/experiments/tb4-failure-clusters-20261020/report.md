# Failure patterns as a task-defect signal on TB4: tables

Population: 66 tasks, 11,550 trials; 50 tasks have at least 20 failed trials with per-test evidence and at least 3 tests.

## Target: pm_ambiguity (30/50 scored tasks positive)

| score | AUROC dev | AUROC test | AUROC all [bootstrap 95%] |
|---|---|---|---|
| modal_set_share | 0.57 | 0.48 | 0.52 [0.36, 0.69] |
| modal_test_share | 0.46 | 0.53 | 0.48 [0.33, 0.65] |
| near_miss_share | 0.38 | 0.59 | 0.52 [0.36, 0.67] |
| fail_rate | 0.80 | 0.58 | 0.64 [0.48, 0.80] |

Threshold from dev (max_dev_f1): modal_set_share ≥ 0.297

| split | flagged | precision | recall |
|---|---|---|---|
| dev | 18 | 9/18 = 50% [29%, 71%] | 9/9 = 100% [70%, 100%] |
| test | 29 | 18/29 = 62% [44%, 77%] | 18/21 = 86% [65%, 95%] |
| all | 47 | 27/47 = 57% [43%, 70%] | 27/30 = 90% [74%, 97%] |

## Target: any_wrong_fail (31/50 scored tasks positive)

| score | AUROC dev | AUROC test | AUROC all [bootstrap 95%] |
|---|---|---|---|
| modal_set_share | 0.57 | 0.55 | 0.56 [0.41, 0.72] |
| modal_test_share | 0.46 | 0.57 | 0.51 [0.34, 0.67] |
| near_miss_share | 0.38 | 0.67 | 0.56 [0.40, 0.72] |
| fail_rate | 0.80 | 0.65 | 0.69 [0.53, 0.83] |

Threshold from dev (max_dev_f1): modal_set_share ≥ 0.297

| split | flagged | precision | recall |
|---|---|---|---|
| dev | 18 | 9/18 = 50% [29%, 71%] | 9/9 = 100% [70%, 100%] |
| test | 29 | 19/29 = 66% [47%, 80%] | 19/22 = 86% [67%, 95%] |
| all | 47 | 28/47 = 60% [45%, 72%] | 28/31 = 90% [75%, 97%] |

## Target: epoch_fn (7/50 scored tasks positive)

| score | AUROC dev | AUROC test | AUROC all [bootstrap 95%] |
|---|---|---|---|
| modal_set_share | 0.33 | 0.71 | 0.58 [0.29, 0.86] |
| modal_test_share | 0.32 | 0.66 | 0.53 [0.25, 0.79] |
| near_miss_share | 0.41 | 0.74 | 0.62 [0.38, 0.86] |
| fail_rate | 0.89 | 0.87 | 0.86 [0.72, 0.97] |

Threshold from dev (max_dev_f1): modal_set_share ≥ 0.427

| split | flagged | precision | recall |
|---|---|---|---|
| dev | 16 | 3/16 = 19% [7%, 43%] | 3/3 = 100% [44%, 100%] |
| test | 24 | 3/24 = 12% [4%, 31%] | 3/4 = 75% [30%, 95%] |
| all | 40 | 6/40 = 15% [7%, 29%] | 6/7 = 86% [49%, 97%] |

