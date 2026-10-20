# TB4 reader baseline: tables

## Recall of the key's task findings, `category_1to1`

| arm | all 66 tasks | dev | test |
|---|---|---|---|
| static | 121/238 = 51% [45%, 57%] | 43/86 = 50% [40%, 60%] | 78/152 = 51% [43%, 59%] |
| leaderboard | 107/238 = 45% [39%, 51%] | 40/86 = 47% [36%, 57%] | 67/152 = 44% [36%, 52%] |

## Recall of the key's task findings, `locus_and_class`

| arm | all 66 tasks | dev | test |
|---|---|---|---|
| static | 4/238 = 2% [1%, 4%] | 0/86 = 0% [0%, 4%] | 4/152 = 3% [1%, 7%] |
| leaderboard | 4/238 = 2% [1%, 4%] | 0/86 = 0% [0%, 4%] | 4/152 = 3% [1%, 7%] |

## Recall of the key's task findings, `task_any_1to1`

| arm | all 66 tasks | dev | test |
|---|---|---|---|
| static | 179/238 = 75% [69%, 80%] | 64/86 = 74% [64%, 82%] | 115/152 = 76% [68%, 82%] |
| leaderboard | 160/238 = 67% [61%, 73%] | 59/86 = 69% [58%, 77%] | 101/152 = 66% [59%, 73%] |

## Precision lower bound (matched predictions / predictions), `category_1to1`

| arm | predictions | all | high confidence only | top 3 per task |
|---|---|---|---|---|
| static | 251 | 121/251 = 48% [42%, 54%] | 115/233 = 49% [43%, 56%] (233) | 80/157 = 51% [43%, 59%] (157) |
| leaderboard | 216 | 107/216 = 50% [43%, 56%] | 101/203 = 50% [43%, 57%] (203) | 79/155 = 51% [43%, 59%] (155) |

## Recall by key category, `category_1to1`

| category | static | leaderboard |
|---|---|---|
| broken | 18/26 = 69% [50%, 83%] | 16/26 = 62% [43%, 78%] |
| prompt_misaligned | 13/70 = 19% [11%, 29%] | 17/70 = 24% [16%, 35%] |
| reachable | 6/8 = 75% [41%, 93%] | 6/8 = 75% [41%, 93%] |
| tamperable | 18/32 = 56% [39%, 72%] | 19/32 = 59% [42%, 74%] |
| weak | 66/102 = 65% [55%, 73%] | 49/102 = 48% [39%, 58%] |

## Paired difference (leaderboard minus static), bootstrap over tasks

| mode | recall difference | 95% interval |
|---|---|---|
| category_1to1 | -5.9% | [-11.5%, -0.5%] |
| locus_and_class | +0.0% | [-1.3%, +1.2%] |
| task_any_1to1 | -8.0% | [-14.3%, -1.9%] |

## Output health

- static: 66/66 files, 251 findings (3.8 per task), tasks with a claim 64/66 = 97% [90%, 99%]; missing 0, invalid 0; verdicts {'defects_found': 64, 'no_defect_found': 2}
- leaderboard: 66/66 files, 216 findings (3.3 per task), tasks with a claim 61/66 = 92% [83%, 97%]; missing 0, invalid 0; verdicts {'defects_found': 61, 'no_defect_found': 5}
