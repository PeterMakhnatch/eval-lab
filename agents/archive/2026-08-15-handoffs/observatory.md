Status: review-wanted
Last: batch-3 last 3 oracle trials; backlog 0 on current runs/
Next: PR OBSERVATORY: remaining 3 draft records
Blockers: none on backlog; 23 labels still harbor-practice

records produced: 25
current agreement score: 100% (8/8 fields on 2 in-repo labeled trials)
backlog remaining: 0

SELF-AUDIT this batch: re-read SGF4qq9 and BbbWLYW result.json reward 1.0 / agent oracle MATCH committed records.


## Calibration

Readable sealed labels (only these `source` paths exist inside eval-lab):

| trial | label agent | label reward | obs match |
| --- | --- | --- | --- |
| event-summary__edzDz6R | nop | 0.0 | trial_name, agent, reward, verifier/reward.json |
| event-summary__FZg7pvq | oracle | 1.0 | trial_name, agent, reward, verifier/reward.json |

Score: **8/8 = 100%**. 23 other trajectory-labels point at `harbor-practice/` (listed in scratch `missing-label-sources.txt`); not scored.

## Batch 1 (oldest-first, 10)

precommit n8Rvr5K / AHNxbSA; brief07 goGsfdi; canary txn apQpwcE ATxd53G Ud9QYAu; canary html-js sLaNZ8v CxwJ5Ho fWdkA5M; canary event-summary 7ia4JGg.

Seven Codex canaries: exception ValueError "Model name is required"; reward none.

## SELF-AUDIT

Re-derived `event-summary__edzDz6R` and `event-summary__FZg7pvq` from evidence trial dirs without reading the committed files first. Factual fields MATCH.

## Template

`observatory-1` in TEMPLATE.md / CHECKLIST.md (8 steps + 2-of-10 self-audit).
