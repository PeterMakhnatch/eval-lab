# MAC Bench at a matched model budget

Status on 2026-11-24: design and analysis of stored data. This study has run no trial.

## Question

Bespoke's MAC Bench compares a talking team with its control at the same wall-clock time. Each agent has its
own budget, so a team can spend more model money than its control. The question here: at the same total model
budget, does a talking team score higher than a silent team of the same size on a real MAC Bench task?

## Files

| File | What it is |
|---|---|
| `DESIGN.md` | The arms, the cap, the marks, the hazards, the launch commands (not run) and the open checks. |
| `POWER.md` | The sample size, the cost and the budget below which the test cannot decide. |
| `analysis/matched_budget.py` | A reader of Bespoke-harness trial folders: scores and spend at equal time, equal dollars, equal tokens. |
| `analysis/power_plan.py` | A seeded planner for a paired sign-flip test with attrition and up to 3 looks. |

The stored data is another lane's pilot of the task Holdout (claude-haiku-5-5, 30 minutes, 2 trials for each of
4 arms): `~/Developer/eval-lab-results/multiagent-campaign-2026-11-23/bespoke/study4-holdout/`. The outputs of
the readers are in `~/Developer/muster-runs/day-2026-11-24/{mac-matched,mac-power}/`, with the audit reports.

## What the stored pilot shows (audited, 2 trials for each arm)

An independent auditor recomputed every number below from the raw files with its own code. All match.

- OBSERVED. At the 15-minute mark the talking team scored 18.1% higher than the silent team (0.0827 against
  0.0700) at nearly the same mean proxy spend ($0.0753 against $0.0755).
- OBSERVED. At the frozen final state the 2 arms had nearly the same mean score (0.1201 against 0.1203), and the
  talking team had spent 29.0% more in proxy USD ($0.1730 against $0.1341). Its output tokens were 1.92% higher.
  Cache reads are about 95% of the extra spend at the proxy's prices.
- OBSERVED. At the first moment each trial reached the smaller arm's mean 15-minute spend, the team's lead was
  0.006 to 0.007 points. At equal time it was 0.0127 points.
- OBSERVED. At the first logged $0.10 of spend the talking team leads in 1 trial pair and is level at replay
  precision in the other. The agents did not know about a cap, and the world did not stop at that moment.
- INFERRED. The pilot shows an early lead at similar spend. It does not show the effect of a cap that is
  enforced. 2 trials for each arm on 1 map cannot settle it.
- OBSERVED. In Holdout 1 agent is 1 survivor, and the world does not scale with the number of agents. So a
  solo agent with the whole budget changes labour and inventory as well as communication. To test talking at an
  equal budget, the control is the silent team of the same size.
- OBSERVED. The harness has a team-wide cap (`budget_usd`), checked before each model request. No stored trial
  has reached it: the pilot caps were $0.75 and $1.25, and the largest spend was $0.1975.

## What the plan says

- A cap of $0.10 for each trial with 2 agents would have been reached in 6 of 6 pilot trials.
- For a gain of 10% of the control's score: 6 complete pairs if the pilot's spread holds; 8 pair starts cost
  about $6.04 to $6.43 (model and Daytona). Below $4.53 the paired test cannot reject.
- The spread comes from 2 trials for each arm. To learn it first: 12 complete pairs, 16 pair starts, about
  $12.08 to $12.85.
- Before any paid trial: the 4 changes of `DESIGN.md` Part 2.3 and the $0 checks O1 to O6 of Part 2.7.
