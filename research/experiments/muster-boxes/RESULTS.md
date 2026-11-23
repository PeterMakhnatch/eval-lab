# muster results: more agents, talking, and the budget of the team

Written 2026-11-21 for Peter, updated 2026-11-23 with E5. OBSERVED means a run or a file shows it. INFERRED
means our reasoning. Each number comes from `FINDINGS.md` (F11 to F21) and the dated entries in `LOG.md`, both
in this folder. Run outputs stay outside Git, in `muster-runs/`. Paths written before 2026-11-21 refer to the
old muster repository: `docs/` and `experiments/` are now this folder, `tasks/` is `muster/tasks/`.

## Summary

- Talking helps a lot: 3 agents with a channel score 85.7%, 3 without 51.5%, 1 agent with 9 opens 30.8%.
- 1 agent with the budget of all 3 (27 opens) scores 98.5% to 100%, at 1/6 of the team's cost.
- The team loses its flags to repeated opens: alike agents choose the same box at nearly the same time.
  A rule that each agent posts a box before it opens it closes the gap (98.8%). A fixed box order per seat
  gets near (94.5%). A shared view of past opens, 1 open at a time, or a pooled budget does not.
- When the facts are split and no agent can act alone (`ledger`, E4), the talking team scores 100%, like 1
  agent that holds all facts. Every team waited for both reports before it answered. So the team's losses in
  `boxes` come from acting on a shared resource at the same time, not from passing information.
- More agents make it worse (E5, 2026-11-23). With 24 opens in total, 1 agent scores 100%, a talking team of 2
  84%, of 3 79%, of 4 69% and of 8 54%: 15 points lost for each doubling. Under the post-first rule, 8 agents
  score 73.5%, 26.5 points below 1 agent: their claims block boxes that nobody opens. E5 did not run the rule
  at 2 or 3 agents; at 3 agents it closed the gap in F14, on another route and backend.
- Limits: 1 harness, 1 main model, 2 synthetic tasks, 10 instances each.

## The task and the setup

`boxes`: 60 boxes in 6 rooms of 10. 2 rooms are treasure rooms, and every box in them holds a flag. The team
score is the number of different flag boxes opened, divided by 20. An open costs 1 unit of budget; messages
are free. 1 open in a room shows what the room holds. Best play scores 1.0 with 27 opens in total.

- Runner: `olamlabs/harbor-multiagent` at commit `6aeb609e`, local Docker. Each seat has its own network
  and reaches only the world service. The verifier grades from the world's event log only.
- Seats: terminus-2 with DeepSeek Flash, thinking off, in every seat (the 1 exception is E2 `indep-3-mixed`).
- 10 instances (layouts). E1 and E2: 3 attempts each, 30 trials per arm. F14c, F15, F16 and F17: 2 attempts,
  20 trials per arm. F14: 3 attempts, 30 trials.
- Statistics: `tidegauge compare` and `decompose`, 95% level. "Resolved" means that the interval excludes 0
  and the exact sign-flip test agrees (p < 0.05).
- E1 and E2 were pre-registered in `PREREGISTRATION.md`. F14 to F17 were designed in `LOG.md` before their
  first trial.
- Every reward of every arm replays exactly from its event log (`muster regrade`). No API key is in the outputs.

## E1: more agents, or talking? (2026-11-19)

| Arm | Agents x opens | Channel | Score (95% rerun) | Repeated opens | Real cost per trial |
|---|---|---|---|---|---|
| `solo-a` | 1 x 9 | no | 30.8% (±3.6) | 0 | $0.003 |
| `solo-b` (the same again) | 1 x 9 | no | 32.5% (±2.5) | 0 | $0.003 |
| `indep-3` | 3 x 9 | no | 51.5% (±5.1) | 10.7 of 27 | $0.007 |
| `team-3` | 3 x 9 | yes | 85.7% (±4.0) | 4.9 of 26 | $0.012 |
| `solo-x3` | 1 x 27 | no | 98.5% (±6.5) | 0 | $0.002 |

OBSERVED:

- Headcount (`indep-3` minus `solo-a`): +20.7 pts (+14.8 to +26.6). Talking (`team-3` minus `indep-3`):
  +34.2 pts (+28.0 to +40.3). Both are resolved (sign-flip p 0.002). Talking gives 62% of the team's gain.
- The noise floor holds: `solo-b` minus `solo-a` is +1.7 pts, not resolved.
- With the same total budget of 27 opens, 1 agent beats the talking team: +12.8 pts, resolved.

## E2: can a difference between agents replace the channel? (2026-11-19)

| Arm (no channel) | Score | Difference to `indep-3` | Resolved |
|---|---|---|---|
| `indep-3` | 51.5% | | |
| `indep-3-salt`: each seat gets "Your random number is NNNN." | 56.3% | +4.8 pts | no (sign-flip p 0.078) |
| `indep-3-mixed`: DeepSeek Flash, Claude Haiku 5.5, GPT-6 Luna | 61.3% | +9.8 pts | yes (sign-flip p 0.004) |

INFERRED: a random number does not make identical agents act differently enough. The mixed arm changes 2
things at once (the agents differ, and 2 seats get other models). Haiku alone scored higher than DeepSeek
alone in the pilot, so part or all of the +9.8 pts can be model quality, not diversity. The mixed arm used
OpenRouter keys on 11-19, before Peter's DeepSeek-only rule.

## Why 1 agent with the budget of 3 beats the talking team

**F13 (stored E1 logs, $0).** Each trial's 27 opens split into new flags, new empty boxes, repeats and
unused opens.

| Arm | New flags | New empty | Repeats | Unused |
|---|---|---|---|---|
| `solo-x3` | 19.7 | 4.0 | 0 | 3.3 |
| `team-3` | 17.1 | 4.4 | 4.9 | 0.6 |

Scouting costs the same. The team loses its 2.6 flags to repeats. 69% of the repeats happen before the
first opener posts any message, 27% while the message is posted and not yet read, 4% after it is read.

**F14 and F14c (real agents, $1.00).** 1 added line in each team instruction: "Before you open a box, post its
number with POST /msg and read GET /msgs. Do not open a box that another agent posted first."

| Arm | Date | Trials | Score | Repeats | Messages | Real cost per trial |
|---|---|---|---|---|---|---|
| `team-3` | 11-19 | 30 | 85.7% | 4.9 | 14.0 | $0.012 |
| `team-3-rerun` | 11-21 | 20 | 83.8% | 5.7 | 14.3 | $0.013 |
| `team-3-reportfirst` | 11-20 | 30 | 98.8% | 0.9 | 34.1 | $0.025 |
| `solo-x3` | 11-19 | 30 | 98.5% | 0 | 0 | $0.002 |

OBSERVED: report-first against the rerun is +15.1 pts, resolved (sign-flip p 0.0075). The rerun against the
11-19 team is -1.9 pts, not resolved, so the plain team did not drift.

INFERRED: the talking team loses because agents act before their teammates report. An explicit
report-first rule closes the gap, but it doubles the messages and the cost. The single agent still costs
about 1/12 of the report-first team.

**F15 (real agents, $0.53): budget or context fragmentation?** Both arms have 27 opens in total. 2 new arms
change 1 factor each from `team-3`: `team-3-pool` gives the 3 seats 1 shared pool of 27 opens.
`team-3-shared` keeps 9 opens per seat, but every open answer lists every open of every seat.

| Arm | Score | Difference to `team-3-rerun` | Repeats | Opens per batch |
|---|---|---|---|---|
| `team-3-rerun` | 83.8% | | 5.7 | 2.5 |
| `team-3-shared` (no fragmentation) | 88.5% | +4.7 pts, not resolved | 3.9 | 2.4 |
| `team-3-pool` (pooled budget) | 93.2% | +9.5 pts, resolved on the tasks question only | 4.65 | 2.9 |
| `solo-x3-rerun` | 100.0% | +16.2 pts, resolved | 0 | 12.5 |

OBSERVED: neither arm closes the pre-registered 2/3 of the gap. In `team-3-shared` every repeat is a race:
43 of 78 happen before the earlier open reaches the actor; 35 come in the same batch of commands, before the
agent reads the answer that shows the box.

**F16 (real agents, $0.25): is the batch the cause?** `team-3-shared-single` adds 1 line to `team-3-shared`:
open 1 box per command, and read `team_opens` before the next choice. Agents follow it (1.02 opens per batch).
The score is 90.3%, +1.7 pts over `team-3-shared`, not resolved, and below the pre-registered 94.6%.
104 of its 111 repeats happen before the earlier open reaches the actor.

**F17 (real agents, $0.27): does a fixed role break the tie?** `team-3-roles` gives each seat its own order
inside every room (p1 from the lowest box, p2 from the highest, p3 from the middle), with no message rule.
The score is 94.5%, +10.8 pts over `team-3-rerun`, resolved on the tasks question. It misses the
pre-registered 94.6% by 0.1 pts, and report-first is still +4.3 pts above it (resolved on the tasks
question). Repeats fall from 5.7 to 2.8, but empty opens rise, because the seats still scout rooms on
their own.

**Answer to the question (INFERRED from F13 to F17).** 1 agent with the budget of 3 wins because it never
races itself. The team loses its flags to repeats: alike agents choose the same box at nearly the same time.
A view of past opens (F15, F16) removes few of them. A pooled budget (F15) recovers about 60% of the gap,
because the agent that knows where the flags are can use the opens that teammates did not spend; it does not
stop the repeats. A fixed order per seat (F17) removes half the repeats at no message cost and gets near.
Of the 5 changes tested, only a claim before the act (F14) closes the gap: 98.8% against 100.0%, with 0.9
repeats per trial, at about 12 times the cost of the single agent.

## E4: when no agent has all the facts (2026-11-21)

`ledger`: warehouses A, B and C each keep a ledger of 12 rows over 5 items, and every item is in at least 2
ledgers. The team reports the total of each item over all 3 ledgers. The first accepted answer, from any agent,
is final. Score: the share of the 5 totals that are exactly right. Pre-registered in `PREREGISTRATION.md`
(Amendment 6) before the first trial. 10 instances, 2 attempts, 20 trials per arm, the same seats as E1.

| Arm | Who reads what | Score | Messages | Answers sent | Real cost per trial |
|---|---|---|---|---|---|
| `solo` | 1 agent reads A, B and C | 100.0% | 0 | 1.00 | $0.0008 |
| `team-3` | each agent reads 1 ledger, channel | 100.0% | 4.85 | 2.55 | $0.0041 |
| `indep-3` | each agent reads 1 ledger, no channel | 0.0% | 0 | 2.35 | $0.0304 |

- OBSERVED: `solo` minus `team-3` is 0 (95% interval [0, 0]). Test 1 (team at least 2/3 of solo) passes.
  Test 2 (premature answers explain the losses) does not apply, because the team lost no points.
- OBSERVED: in all 20 `team-3` trials, the answering agent had read the reports of both other agents before it
  answered (`premature` 0 in 20 of 20). The first answer came after 3 to 6 posts. The answering agent changed:
  p3 10 times, p2 8 times, p1 2 times. The later answers were refused, as the rules say.
- OBSERVED: `indep-3` cannot pool, and scores 0 in 20 of 20 trials (the expected floor; no leak). Its agents
  spend their turns on the problem and cost 7 times as much as the team.
- INFERRED: passing 5 numbers from each agent is easy for this model, and the first-answer-is-final rule did
  not tempt any agent to answer early. In `boxes` the team loses because agents act on the same boxes at the
  same time (F13), not because information does not arrive. A harder `ledger` (more items, a message limit,
  or a deadline) is needed to find where pooling starts to fail. The team still costs 5 times as much as 1
  agent that holds all facts, so when 1 agent can hold the facts, it is the cheaper choice.

## E5: more seats at the same total budget (2026-11-23)

`crowd`: the `boxes` world and layouts with 24 opens in total, split equally over k seats (24, 12, 8, 6 or 3
each). Pre-registered in `PREREGISTRATION.md` (Amendments 7 and 7a) after an independent review of the plan;
Amendment 9 (reruns and the order after the pull limit) was written after the primary arms were seen. Seats:
terminus-2 with DeepSeek V4.1 Flash through OpenRouter, thinking off. Runner: Daytona sandboxes. 20 trials for
each complete arm (`indep-2` 18). An independent audit recomputed the numbers from the event logs. Details,
deviations and the audit: F19, F20 and `LOG.md`.

| Arm | Seats x opens | Score | Repeats (of 24 opens) | Unused opens | Messages | Cost per trial |
|---|---|---|---|---|---|---|
| `solo-24` | 1 x 24 | 100.0% | 0.00 | 0.65 | 0 | $0.0017 |
| `team-2` | 2 x 12 | 84.0% | 2.35 | 0.45 | 6.5 | $0.0059 |
| `team-3` | 3 x 8 | 79.2% | 3.45 | 0.40 | 12.4 | $0.0084 |
| `team-4` | 4 x 6 | 69.2% | 5.45 | 0.65 | 18.8 | $0.0123 |
| `team-8` | 8 x 3 | 54.2% | 8.40 | 0.15 | 31.2 | $0.0178 |
| `claim-8` | 8 x 3, post-first rule | 73.5% | 1.25 | 2.60 | 78.4 | $0.0502 |
| `indep-8` | 8 x 3, no channel | 25.8% | 13.85 | 0.00 | 0 | $0.0123 |

- OBSERVED: the first primary test passes. The `team-k` score falls by 15.5 points for each doubling of k
  (95% interval [-20.1, -10.7], exact sign-flip p 0.002, slopes for each of the 10 layouts).
- OBSERVED: the second primary test fails. `solo-24` minus `claim-8` is 26.5 points [20.8, 32.3], far above
  the 5-point margin. The rule still adds 19.2 points [14.0, 24.5] to the plain team of 8.
- OBSERVED: the team of 8 repeats 8.4 of its 24 opens; 8 silent agents repeat 13.85. The count of repeats
  before any agent saw a report stays at 0.8 to 1.65 for each trial, and most of the growth is in later
  repeats (0.70 at 2 seats, 7.00 at 8). None of the 168 repeats of `team-8` came after the agent had read a
  report of that box: for 82% no report existed yet, and for 18% it existed but was not read. 75% of the
  repeats hit the lowest closed box of a room. The median repeat comes 4.2 s after the first open of the box;
  the median report comes after 7.4 s.
- OBSERVED: every team of 3 or more writes a plan, mostly before the first open. A team of 8 writes 5.75
  plans from 4.4 authors in a trial. An agent's first open obeys the plan it read in 84% of cases at 4 seats
  and in 59% at 8. Plans for 6 rooms leave seats 7 and 8 without a room.
- OBSERVED: under the post-first rule, 31 of 160 agents stop with opens left. 28 of them say that every
  remaining treasure box is claimed, while 7.8 flag boxes are still closed. Only 31% of claims end in an open
  by the claimant.
- INFERRED: within E5, the loss grows with the number of agents that race for the same next step: when a
  treasure room is known, the agents take the lowest closed box in it, inside the 7 s before a report exists.
  A written claim turns the race into a queue that blocks. The next candidate is a rule that gives each box 1
  owner without a message for each box (a fixed share by seat name, 1 agent that assigns, or a claim that the
  world enforces and that expires). No such rule was tested; E7 is built for it (`LOG.md`).
- OBSERVED, 2 seats: `team-2` 84.0%, 2 silent agents (`indep-2`) 67.5%, 1 agent with 12 opens (`solo-12`)
  45.2%, and the distinct flags of 2 separate `solo-12` runs 52.0%. The channel adds 16.5 points [5.3, 29.2]
  even at 2 seats, where best silent play can score 1.0.
- Not done: the arms without a channel at 3 and 4 seats have 7 and 11 of 20 trials. The claim rule at 2, 3 and
  4 seats, the single-agent arms with 3, 6 and 8 opens (for the union control) and the route anchor have no
  result. Docker Hub's pull limit failed 111 trials (F21). After the fix, the last credits of the shared
  OpenRouter account went to the reruns of `solo-12` and `indep-2`, and then the account was empty.
- E6 `field` (agents added to a fixed large world of 240 boxes, 9 opens for each agent, with the single agent
  that has the whole budget as control) is pre-registered as Amendment 8. Its family, its analysis and its
  pilot exist. 1 of 13 scored arms is complete: 1 agent with 72 opens scores 70.5%, which is 93.5% of best
  scripted play. The pilot (1 trial each, not scored) gave 8 talking agents 53.8% and 8 silent agents 16.2%.
- E7 `crowdrules` (rule paragraphs that give each box 1 owner: a share by seat number, 1 coordinator, a fixed
  plan by seat number; 8 and 4 seats) is pre-registered as Amendment 10 after an independent review. Its
  family, its scripted checks and its analysis exist. 0 model trials.
- Seat count of the runner (F22, scripted trials, no model): 48 seats in 1 trial pass on the small Daytona
  sandbox, also 3 trials at once. At 64 to 80 seats, 4 of 11 trials lost 1 seat and 3 of them gave a reward
  of 0.26 to 0.30 in place of 1.0 with no error in `result.json`. 120 seats pass on a sandbox with 4 vCPU and
  8 GiB when the build does not use Compose Bake.

## How this fits other work

- Anthropic Fellows, "Worse Together" (2026-11): messages reach the committing agent in 94% to 99% of episodes,
  but enter its context before the commit in only 47% to 67%. A guard that checks for pending messages before
  the commit recovers 62% to 73% of failed episodes. This is the same race as our repeats in F15, and the
  report-first rule of F14 is a guard of that kind.
  https://alignment.anthropic.com/2026/worse-together/ (local copy:
  `research-context/multi-agent/raw/scrapes/alignment.anthropic.com/2026-11-19-2026-worse-together.md`)
- Bespoke Labs, Multi Agent Coordination Bench (2026-11): a team of 8 costs 8.8 times as much as 1 agent and
  scores 0.42 against 0.35; 1 agent gives the most score per dollar in every task and model pair. E1 agrees:
  `solo-x3` costs $0.002 per trial, `team-3` $0.012.
  https://benchmarks.bespokelabs.ai/multi-agent-coordination-bench/ (local copy:
  `research-context/multi-agent/sources/posts/2026-11-18-multi-agent-coordination-bench.md`)
  E5 adds the control that the benchmark lacks, a fixed total budget: there the team of 8 scores 54% against
  100% for 1 agent, and talking adds 28.5 points over 8 silent agents. On MAC Bench at 8 agents, on the 3
  tasks that have a silent team (9 task and model cells), talking teams beat silent teams by +0.046; on the
  27 cells with a best-of-8 control they lose by 0.039 (recomputed from the page data in
  `research-context/multi-agent/2026-11-23-agent-count-scaling-designs-and-evidence.md`).
- "Test-Time Compute Scales Better with Parallel Agent Teams" (Duke, Modal, Princeton, NYU, 2026-11) reports
  the other direction on long tasks. At equal cost, 1 agent that works 12 hours scores 52.7 and a team of 2
  that works 6 hours each scores about 5.4 points above 1 agent at 6 hours (Graph Node Classification).
  Longer sessions barely help there. INFERRED: in `boxes`, 1 agent with 27 opens loses nothing to a long
  session, because the task is short and its whole state fits in 1 context. So `boxes` favors 1 agent.
  https://arxiv.org/html/2611.09798 (local copy:
  `research-context/multi-agent/sources/papers/2026-11-06-parallel-agent-teams-compute-scaling.md`)

## Limits

- **1 harness:** terminus-2 in every seat. Other harnesses may read the channel or plan differently.
- **1 main model:** DeepSeek Flash with thinking off. The only other models are 2 seats of 1 E2 arm and
  2 single pilot trials. A stronger model or thinking on can change the size of every gap.
- **2 synthetic tasks:** `boxes` has 1 obvious split of the work (rooms), free messages, and a reward that
  only counts distinct flags. It rewards a team for not repeating work, and for nothing else. `ledger` has 5
  items and free messages, and is at its ceiling for the team. Shared pools (`commons`) and honest claims
  (`scarce`) exist in muster but have no scored model results yet.
- **Small samples:** 10 instances; 20 or 30 trials per arm. The design detects differences of about 12 to 16
  pts, so smaller real effects can show as "not resolved".
- **Time:** E1 and E2 ran on 11-19, F14 on 11-20, F14c to F17 on 11-21, E5 on 11-23. F14c checks for drift
  for the plain team only. E5 used another route to the model (OpenRouter to DeepInfra) and another backend
  (Daytona) and has no anchor arm, so its levels must not be compared with the earlier levels.
- **Prompt protocols:** report-first and the F15 to F17 changes are written rules. They measure the value of
  that rule, not what agents find without it.
- **2 runners:** the fork of Harbor at `6aeb609e`, on local Docker on 1 Mac (E1 to E4) and on Daytona
  sandboxes (E5, E6).
- **E5 is at 1 total budget (24 opens).** At 8 seats each agent has 3 opens and there are more seats than
  rooms, so the game differs in kind from 2 to 4 seats. E5 equalizes opens, not tokens or time.
- **E5 reruns and secondary tests.** 21 of the 200 E5 trials are reruns that ran 12 to 27 minutes later
  through another sandbox image route; 6 of the 10 `indep-2` instances have rerun trials only. The secondary
  contrasts have no correction for their number. The message classes of F20 come from keyword rules that
  agree with a hand check on 87% to 95% of sampled messages.

## Cost

| Run | Trials | Real cost |
|---|---|---|
| Pilot (11-19, not scored) | 8 | $0.38 |
| E1 and E2 (11-19) | 210 | $1.22 |
| F14 (11-20) | 30 | $0.74 |
| F14c (11-21) | 20 | $0.26 |
| F15 (11-21) | 60 | $0.53 |
| F16 (11-21) | 20 | $0.25 |
| F17 (11-21) | 20 | $0.27 |
| E4 `ledger` (11-21, pilot included) | 63 | $0.72 |
| E5 `crowd` (11-23; 313 started, 200 with a reward) | 200 | $2.68 |
| E5 pilots (11-23, not scored) | 8 | $0.13 |
| E6 `field` (11-23; 1 arm and the pilot) | 24 | $0.18 |
| **Total** | **663** | **$7.36** |

Real cost until E4 is computed from tokens at DeepSeek's price for the hour of the run; Harbor records the
peak price. From E5 the route is OpenRouter, and the recorded cost is the billed cost. E5 and E6 also used
11.9 hours of Daytona sandbox time, about $2.00 at list price (not checked against a bill). The scripted
checks for E7 and the seat-count probe of F22 used about $0.45 more.
