# MAC Bench at a matched model budget

Status on 2026-11-24: design and analysis of stored data. This study has run no trial. The lane's
recommendation is not to buy the Holdout pilot now (see "Decision of 2026-11-24" at the end).

## Question

Bespoke's MAC Bench compares a talking team with its control at the same wall-clock time. Each agent has its
own budget, so a team can spend more model money than its control. The question here: at the same total model
budget, does a talking team score higher than a silent team of the same size on a real MAC Bench task?

## Files

| File | What it is |
|---|---|
| `DESIGN.md` | The arms, the cap, the marks, the hazards, the launch commands (not run) and the open checks. |
| `POWER.md` | The sample size, the cost and the budget below which the test cannot decide. |
| `PUBLIC-DATA.md` | Bespoke's published runs at equal agent-hours and by final score for each model dollar. |
| `analysis/matched_budget.py` | A reader of Bespoke-harness trial folders: scores and spend at equal time, equal dollars, equal tokens. |
| `analysis/power_plan.py` | A seeded planner for a paired sign-flip test with attrition and up to 3 looks. |
| `analysis/holdout_reads.py` | A reader of the Holdout pilot's traces: message delivery, reads between actions, repeated orders. |
| `analysis/public_equal_compute.py` | A reader of the captured public chart data: equal agent-hours, parity brackets, score for each dollar. |

The stored data is another lane's pilot of the task Holdout (claude-haiku-5-5, 30 minutes, 2 trials for each of
4 arms): `~/Developer/eval-lab-results/multiagent-campaign-2026-11-23/bespoke/study4-holdout/`. The outputs of
the readers are in `~/Developer/muster-runs/day-2026-11-24/{mac-matched,mac-power,mac-reads,mac-public}/`, with
the audit reports. The public data is the library capture
`~/Developer/research-context/multi-agent/sources/posts/2026-11-18-mac-bench-public-result-data.md`.

## What Bespoke's published runs show at equal compute

517 published runs, 12 tasks, 3 models, 36 task-and-model cells. A team of k agents has used the 4 agent-hours
of 1 agent's full run after 4/k hours. The lane lead recomputed the 3 headline rows and the split by control
with separate code before reading the worker's output; they agree. The other tables have 4 of 10 listed
numbers checked by hand.

| Team | Against 1 agent at the same time (4 h) | Against 1 agent at the same agent-hours | Cells where the team leads |
|---|---:|---:|---:|
| 2 agents (at 2 h) | +0.023 | −0.048 | 13 of 36 |
| 4 agents (at 1 h) | +0.039 | −0.107 | 8 of 36 |
| 8 agents (at 30 min) | +0.070 | −0.157 | 8 of 36 |

- OBSERVED. On the 3 tasks whose control is a silent team (Holdout, Dubai ATC, Mindustry) the team of 8 at
  30 minutes is +0.046 against 1 agent at 4 hours (6 of 9 cells). On the 9 other tasks it is −0.224 (2 of 27).
- OBSERVED. Final score for each $100 of model spend, mean over 36 cells: 1 agent 1.549, team of 2 0.804, team
  of 4 0.404, team of 8 0.211. The single agent has the higher ratio in 36 of 36 cells.
- INFERRED. In the published data a team buys wall-clock time and bodies in a live world, not score for each
  dollar. The data cannot show 1 agent with 2 times the budget, and it has no spend at the marks. Several
  tasks run in real time (Seed to Profit earns nothing in its first hour), so equal agent-hours is not a fair
  cut for them. `PUBLIC-DATA.md` section 5 lists the limit for each task family.

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

## What the agents did with messages in the stored pilot (audited)

An independent auditor confirmed 34 of 36 sampled facts and recounted the 4 headline numbers from the raw
files. Its 4 corrections are applied in the reader. The sentences below are the auditor's wording.

- OBSERVED. All 62 game-routed posts (of 68) appear in a later tool result of the receiver. The median lag is
  0.31 s: 26 of 62 arrive within 0.3 s, 59 of 62 within 5 s, and the longest takes 25 s. 6 of 68 posts reached
  no teammate.
- OBSERVED. The habit of the synthetic `muster` worlds, 1 board read and then 1 batch of actions, does not
  appear. All 14 seats read events between their first and last actions. An events read lies between
  consecutive action steps in 72 of 83 cases in the team arm and in 60 of 72 in the guard arm.
- OBSERVED. An `events` read also returns game progress, and the silent arm reads at a similar rate (21.6
  reads for each 10 minutes against 27.4 in the team arm). So these reads do not show that agents look for
  teammate posts.
- OBSERVED. In the 2 silent trials 8 of 93 accepted shared-work orders repeat a teammate's earlier order on
  the same target; in the 2 team trials 10 of 100 do. 5 of the 8 silent repeats are 2 agents asking a stranger
  the same question. 6 of the 10 team repeats are chops on a tree after the teammate's announcement was in an
  earlier result. The game allows several harvests of 1 tree, so these are not counts of wasted work.
- INFERRED. A post that is never fetched is not the main gap in these trials, unlike the synthetic worlds
  (`muster-boxes/FINDINGS.md` F31: 55.8% of repeats had a post that the agent had not fetched). The pilot does
  not measure whether a delivered post was used or ignored. A tool result is not attention.

## What the plan says

- A cap of $0.10 for each trial with 2 agents would have been reached in 6 of 6 pilot trials.
- For a gain of 10% of the control's score: 6 complete pairs if the pilot's spread holds; 8 pair starts cost
  about $6.04 to $6.43 (model and Daytona). Below $4.53 the paired test cannot reject.
- The spread comes from 2 trials for each arm. To learn it first: 12 complete pairs, 16 pair starts, about
  $12.08 to $12.85.
- Before any paid trial: the 4 changes of `DESIGN.md` Part 2.3 and the $0 checks O1 to O6 of Part 2.7.

## The launcher and its $0 proofs (built, reviewed, not landed)

A launcher that wraps the other lane's Bespoke lab without editing it is on the pushed branch `fable/mac-run`
(`e05fc5018`; 13 files under `run/` and the plan `campaign/plans/mac-matched-pilot.toml`, marked "not
approved"). It makes the 4 changes of `DESIGN.md` Part 2.3. Its proofs used scripted seats on local Docker and
a fake model endpoint: no model call, no paid call. Receipts and the 2 reviews are in
`~/Developer/muster-runs/day-2026-11-24/mac-run/{build,proofs,reviews}/`.

| Check of `DESIGN.md` Part 2.7 | Verdict | Evidence in short |
|---|---|---|
| O1, Claude Code on a cap refusal | pass | Exit 1 in 0.4 s, 1 request, no retry. |
| O2, team cap and per-seat cap | pass | 12 of 12 cases at 1, 2 and 4 seats; largest overshoot of the test burst $0.026. |
| O3, background processes | fail | Detached and orphan processes got 50 replies and 8 accepted orders after the harness stop; the other seat's workers got 21 accepted orders after its clean exit. |
| O4, fixed horizon | pass | Both seats ended at 50.9 s; the world froze at 121.1 s for `time_up`; queued orders ran on. |
| O5, sealed Git in every arm | pass | 5 of 5 seats failed 2 reads and 2 writes against the team remote. |
| O6, web tools denied | pass | The CLI refused both tools with the changed settings; the unchanged settings offered both. |
| Game call log | pass | 36 of 36 calls in each arm, no message text; the silent arm refused 6 of 6 messages. |

An independent safety review gave the verdict "do not land" with 4 blockers:

1. The last paid gate of `run/matched_trial.py` is an `assert`. Python with `-O` removes it.
2. The recorded-cost cap of `campaign` read only `seat_results`. A Bespoke result records 1 team cost, so the
   cap counted $0.00 for it. (This is a defect of `campaign` for every harness without a seat list; its fix
   is a separate change.)
3. The admission limit uses estimates, not provider bills, and an incomplete cleanup can look settled. No
   code checks Daytona funds.
4. O3: a seat that stops is not a boundary for game activity. Register and measure it in both arms, or end
   the seat's game access at its exit.

## Decision of 2026-11-24

An independent skeptic reviewed the plan against the day's results. Its verdict, which the lane accepts: do
not buy the Holdout pilot now.

- No outcome of the pilot changes what a builder does. It compares a talking pair with a silent pair on 1
  game. It cannot say whether a second agent is worth its cost.
- Holdout is 1 of the 2 tasks where teams hold up in Bespoke's data. The pilot would use a cheap model for 30
  of 240 minutes on 1 map and seed.
- Bespoke's published runs already answer the descriptive question (the section above): a single agent has
  the higher final score for each model dollar in 36 of 36 cells.
- Reopen this study only for a named decision about Holdout, and only after the 4 blockers are closed.
- The readers, `DESIGN.md` and `POWER.md` stay as the record of how to do a matched-budget comparison on a
  Bespoke-harness task.
