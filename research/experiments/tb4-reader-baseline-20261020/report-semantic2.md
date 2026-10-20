# TB4 reader baseline: semantic match tables

## Recall of the key's 238 task findings (judge matched the same defect, 1-to-1)

| arm | same | same or partial | dev | test | precision (same / predictions) |
|---|---|---|---|---|---|
| static | 71/238 = 30% [24%, 36%] | 97/238 = 41% [35%, 47%] | 25/86 = 29% [21%, 39%] | 46/152 = 30% [24%, 38%] | 71/251 = 28% [23%, 34%] of 251 |
| leaderboard | 71/238 = 30% [24%, 36%] | 91/238 = 38% [32%, 45%] | 26/86 = 30% [22%, 41%] | 45/152 = 30% [23%, 37%] | 71/216 = 33% [27%, 39%] of 216 |

Paired difference (leaderboard minus static), same matches: +0.0% [-3.6%, +3.4%]

Found by either arm: 84/238 = 35% [30%, 42%]; by both: 58/238 = 24% [19%, 30%].

## Recall by key category (same matches)

| category | static | leaderboard |
|---|---|---|
| broken | 7/26 = 27% [14%, 46%] | 9/26 = 35% [19%, 54%] |
| prompt_misaligned | 14/70 = 20% [12%, 31%] | 18/70 = 26% [17%, 37%] |
| reachable | 5/8 = 62% [31%, 86%] | 5/8 = 62% [31%, 86%] |
| tamperable | 15/32 = 47% [31%, 64%] | 12/32 = 38% [23%, 55%] |
| weak | 30/102 = 29% [21%, 39%] | 27/102 = 26% [19%, 36%] |

## Recall by PM finding type (a finding can carry several)

| type | static | leaderboard |
|---|---|---|
| ambiguity | 16/74 = 22% [14%, 32%] | 22/74 = 30% [21%, 41%] |
| grading_or_isolation | 6/25 = 24% [11%, 43%] | 8/25 = 32% [17%, 52%] |
| reward_hack | 50/140 = 36% [28%, 44%] | 42/140 = 30% [23%, 38%] |

## Recall by label strength

| strength | static | leaderboard |
|---|---|---|
| confirmed | 6/10 = 60% [31%, 83%] | 5/10 = 50% [24%, 76%] |
| pm_reported | 65/228 = 29% [23%, 35%] | 66/228 = 29% [23%, 35%] |

## Key findings neither arm matched: 154

- by category: {'prompt_misaligned': 49, 'weak': 71, 'tamperable': 15, 'broken': 16, 'reachable': 3}
- by PM type: {'ambiguity': 50, 'reward_hack': 89, 'grading_or_isolation': 15}
- by PM execution claim: {'unknown': 65, 'true': 88, 'false': 1}

Match problems: 0
