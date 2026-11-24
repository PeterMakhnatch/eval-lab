# Findings

Dated observations from real runs on `olamlabs/harbor-multiagent` at commit `6aeb609e`, local Docker on macOS.
OBSERVED means a run or a file shows it. INFERRED means our reasoning. Run outputs stay outside Git.
Since 2026-11-21 muster lives in Eval Lab. Paths written earlier refer to the old muster repository:
`docs/` and `experiments/` are now this folder, `tasks/` is `muster/tasks/`, `src/muster/` is `muster/`.

## F1. The upstream example pays a tested seat that did nothing (2026-11-19, $0)

OBSERVED. `muster matrix examples/tasks/hello-seats --policies oracle,idle,spoof --attempts 2`: the roster
`p1=idle,p2=spoof` gets reward 1 in 2 of 2 trials. Seat `p1` is under test and does nothing. Seat `p2`
posts the note with `"author": "p1"`. The world trusts the `author` field, and the verifier checks only that
a note with that author exists and that each seat left a log file.
The branch `flotilla/hello-seats-identity` (world-issued tokens) gives 0 for the same roster.

## F2. Seats on 1 network can talk without the channel (2026-11-19, $0)

OBSERVED. With all seats on 1 Compose network, a seat read a file that another seat served on a port.
So a condition "without a channel" was not silent. muster's kit gives each seat its own internal network.
A probe trial shows: the world answers, no other seat answers, the internet does not answer.

## F3. Docker runs out of IPv4 networks at about 30 (2026-11-19, $0)

OBSERVED. On Docker Desktop with default address pools, 11 more networks could be created when 26 existed.
Then: "all predefined address pools have been fully subnetted". 1 trial of a control matrix failed so.
A trial with 3 seats on own networks needs 3 networks. IPv6-only internal networks use no IPv4 pool.
The kit now uses them, and a dual-stack world server.

## F4. Verifiers that write no reward when a seat fails (2026-11-19, $0)

OBSERVED in control matrices on 3 seat tasks of other lanes: `kit-check` (7 of 8 rosters),
`cramped-room-parity` (3 of 4), `dispatch-handoff` (3 of 4) end with `RewardFileNotFoundError` or a
timeout when a seat is `nop`.
INFERRED. Harbor's job mean leaves an invalid trial out. A team then looks better than it is. A verifier
should write reward 0 when the evidence is missing.

## F5. Harbor skips the check of participants before a run (2026-11-19, $0)

OBSERVED. With a missing model key for seat `p2`, `main` builds the environment, installs both agents and
plays for 28 s, then fails. The branch `muster/preflight-participants` fails in about 2 s. Draft PR #13 in
the fork.

## F6. Seat model keys are not scrubbed, and wrapped keys survive the scrub (2026-11-19, $0.06)

OBSERVED. A real OpenRouter key stayed in a seat log for about 8 minutes (then redacted). 2 causes:
(a) the scrub set reads only the first seat's resolved key (draft PR #33 in the fork);
(b) mini-swe-agent prints the key in a config list wrapped at 80 columns, and the scrub matches exact
strings only (branch `muster/redact-wrapped-secrets`).

## F7. The job summary names only the first tested seat (2026-11-19, $0)

OBSERVED. A job with roster `p1=oracle, p2=nop` prints `adhoc • oracle` and groups the trials under the agent
of `p1`. `result.json` of the job has no roster. Use `seat_results` of each trial.

## F8. Pilot with a real model: what agents do in `boxes` (2026-11-19, $0.38, not scored)

OBSERVED, 1 trial for each arm, instance 1, terminus-2 with DeepSeek Flash.

- Thinking on, no channel: each of the 3 agents invented a split of the rooms (reasoning up to 130,000
  characters in 1 step). Reward 0.95, cost $0.148.
- Thinking off, no channel: 2 of 3 agents opened the same boxes first. Reward 0.75, cost $0.012.
- With a channel: 15 to 20 messages. 13 of 25 actions happened while a message of another agent was posted
  and not yet read. Reward 0.85 to 0.90.
- Without a channel, agents also called routes that do not exist (`/score`, `/state`, `/info`), to look
  for one.

INFERRED. 1 trial per arm cannot rank the arms. E1 (pre-registered, 210 trials) answers that question.

## F9. In `scarce`, a false claim of need pays, and the world log proves each lie (2026-11-19, $0)

OBSERVED. `muster matrix tasks/scarce/build/team-3/scarce-i01 --policies oracle,liar` on real Docker,
8 rosters. The `liar` policy claims a need of 1 and then takes its true need. In every roster with 1 or 2
liars, each liar scores 1, the team reward stays 1.0, and an honest seat takes 0. With 3 liars the takes
overdraw the pool, and the reward is 0. The verifier counts every lie exactly (`false_claims` 1, 2 and 3).
The `spoof` policy (other seats' needs, a second take, acting as another seat) gains nothing in 8 rosters.

INFERRED. The family measures the incentive to misreport without a judge. A model team that is honest and
cooperative yields in the seat with the largest need. A model that lies is visible in `false_claims`.

## F10. An independent replication of the flotilla lane's coordination table (2026-11-19, $0)

OBSERVED. `tidegauge decompose --task-key` on copies of 276 trials that the flotilla lane ran (schedule task,
12 instances, 1 trial per instance and condition, 4 models, terminus-2). On the main wave (c1), tidegauge
gives the same scores, gains, 95% intervals, exact p values and cost values as the lane's own table.

| Model (c1) | Coordination (talk - silent) | Exact p | Total (talk - solo) | Exact p |
|---|---|---|---|---|
| DeepSeek V4 Flash | +91.7 pts | 0.0010 | not resolved | 0.25 to 1.00 for all 4 |
| GLM 4.7 Flash | +33.3 pts | 0.1250 | not resolved | |
| GPT-OSS-120B | +50.0 pts | 0.0703 | not resolved | |
| Qwen 3.6 35B-A3B | +50.0 pts | 0.0703 | not resolved | |

- The exact test resolves the coordination gain only for DeepSeek. The paired t-test calls the 3 other gains
  resolved.
- No talking team is resolved as different from 1 agent that has all the information.
- In this design "headcount" (silent - solo) measures the split of the information over 2 seats: it removes
  33 to 100 pts.

Since PeterMakhnatch/tidegauge#3, a tasks verdict needs the t-test and the exact sign-flip test to agree.
With that rule, `decompose` resolves the c1 coordination gain only for DeepSeek.

INFERRED. With 12 differences of -1, 0 or +1, read the exact test, not the t-test. 12 instances with 1 trial
each detect only coordination effects of about 25 to 59 pts. Full report: `muster-runs/flotilla-decompose/REPORT.md`
(local, not in Git).

## F11. E1: in `boxes`, talking adds 34 points, but 1 agent with the same budget beats the team (2026-11-19, 11:00 to 11:46 PM ET, $1.22)

Pre-registered (`experiments/PREREGISTRATION.md`). terminus-2 with DeepSeek Flash, thinking off, in every seat.
10 instances, 3 attempts each, 30 trials per arm, 04:00 to 04:46 UTC. Every trial counts (`--trials all`).

OBSERVED.

| Arm | Score (95% rerun) | Duplicate opens | Real cost per trial |
|---|---|---|---|
| 1 agent, 9 opens (`solo-a`) | 30.8% (±3.6) | 0 | $0.003 |
| The same, again (`solo-b`) | 32.5% (±2.5) | 0 | $0.003 |
| 3 agents, no channel (`indep-3`) | 51.5% (±5.1) | 10.7 of 27 | $0.007 |
| 3 agents, channel (`team-3`) | 85.7% (±4.0) | 4.9 of 26 | $0.012 |
| 1 agent, 27 opens (`solo-x3`) | 98.5% (±6.5) | 0 | $0.002 |

- `tidegauge decompose`: headcount +20.7 pts (95% rerun +14.8 to +26.6), coordination +34.2 pts (+28.0 to +40.3),
  total +54.8 pts. All 3 are resolved for both questions; the sign-flip p is 0.002. Coordination gives 62% of the gain.
- The noise floor holds: `solo-b` minus `solo-a` is +1.7 pts, not resolved.
- With the same total budget, 1 agent beats the team that talks: `solo-x3` minus `team-3` is +12.8 pts, resolved.
- Without a channel, 2 or more agents start with the same box in 87% of the trials, and 40% of all opens repeat
  another agent's open. With a channel, agents send 14 messages per trial. 82% of their actions happen while a
  message of another agent is posted and not yet read, and repeats fall to 4.9 of 26.
- All 210 rewards of E1 and E2 replay exactly from their event logs (`muster regrade`).

INFERRED. More agents help here mostly because they bring more budget. Talking recovers part of the work that
identical agents repeat, but it does not reach 1 agent with the same budget. This agrees in direction with
"Worse Together" and MAC Bench. It is 1 model, 1 harness and 1 synthetic task.

## F12. E2: a random number does not separate identical agents; 3 different models do better (2026-11-19)

OBSERVED. Against `indep-3` (51.5%): 1 extra line "Your random number is NNNN." per seat gives 56.3%,
+4.8 pts, not resolved (sign-flip p 0.078); same first box falls from 87% to 67% of the trials.
3 different models (DeepSeek Flash, Claude Haiku 5.5, GPT-6 Luna) give 61.3%, +9.8 pts, resolved
(rerun p 0.011, sign-flip p 0.004), although the 3 models still start with the same box in 93% of the trials.

INFERRED. The mixed arm changes 2 things at once: the agents differ, and 2 seats get other models. In the pilot,
Haiku alone scored 0.45 and DeepSeek alone 0.25, so part or all of the +9.8 pts can be model quality, not
diversity. A follow-up needs a 3-seat arm of each model alone.

## F13. Why 1 agent with the budget of 3 beats the talking team: repeated opens, before the report exists (2026-11-20, $0)

Exact, from the graded event logs of E1 (no new run). `experiments/analysis/budget_identity.py` splits each
trial's 27 opens into new flags, new empty boxes, repeats of a box that some seat opened before, and unused
opens. For each repeat it asks what the actor knew when it opened the box.

OBSERVED (means over 30 trials per arm):

| Arm | New flags | New empty | Repeats | Unused |
|---|---|---|---|---|
| 1 agent, 27 opens (`solo-x3`) | 19.7 | 4.0 | 0 | 3.3 |
| 3 agents, channel (`team-3`) | 17.1 | 4.4 | 4.9 | 0.6 |
| 3 agents, no channel (`indep-3`) | 10.3 | 5.9 | 10.7 | 0.1 |

- Scouting costs the same: 4.0 and 4.4 empty boxes. The team loses its 2.6 flags (12.8 pts) to repeats.
- If every repeat of the team had been a new flag, its mean reward would be 0.998, above the 0.985 of the
  single agent. In 29 of 30 team trials, the repeats are at least as many as the missing flags. The 3 team
  trials with 0 repeats score 1.0.
- Of the team's repeats, 69% happen before the first opener posts any message after its open, 27% while that
  message is posted and not yet read by the actor, and 4% after the actor read it.
- Every team seat reads the message list before its first open. The single agent stops when it has all
  20 flags and leaves 3.3 opens unused.

INFERRED. The channel works when agents read it, and they do. The team loses because agents act before
their teammates report, not because they ignore reports. A rule that delays acting until a report arrives,
or that gives each agent a disjoint part of the boxes, would remove most of the gap. A next experiment can
test this at about $0.35 (30 trials of a `team-3` variant whose instruction says that every open is posted).

## F14. When agents post each box before they open it, the team matches the single agent (2026-11-20 and 11-21, $1.00)

Pre-registered in `experiments/LOG.md` before each run. DeepSeek Flash, thinking off, the E1 roster, off-peak.
Arm `team-3-reportfirst` is E1 `team-3` with 1 added line: "Before you open a box, post its number with
POST /msg and read GET /msgs. Do not open a box that another agent posted first." Arm `team-3-rerun` is
E1 `team-3` without change, run again on 11-21 as a control for drift over time.

OBSERVED:

| Arm | Trials | Score | Repeats per trial | Messages per trial | Real cost per trial |
|---|---|---|---|---|---|
| E1 `team-3` (11-19) | 30 | 85.7% | 4.9 | 14.0 | $0.012 |
| `team-3-rerun` (11-21) | 20 | 83.8% | 5.7 | 14.3 | $0.013 |
| `team-3-reportfirst` (11-20) | 30 | 98.8% | 0.9 | 34.1 | $0.025 |
| E1 `solo-x3`, 1 agent with 27 opens | 30 | 98.5% | 0 | 0 | $0.002 |

- `tidegauge compare`, `team-3-rerun` against `team-3-reportfirst`: +15.1 pts, resolved for both questions
  (tasks interval +6.6 to +23.6, sign-flip p 0.0075). E1 `team-3` against `team-3-rerun`: -1.9 pts, not
  resolved (p 0.66).
- All 50 new rewards replay exactly from their event logs. No key in the outputs.

INFERRED. This confirms the F13 mechanism: the team lost because agents acted before they reported, not
because they lacked ability. The rerun shows that the gain is not drift of the model or the provider. The
added line is an explicit protocol, so the result measures that protocol, not what agents find alone. The
protocol doubles the messages and the cost, and the single agent still costs about 1/12 of the team.

## F15. Budget against context fragmentation: each explains only part of the gap (2026-11-21, $0.53)

Pre-registered in `experiments/LOG.md` (1:25 AM ET). Same roster, instances and channel as `team-3`;
20 trials per arm. 2 arms change 1 factor each. `team-3-pool`: the 3 seats share 1 pool of 27 opens.
`team-3-shared`: 9 opens per seat, and each open answer lists every open of every seat (`team_opens`).

OBSERVED:

| Arm | Score | Difference to `team-3-rerun` | New flags | Repeats | Opens per batch |
|---|---|---|---|---|---|
| `team-3-rerun` (F14c) | 83.8% | | 16.75 | 5.7 | 2.5 |
| `team-3-shared` | 88.5% | +4.7 pts, not resolved (sign-flip p 0.44) | 17.7 | 3.9 | 2.4 |
| `team-3-pool` | 93.2% | +9.5 pts, tasks question resolved (p 0.032), rerun question not | 18.65 | 4.65 | 2.9 |
| `solo-x3-rerun` | 100.0% | +16.2 pts, resolved (sign-flip p 0.008) | 20.0 | 0 | 12.5 |

- Neither arm reaches the pre-registered 2/3 of the gap (94.6%).
- In `team-3-shared`, all 78 repeats are races. 43 happen before the earlier open reaches the actor. The other
  35 happen 0 to 1.5 s after the answer that shows the box: the agent sent several opens in 1 batch of
  commands and did not read the answer in between. (A batch is opens of 1 seat less than 2 s apart.)
- In `team-3-pool`, repeats stay, but the seat that finds a treasure room keeps opening there (the busiest
  seat makes 15.6 opens on average), so fewer flags stay out of reach.

INFERRED. Repeats are the root cause, and pooling only compensates for them. With a pool, the seat that knows
where the flags are can use opens that its teammates did not spend, so repeats cost fewer flags. F14 shows that
the split of 9 opens per seat is enough when repeats are rare (98.8% with 0.9 repeats). Showing every open
removes few repeats. F16 tests whether batches of opens are the reason.

## F16. 1 open at a time with the shared view does not stop repeats (2026-11-21, $0.25)

Pre-registered in `experiments/LOG.md` (2:31 AM ET). Arm `team-3-shared-single`: the `team-3-shared` tasks plus
1 line: "Open only 1 box per command. Read its answer, with `team_opens`, before you choose the next box. Do
not open a box that `team_opens` already lists." 20 trials.

OBSERVED:

| Arm | Score | Repeats | Opens per batch | Repeats after the earlier open was shown |
|---|---|---|---|---|
| `team-3-shared` (F15) | 88.5% | 3.9 | 2.4 | 35 of 78 |
| `team-3-shared-single` | 90.3% | 5.55 | 1.02 | 7 of 111 |
| `team-3-reportfirst` (F14) | 98.8% | 0.9 | 1.2 | |

- Agents follow the rule (1.02 opens per batch). The score is +1.7 pts over `team-3-shared`, not resolved
  (sign-flip p 0.55). It stays below the pre-registered 94.6%.
- 104 of the 111 repeats happen before the earlier open reaches the actor. 93% of the repeats (103 of 111)
  are the lowest unopened box of the room as far as the actor knew; in `team-3-reportfirst` only 52% are.

INFERRED. Batches explained which repeats looked avoidable in F15, but not the loss. The main cause is that
alike agents choose the same box at nearly the same time. A view of what happened cannot prevent that; a
claim before the act (F14) can, because the second agent reads the claim before it chooses. This agrees with
the commit races in Anthropic's "Worse Together" (see `docs/RESULTS.md`).

## F17. A fixed order per seat closes most of the gap, but a claim still does better (2026-11-21, $0.27)

Pre-registered in `experiments/LOG.md` (3:08 AM ET). Arm `team-3-roles`: the plain `team-3` tasks plus 1 line per
seat with its order inside every room: p1 from the lowest box upward, p2 from the highest downward, p3 from
the middle outward. Nothing about which rooms to scout. 20 trials.

OBSERVED:

| Arm | Score | New empty | Repeats | Obvious next box |
|---|---|---|---|---|
| `team-3-rerun` | 83.8% | 3.95 | 5.7 | 69% |
| `team-3-roles` | 94.5% | 4.65 | 2.8 | 35% |
| `team-3-reportfirst` (F14) | 98.8% | 3.33 | 0.9 | 54% |

- +10.8 pts over `team-3-rerun`, resolved on the tasks question (sign-flip p 0.040), not on the rerun
  question (p 0.059). `team-3-reportfirst` is +4.3 pts above it, resolved on the tasks question (p 0.011).
- 94.5% is 0.1 pts below the pre-registered 94.6%, so the test fails.
- 91% of the opens follow the seat's order. Repeats fall by half; empty opens rise by 0.7.

INFERRED. A fixed role breaks the tie between alike agents inside a room, without more messages. (E2's random
number per seat, without a channel, gave no resolved gain.) It does not split the scouting, so seats spend
more opens on empty rooms.
A claim before the act does both, at the price of about 2.4 times the messages.

## F18. E4: with split facts the talking team scores like 1 agent with all facts (2026-11-21, $0.72)

Pre-registered in `PREREGISTRATION.md` (Amendment 6). Family `ledger`, arms `solo`, `indep-3`, `team-3`, 10
instances x 2 attempts, roster `t2-deepseek-flash-plain`. Analysis: `analysis/e4_ledger.py`.

OBSERVED:

| Arm | Score | Messages | `premature` | Answers sent | Real cost |
|---|---|---|---|---|---|
| `solo` | 1.000 | 0 | 0 | 1.00 | $0.016 |
| `team-3` | 1.000 | 4.85 | 0 | 2.55 | $0.083 |
| `indep-3` | 0.000 | 0 | 1 | 2.35 | $0.609 |

- `solo` minus `team-3`: 0, 95% interval [0, 0], not resolved. Test 1 passes. Test 2 does not apply (no losses).
- `seen_before_answer` is 2 in all 20 team trials: the answering seat had read both other seats.
- `indep-3` scores 0 in 20 of 20: no leak between seats without the channel.

INFERRED. The cost of split information is 0 here: the task is at the team's ceiling. Together with F13, the
team's losses in `boxes` come from simultaneous action on shared boxes, not from information transfer. The team
costs 5 times as much as `solo` for the same score.

## F19. E5: at the same total budget, a talking team loses 15 points for each doubling of its size (2026-11-23, $2.68)

Pre-registered in `PREREGISTRATION.md` (Amendments 7 and 7a) before the first scored trial. Amendment 9 (the
rerun rule and the order after the pull limit) was written at 3:50 PM ET, after the primary arms were done and
seen, and before the first rerun. Family `crowd`: the `boxes` world and its 10 layouts, 24 opens in total,
split equally over k seats. Roster `t2-or-flash-deepinfra` (terminus-2, DeepSeek V4.1 Flash through OpenRouter,
thinking off), at most 30 turns for each seat. Runner: the fork at `6aeb609e` on Daytona sandboxes (Docker
inside the sandbox, 2 vCPU, 4 GiB). Analysis: `analysis/crowd_scaling.py`. 10 instances x 2 attempts for each
arm. An independent audit recomputed every number of this finding from the event logs with its own code
(`LOG.md`, 2026-11-23).

OBSERVED (the 8 complete arms, 20 trials each, 0 trial or seat exceptions; 140 of 140 rewards of the first 7
replay with `muster regrade` against the clean landed task tree):

| Arm | Seats x opens | Score | New flags | New empty | Repeats | Unused | Messages | Cost per trial |
|---|---|---|---|---|---|---|---|---|
| `solo-24` | 1 x 24 | 100.0% | 20.00 | 3.35 | 0.00 | 0.65 | 0 | $0.0017 |
| `team-2` | 2 x 12 | 84.0% | 16.80 | 4.40 | 2.35 | 0.45 | 6.5 | $0.0059 |
| `team-3` | 3 x 8 | 79.2% | 15.85 | 4.30 | 3.45 | 0.40 | 12.4 | $0.0084 |
| `team-4` | 4 x 6 | 69.2% | 13.85 | 4.05 | 5.45 | 0.65 | 18.8 | $0.0123 |
| `team-8` | 8 x 3 | 54.2% | 10.85 | 4.60 | 8.40 | 0.15 | 31.2 | $0.0178 |
| `claim-8` | 8 x 3, claim rule | 73.5% | 14.70 | 5.45 | 1.25 | 2.60 | 78.4 | $0.0502 |
| `indep-8` | 8 x 3, no channel | 25.8% | 5.15 | 5.00 | 13.85 | 0.00 | 0 | $0.0123 |
| `solo-12` | 1 x 12 | 45.2% | 9.05 | 2.95 | 0.00 | 0.00 | 0 | $0.0035 |

The 4 middle columns sum to 24 in every row (`solo-12`: 12). Best scripted play scores 1.0 in every `team-k`
and `claim-k` arm, 0.70 in `indep-8` and 0.50 in `solo-12`.

- **Primary test 1 passes.** For each instance, the least-squares slope of the `team-k` score on log2 k over
  k = 2, 3, 4, 8. Mean slope -15.5 points for each doubling, 95% interval [-20.1, -10.7], exact sign-flip
  p 0.002.
  Not pre-registered, a check after the result: without the 8-seat arm (k = 2, 3, 4 only) the slope is -14.3
  points [-25.5, -3.0], p 0.047. So the fall is not made by the 8-seat arm alone, where the game has more
  seats than rooms.
- **Primary test 2 fails.** `solo-24 - claim-8` = 26.5 points [20.8, 32.3]. The lower end is above the
  5-point margin, so the claim rule does not make 8 agents match 1 agent.
- `solo-24 - team-k`: 16.0 [7.3, 24.8], 20.8 [14.0, 28.0], 30.8 [22.5, 38.0] and 45.8 [43.2, 48.5] points at
  k = 2, 3, 4, 8. All have sign-flip p below 0.02.
- The channel still helps at 8 seats: `team-8 - indep-8` = 28.5 points [22.0, 35.2]. The claim rule helps too:
  `claim-8 - team-8` = 19.2 points [14.0, 24.5].
- Repeats grow by about 1 for each added seat: 2.35, 3.45, 5.45 and 8.40 of the 24 opens (8 silent agents:
  13.85). For scale, computed: 8 agents that pick 3 boxes each at random from all 60 would repeat 3.8 opens,
  and 8 agents that pick at random among the 20 flag boxes would repeat about 9.5. Neither is a fair chance
  level, so the data do not show that the team does worse than chance.
- Cost: `team-8` costs about 11 times as much as `solo-24` for a trial, `claim-8` 30 times. A Daytona trial
  took 108 s (`solo-24`) to 253 s (`claim-8`). 1 seat reached the 30-turn cap (in `claim-8`, after it had used
  its 3 opens). No seat reached the 600 s time limit.
- The pre-registered expectations: `team-2` 95% (observed 84.0%), `team-8` 70% (54.2%), `indep-8` 10% to 20%
  (25.8%), `claim-8` not a pass (it fails). I was too optimistic for every team arm.

More arms:

| Arm | Trials with a reward | Planned | Score | Status |
|---|---|---|---|---|
| `indep-2` | 18 | 20 | 67.5% | counted: 2 reruns failed when the account was empty |
| `indep-3` | 7 | 20 | 46.4% | incomplete |
| `indep-4` | 11 | 20 | 42.5% | incomplete |
| `claim-4` | 4 | 20 | 87.5% | incomplete |
| `claim-2`, `solo-8` | 0 | 20, 30 | - | started; every trial failed at the pull limit |
| `claim-3`, `team-3-e1`, `solo-6`, `solo-3` | 0 | 20 to 80 | - | not run |

- With 2 seats: `team-2 - indep-2` = 16.5 points [5.3, 29.2] (the channel helps, although best silent play
  can score 1.0); `indep-2 - solo-12` = 22.2 points [14.7, 29.0]; `indep-2 - union-2` = 15.5 points
  [4.2, 25.0], where `union-2` (52.0%) is the distinct flags of 2 separate `solo-12` runs. With the 2
  unmeasured `indep-2` trials counted as 0, these are 23.5 [12.0, 35.2], 15.2 [4.5, 25.8] and 8.5
  [-5.5, 21.8]. So "2 silent agents with different names cover more than 2 copies of 1 agent" is not robust.
- The incomplete arms were stopped by the Docker Hub pull limit (F21) and then by an empty OpenRouter account.
  The pre-registration says that their contrasts are not available; their scores are for the record only.
- E5 has no route anchor (`team-3-e1`) and no union control above 2 seats. Levels must not be compared with
  E1 to F18 (other route, other backend, 27 opens there).
  Later (night of 2026-11-24): the incomplete arms, the route anchor and the union control are complete; see
  F25.
- Counting: 313 trials were started. 200 have a reward. 111 failed at the pull limit before any seat started,
  and 2 failed when the account was empty. Failed trials are unmeasured, never 0. 23 of the failed trials were
  run again (`solo-12` 7, `indep-2` 16); 21 of the reruns have a reward and are in the table. The reruns ran 12
  to 27 minutes after the first runs, through the mirror snapshot (F21). 6 of the 10 `indep-2` instances have
  rerun trials only. Paired by instance, rerun minus first run is 0.0 points for `solo-12` (7 pairs) and +3.8
  for `indep-2` (4 pairs).
- Deviations from the pre-registration:
  - Up to 20 E5 trials ran at once (5 lanes from 3:31 PM ET), not 8. Median model latency by arm was 2.4 s to
    2.6 s and is not ordered by k.
  - The order of Amendment 7a was not kept inside the lanes: `team-4` started before `claim-8` and `team-2`,
    and `solo-12` (last in the order) started before `indep-2`, `indep-3`, `claim-4` and `claim-2`. So
    `solo-12` had 13 rewards before the pull limit and `indep-2` had 4.
  - Amendment 9 put the single-agent arms before the reruns of `indep-k`. I ran both groups at once. The last
    credits went to the `solo-12` and `indep-2` reruns; `solo-8` was not run again, and `solo-6` and `solo-3`
    never started.
  - Amendment 9 moved `claim-2`, `claim-3` and `claim-4` behind E6 after the `claim-8` result was known.
  - The task copy has untracked Python 3.12 bytecode files that the landed tree lacks (`LOG.md`, 3:10 PM).
    Every tracked file is equal, and the images run Python 3.13.
- The secondary contrasts have no correction for their number (Amendment 7).

INFERRED. Within E5, the loss of a talking team grows with the number of seats when the work is held fixed,
and it grows in the count of repeated opens. A free channel recovers 28.5 of the 74.2 points that 8 silent
agents lose (about 40%). The claim rule recovers 19.2 of the remaining 45.8 points (about 40%), at 2.8 times
the cost of the plain team. 1 agent beats every team size tested and is the cheapest. The single steps from 2
to 3 seats (4.8 points) and from 3 to 4 seats (10.0 points) are not resolved alone; the step from 4 to 8 seats
is (15.0 points, [6.8, 24.2]).

## F20. Why 8 agents lose: a race for the next free box, plans that bind no one, and claims that block boxes (2026-11-23, $0)

Analysis of the E5 event logs, `analysis/crowd_messages.py` (the same output on each run; its message classes
are keyword rules that agree with a hand check on 35 of 40 plans, 37 of 40 claims and 38 of 40 empty-room
messages, so counts of plans and claims have an error of about 5% to 13%). Each lost flag is a repeat, an
extra empty box, or an unused open, counted against `solo-24`.

OBSERVED:

| Measure | `team-2` | `team-3` | `team-4` | `team-8` | `claim-8` |
|---|---|---|---|---|---|
| Repeats for each trial | 2.35 | 3.45 | 5.45 | 8.40 | 1.25 |
| of these, in round 1 (before any seat saw a report) | 1.65 | 0.80 | 1.10 | 1.40 | 0.15 |
| of these, later | 0.70 | 2.65 | 4.35 | 7.00 | 1.10 |
| Plans (messages that give seats a target) for each trial | 1.35 | 5.00 | 6.05 | 5.75 | 2.30 |
| Plans that conflict with an earlier plan | 0.35 | 3.05 | 4.00 | 2.75 | 0.80 |
| A seat's first open after a plan that names it obeys the plan | 71% | 85% | 84% | 59% | 50% |
| Median time from an open to its report | 13.2 s | 10.1 s | 8.7 s | 7.4 s | 7.1 s |
| Messages and reads for each open | - | - | - | 1.3 and 1.3 | 3.7 and 4.3 |

- The count of repeats in round 1 stays at 0.8 to 1.65 for each trial. But round 1 holds fewer opens at larger
  k (13.3, 6.8, 5.3 and 5.8), so the repeat rate in round 1 doubles (0.12 to 0.24 for each open). Most of the
  growth in the count is in later repeats (0.70 to 7.00).
- In `team-8`, none of the 168 repeats came after the repeater had read a report of that box (a post that
  names the box, by a seat that had opened it). For 137 (82%) no report existed yet. For 31 (18%) a report
  existed, but the repeater had not read it. 44 repeats (26%) came after the repeater had read another kind of
  message that names the box, such as a plan or an intent. 73% of the repeats come after both treasure rooms
  had an open flag, 80% land on flag boxes, and 75% hit the lowest closed box of the room. The median repeat
  comes 4.2 s after the first open of the box; the median report comes after 7.4 s.
- Every `team-3`, `team-4` and `team-8` trial has a plan, usually before the first open (at about 15 s). At 8
  seats, 4.4 authors post 5.75 plans in a trial. Plans for 6 rooms have no room for seats 7 and 8: in 107 of 142
  cases a plan keeps them in reserve or adds them to a room that another seat has.
- The seat number becomes the room number. In `team-8`, the first box of seat p1 is box 1 in 90% of trials, of
  p2 box 11 in 65%, of p3 box 21 in 70%, of p4 box 31 in 50%, of p5 box 41 in 60%, of p6 box 51 in 70%. Seats
  p7 and p8 have no such box (their most frequent first box has 15% and 21%). Without a channel (`indep-8`)
  every seat opens box 1 first in 50% to 80% of trials.
- `claim-8`: 31 of 160 seats stop with opens left (52 opens). 28 of the 31 final texts say that every
  remaining treasure box is claimed. At that moment 7.8 flag boxes are still closed, and only 0.97 of them
  were never named in a message. A trial has 63.5 claim posts for 26 boxes; 15.4 boxes have 2 or more
  claimants and 4.9 of those are never opened. Only 31% of claims end in an open by the claimant (`team-8`:
  90% of its few claims).
- Share of the lost points. In `team-8` (45.8 points lost): repeats before a report could be read 34.5 points
  (75%), repeats that follow a plan that overlaps another seat's open 7.5 (16%), extra empty boxes 6.3 (14%);
  `team-8` uses 0.5 more opens than `solo-24`, worth -2.5 points, so the parts sum to 45.8. In `claim-8`
  (26.5 points lost): extra empty boxes 10.5 (40%), unused opens 9.8 (37%), repeats 6.3 (24%).

Example (`team-8`, `crowd-i07__RLX8rfF`): box 2 was opened by p7 (seq 98), p3 (105), p8 (108) and p2 (112);
p7's report came at seq 103. Example (`claim-8`, `crowd-i10__YyAX4QU`, p7's last text): "The first mention for
every box in the two confirmed treasure rooms was made by another agent. [...] Our own three opens remain
unused".

INFERRED. The mechanism is the race of F13 at every size, with more racers: once a treasure room is known,
each agent takes the lowest closed box in it, inside the 7 s before a report exists. What changes in kind at 8
seats is the plan: more seats than rooms breaks the "1 seat for each room" plan, and nobody owns the extra
seats. The claim rule replaces the race by a queue in which every mention reserves a box for ever, so the
queue blocks. The next candidate is a rule that gives each box 1 owner without a message for each box (a
fixed share by seat name, or 1 agent that assigns), or a claim that the world enforces and that expires. No
such rule was tested here; E7 should test it.

## F21. Daytona for multi-seat trials: 20 at once work, Docker Hub's pull limit stops them, a mirror fixes it (2026-11-23, about $2.00 of sandbox time)

OBSERVED:

- Muster trials run on Daytona with no change to tasks or to Harbor: the fork's `--env daytona` starts Docker
  inside a sandbox and builds the same compose project. 13 scripted trials equal the local reference values,
  and a probe policy shows that seats cannot reach each other or the internet (21 checks for each seat with
  the plain image; 20 of 20 in a later run with the mirror snapshot).
- With model seats, up to 20 E5 trials (at most 96 seats) ran at once; with the E6 pilot, 24. The 179 trials
  that started before the pull limit (35.5 minutes) had 0 trial or seat exceptions. A trial with 8 seats fits
  in 2 vCPU and 4 GiB. In 1 paired trial the agent phase took 115 s on Daytona and 43 s on local Docker (the
  whole trial 173 s and 62 s); the 2 trials made different model calls, so this is not a clean latency
  measure. Sandbox time: 11.9 hours for 355 trials, about $2.00 at list price (not checked against a bill), so
  $0.006 for a trial. That is as much as the model cost of a small trial.
- From 20:38Z, 111 trials failed before any seat started: `docker compose build` got `429 Too Many Requests`
  from Docker Hub for `python:3.13-slim`. Every sandbox pulls the base image again, and the sandboxes share
  Daytona's addresses.
- Fix: a Daytona snapshot (`muster-dind-mirror-v1`) of the same `docker:28.3.3-dind` image with
  `registry-mirrors: https://mirror.gcr.io` in `/etc/docker/daemon.json`, passed as
  `--ek dind_snapshot=muster-dind-mirror-v1`. A control sandbox without it still failed at the same minute.
- Trap: with a snapshot, Harbor passes no resources to Daytona. `--override-cpus` and `--override-memory-mb`
  are written to `result.json` but have no effect.
- The same kit runs unchanged on upstream head `711ca65a` (16 scripted trials, the same rewards, regrade 16
  of 16).

INFERRED. A cloud backend removes the 2-trial limit of the laptop: E5's first 179 trials took 36 minutes. The
pull limit is a property of "Docker inside a fresh sandbox" on any provider, so a registry mirror
(or a prebuilt image) belongs in the default setup. A fork PR adds a `registry_mirrors` option and a warning
for the resources trap (see `LOG.md`).

## F22. Seats in 1 trial: 48 pass on the small sandbox, 64 to 80 can give a wrong score without an error, 120 pass on a larger sandbox (2026-11-23, $0.37 of sandbox time)

A probe with the scripted `oracle` policy, no model call. 37 trials on Daytona (Docker inside 1 sandbox, 1
container for each seat plus the world). The tasks are scratch copies outside the repo: the `field` world,
`team-k` for k = 8 to 120, 3 opens for each seat (2 at 120 seats). Runner: `muster run` at `e2a07d8fd` with the
fork at `6aeb609e`. Report, scripts and run folders: `muster-runs/infra-evidence-2026-11-23/seat-scale/`.

OBSERVED:

| Seats | Sandbox | Trials at once | Trials with the reference reward | Trial time | Peak memory | First failure |
|---|---|---|---|---|---|---|
| 8 | 2 vCPU, 4 GiB | 1 | 3 of 3 | 71 s | 0.7 GiB | none |
| 16 | 2 vCPU, 4 GiB | 1 | 2 of 2 | 76 s | 1.0 GiB | none |
| 24 | 2 vCPU, 4 GiB | 1 | 2 of 2 | 90 s | 1.4 GiB | none |
| 32 | 2 vCPU, 4 GiB | 1 | 2 of 2 | 112 s | 1.7 GiB | none |
| 48 | 2 vCPU, 4 GiB | 1 | 2 of 2 | 129 s | 2.2 GiB | none |
| 48 | 2 vCPU, 4 GiB | 3 | 3 of 3 | 174 s | 2.3 GiB | none |
| 64 | 2 vCPU, 4 GiB | 1 | 1 of 1 | 234 s | 3.0 GiB | none |
| 64 | 2 vCPU, 4 GiB | 3 | 2 of 3 | 208 s | 2.9 GiB | 1 seat crashed; reward 0.26; no error in `result.json` |
| 80 | 2 vCPU, 4 GiB | 1 | 1 of 1 | 212 s | 3.7 GiB | none |
| 80 | 2 vCPU, 4 GiB | 3 | 3 of 6 | 236 s | 3.7 GiB | 1 seat crashed in each; rewards 0.99, 0.30, 0.28 |
| 120 | 2 vCPU, 4 GiB | 1 | 0 of 2 | - | 4.0 GiB | image build failure; out of memory |
| 80 | 4 vCPU, 8 GiB | 3 | 3 of 3 | 152 s | 3.5 GiB | none |
| 120 | 4 vCPU, 8 GiB | 1 | 0 of 2 | - | - | image build failure (`no such job`) |
| 120, `COMPOSE_BAKE=false` | 4 vCPU, 8 GiB | 2, then 3 | 5 of 5 | 197 s | 5.2 GiB | none |

- No code refuses more than 8 seats. `muster run` and the roster took 8 to 120 seats.
- **A wrong score without an error.** In 4 of 11 trials at 64 to 80 seats on the small sandbox, 1 seat died
  with `ConnectionResetError`. `result.json` has no exception for the trial or for the seat. In 3 of the 4
  the dead seat was a scout, the other seats waited and stopped, and the reward was 0.26 to 0.30 in place of
  1.0. The only trace is `seats/<seat>/agent/exit-code.txt`. This is the fork at `6aeb609e`. Upstream head
  `711ca65a` records the crash of a scripted seat in the trial result (upstream commit `f42969ac`).
- The world server of the kit keeps the default listen queue of 5 connections. The kernel counted 0 dropped
  connections at 8 to 24 seats, 5 to 10 at 32 to 48 seats and 171 to 610 at 80 to 120 seats. On 4 vCPU, 0 of 8
  probe trials at 80 to 120 seats had a seat crash. In a later run of 2 trials at 120 seats on 4 vCPU (the
  proof for fork PR #101), 1 trial lost 2 seats and scored 0.19. On 2 vCPU the CPU was throttled in 21% to
  69% of the periods.
- At 120 seats, `docker compose build` failed in 3 of 4 trials (`NotFound ... no such job`): the kit has 1
  build target for each seat. With `COMPOSE_BAKE=false`, 5 of 5 builds passed. The fork sets this value for
  its kernel Docker environment and not for Daytona.
- Memory grows by about 40 MiB for each seat. The fork opens 1 session for each command and never closes it:
  the sandbox holds exactly 15 x seats + 34 `sh` processes (1834 at 120 seats).
- `docker compose up` takes 11 s at 8 seats and 103 s at 120 seats on 2 vCPU. The fork's limit for this step
  is a fixed 120 s.
- A second snapshot exists for large trials: `muster-dind-mirror-4c8g` (4 vCPU, 8 GiB, the same mirror).

INFERRED:
- E5 to E7 are not in the failing range: they have at most 8 seats, where the kernel counted 0 dropped
  connections.
- The seat crash needs the short listen queue, and a starved CPU makes it frequent: 4 of 11 trials on 2
  vCPU, 1 of 10 on 4 vCPU. No packet trace was taken.
- A trial with model agents has a lower limit than these scripted trials, because a model seat uses more
  memory and more commands.
- The wrong score without an error is the dangerous part: above 48 seats on a small sandbox, an
  infrastructure fault looks like a weak team. An experiment in that range needs the larger sandbox, a check
  of every seat's exit code before a reward counts, and a kit version with a longer listen queue
  (`muster/tasks/` is frozen, so that is a new kit version).
- Fork PRs (drafts on upstream head): #101 sets `COMPOSE_BAKE=false` for Daytona (at 120 seats the build
  passed in 2 of 2 trials on the branch and failed in 1 control trial on head); #102 adds a test that a crashed
  scripted seat is recorded. After a sync of the fork's `main` to upstream head, the crash is an error in the
  trial result. The score of such a trial is still wrong, so it must not count.

## F23. E6: with its own budget for each agent, 8 talking agents beat 8 silent agents by 14 points and lose to 1 agent with the whole budget by 24 points (2026-11-23, $3.36)

Pre-registered in `PREREGISTRATION.md` (Amendments 8 and 8a; the route and the floor of Amendment 9) before
the first scored trial. Family `field`: 240 boxes in 24 rooms of 10, 8 treasure rooms, reward = distinct flag
boxes / 80, 10 layouts. Every seat has 9 opens, so the total budget grows with the number of seats, as in the
published sweeps. The added control is 1 agent with the whole budget of the team. Roster, turn cap and
backend as in E5. 10 instances x 2 attempts for each arm (`solo-9`: 8 attempts). Analysis:
`analysis/crowd_scaling.py --per-seat-budget 9`. An independent audit recomputed the numbers before this text
(`LOG.md`).

OBSERVED (13 arms, 320 trials, every trial has a reward, 0 reruns):

| Arm | Seats x opens | Score | Share of best scripted play | New flags | Repeats | Unused | Messages | Cost per trial |
|---|---|---|---|---|---|---|---|---|
| `solo-9` | 1 x 9 | 7.7% | 0.88 | 6.15 | 0.01 | 0.00 | 0 | $0.0024 |
| `solo-18` | 1 x 18 | 15.4% | 0.89 | 12.35 | 0.10 | 0.00 | 0 | $0.0033 |
| `solo-36` | 1 x 36 | 27.9% | 0.74 | 22.35 | 0.00 | 2.25 | 0 | $0.0035 |
| `solo-72` | 1 x 72 | 70.5% | 0.94 | 56.40 | 2.45 | 0.00 | 0 | $0.0048 |
| `indep-2` | 2 x 9, no channel | 13.1% | 0.70 | 10.45 | 2.45 | 0.00 | 0 | $0.0069 |
| `indep-4` | 4 x 9, no channel | 21.1% | 0.61 | 16.85 | 9.90 | 0.00 | 0 | $0.0139 |
| `indep-8` | 8 x 9, no channel | 32.0% | 0.52 | 25.60 | 31.10 | 0.00 | 0 | $0.0248 |
| `team-2` | 2 x 9 | 12.9% | 0.71 | 10.35 | 1.30 | 0.00 | 9.0 | $0.0066 |
| `team-4` | 4 x 9 | 23.5% | 0.65 | 18.80 | 2.65 | 0.15 | 17.3 | $0.0125 |
| `team-8` | 8 x 9 | 46.4% | 0.62 | 37.10 | 13.60 | 6.45 | 30.7 | $0.0243 |
| `scout-2` | 2 x 9, scout rule | 11.4% | 0.63 | 9.15 | 0.70 | 0.00 | 16.9 | $0.0083 |
| `scout-4` | 4 x 9, scout rule | 14.8% | 0.41 | 11.85 | 7.05 | 2.55 | 40.9 | $0.0157 |
| `scout-8` | 8 x 9, scout rule | 40.8% | 0.55 | 32.65 | 17.70 | 5.50 | 64.6 | $0.0339 |

- **Primary 1 is resolved: talk helps at 8 seats.** `team-8 - indep-8` = 14.4 points [8.9, 20.8], sign-flip
  p 0.002.
- **Primary 2: 1 agent is better.** `solo-72 - team-8` = 24.1 points [16.6, 31.8], p 0.004. The single agent
  is ahead on 9 of the 10 layouts.
- **Primary 3 passes: the team falls behind 1 agent as it grows.** (`solo-72 - team-8`) - (`solo-18 -
  team-2`) = 21.6 points [13.6, 28.8], p 0.004.
- The gaps of the published sweeps, at k = 2, 4, 8:
  - More silent agents score more (`indep-k - solo-9`): 5.4, 13.4 and 24.3 points. This gap mixes 2 things:
    more agents, and a text that tells each seat that other agents exist.
  - Talk adds nothing at 2 seats and much at 8 (`team-k - indep-k`): -0.1 [-2.4, 2.5], 2.4 [-0.4, 5.1],
    14.4 [8.9, 20.8].
  - The control that the sweeps lack (`team-k - solo-9k`): -2.5 [-4.9, 0.3], -4.4 [-8.4, -0.6] with test
    p 0.08, and -24.1 [-31.8, -16.6]. Only the gap at 8 seats is resolved by the interval and by the test.
- The scout rule does not help: `scout-k - team-k` = -1.5 [-3.9, 1.2], -8.7 [-11.2, -5.7] and
  -5.6 [-12.8, 0.9]. At 4 seats it is worse than no channel (`scout-4 - indep-4` = -6.2 [-8.4, -3.9]).
  50 scout seats stop with opens left (24 in `scout-4` with 51 opens, 26 in `scout-8` with 110 opens). In all
  50, the last message says that all treasure rooms are known and that the score cannot rise. That belief is
  false: the score counts boxes, not rooms. It explains at most 3.2 points at 4 seats and 6.9 points at 8.
- Silent agents that know of the other agents spread over the rooms. `indep-k - union-k` = 3.4 [1.7, 5.3],
  10.3 [8.1, 12.8], 20.5 [15.5, 25.9], where `union-k` (9.6%, 10.8%, 11.5%) is the distinct flags of k
  separate `solo-9` runs. A single agent starts in room 1 in 95% to 100% of trials; a silent seat in 59% to
  64%. The silent text says that other agents exist; the solo text says "You work alone".
- Teams split the world by seat number without a rule. In `team-4` the first box of p2, p3 and p4 is box 61,
  121 and 181 in 70% of trials each. In `team-8`, 23% of the seats that opened a box start in room 1.
- `team-8` has 272 repeats in 20 trials. 90% come less than 30 s after the first open of the box. The
  repeater had read a post that names the box in 25% of them. The first opener had reported the box or its
  room before the repeat in 26%.
- Single agents with a large budget open by script: 95% of the opens of `solo-72` come less than 1 s after
  the open before (`team-8`: 9%). 1 `solo-72` trial scored 0.20 because its script repeated 47 boxes.
- Cost: `team-8` costs 5 times as much as `solo-72` for a trial and scores 24 points less.
- **A fault of the model route in `team-8`.** The first 8 trials of the arm started together. 14 of their 64
  seats made 1 to 3 model calls and 0 opens, got no model response after 7:09 PM ET, and ended at the 600 s
  limit. 6 trials have such a seat. 126 of the 129 unused opens of the arm belong to these seats. No other
  arm has a seat at the 600 s limit. Amendment 8 counts these trials, so the table does. A check after the
  result, without the 6 trials (14 trials, all 10 instances): `team-8` 49.0%; primary 1 = 17.0 [12.9, 22.6];
  primary 2 = 21.5 [14.2, 27.8]; primary 3 = 19.0 [11.2, 25.4]; `scout-8 - team-8` = -8.2 [-15.4, -0.9],
  p 0.07. No primary verdict changes.
- Seats at the 30-turn cap with opens left: 2, both in `solo-36` (32 opens). Seats with 0 model calls: 0.
- Process and deviations:
  - `solo-72` ran from 3:50 PM to 4:04 PM ET with 4 trials at once, at the same time as 2 E5 rerun lanes: 12
    trials at once at the peak, not 8. 1 of its trials logged an error for exhausted credits after its 72
    opens were done. Its median model call took 2.6 s (the later solo arms: 1.6 s to 1.9 s); every seat used
    all 72 opens.
  - The other 12 arms ran from 7:08 PM to 9:23 PM ET in 1 lane with 8 trials at once, in the registered
    order. All 320 trials used the mirror snapshot and the same Harbor checkout.
  - The E6 pilot ran on the route without the mirror snapshot, so it did not test the scored route.
  - The floor was $1 (Amendment 9). The cost is $3.36 against an expected $3; it is computed from tokens at
    the list price and is not checked against the bill.
- The pre-registered expectations: `solo-9` near 9% (7.7%), `solo-72` 55% to 70% (70.5%), `indep-8` 15% to
  25% (32.0%), `team-8` 35% to 45% (46.4%), `scout-8` 5 to 10 points above `team-8` (5.6 points below). The 3
  primary verdicts are as expected. I was wrong about the scout rule and too low for the silent team.

INFERRED. The published design (every agent has its own budget; compare the team with silent agents and with
1 agent that has 1 budget) makes a team look good: 46% against 8%, and talk adds 14 points. The same team
loses 24 points to 1 agent that has the same number of opens and costs a fifth. So a sweep over the number of
agents needs the control "1 agent with the whole budget" before it says that agents help. Limits of this
reading: E6 equalizes opens only, and a team of 8 has 8 times the turns of 1 agent. Part of the 24 points
comes from the 14 stalled seats (about 2.6 points) and part from the single agent's use of scripts. E6 has 1
model, 1 harness and 1 synthetic search world; it says nothing about the results of MAC Bench itself. E6
does not test why the scout rule fails.

## F24. E7: a fixed plan by seat number, or 1 coordinator, brings 8 agents from 58% to 94% to 97%; a share of the boxes by seat number without a plan does not help (2026-11-23, $2.03)

Pre-registered in `PREREGISTRATION.md` (Amendment 10 after an independent review, and Amendment 10a) before
the first scored trial. Family `crowdrules`: the `crowd` world of E5, the same 10 layouts, 24 opens in total.
Each rule is 1 paragraph at the end of the `team-k` text. Roster, turn cap and backend as in E5. 2 lanes of 4
trials. 10 instances x 2 attempts for each arm. Analysis: `analysis/crowd_scaling.py` (RULES block) and
`analysis/crowd_rules.py`, both landed before the pilot. An independent audit recomputed the numbers before
this text (`LOG.md`).

OBSERVED (9 arms, 180 trials, every trial has a reward, 0 stalled seats, 0 reruns):

| Arm | Rule | Score | New flags | New empty | Repeats | Unused | Messages | Cost per trial |
|---|---|---|---|---|---|---|---|---|
| `solo-24` | 1 agent with 24 opens | 100.0% | 20.00 | 3.65 | 0.00 | 0.35 | 0 | $0.0015 |
| `team-8` | none (the E5 text) | 58.2% | 11.65 | 4.85 | 7.50 | 0.00 | 30.6 | $0.0204 |
| `zones-8` | a fixed plan by seat number | 97.0% | 19.40 | 4.60 | 0.00 | 0.00 | 13.0 | $0.0074 |
| `chief-8` | p1 gives each agent its boxes | 94.3% | 18.85 | 4.45 | 0.10 | 0.60 | 26.0 | $0.0168 |
| `share-8` | box n belongs to agent ((n - 1) mod 8) + 1 | 45.0% | 9.00 | 14.60 | 0.00 | 0.40 | 32.1 | $0.0172 |
| `team-4` | none | 71.2% | 14.25 | 4.25 | 5.05 | 0.45 | 17.0 | $0.0105 |
| `zones-4` | a fixed plan by seat number | 95.5% | 19.10 | 4.90 | 0.00 | 0.00 | 15.9 | $0.0053 |
| `chief-4` | p1 gives each agent its boxes | 74.8% | 14.95 | 8.70 | 0.10 | 0.25 | 19.9 | $0.0099 |
| `share-4` | box n belongs to agent ((n - 1) mod 4) + 1 | 63.0% | 12.60 | 10.20 | 0.00 | 1.20 | 30.5 | $0.0123 |

- **Primary, 8 seats, Holm's rule over 3 tests:**
  - `zones-8 - team-8` = 38.8 points [34.2, 43.8], p 0.002: the rule helps.
  - `chief-8 - team-8` = 36.0 points [31.2, 40.8], p 0.002: the rule helps.
  - `share-8 - team-8` = -13.2 points [-25.2, -1.3], p 0.074: not resolved. The interval is below 0, but the
    test does not pass Holm's rule.
- Against 1 agent (5-point margin, secondary): `solo-24 - zones-8` = 3.0 [1.0, 5.3] and `solo-24 - chief-8` =
  5.7 [2.3, 10.2]. Both are inconclusive: 1 agent is higher, no rule passes the margin, and neither of the 2
  fails it. `solo-24 - share-8` = 55.0 [43.3, 66.0] fails.
- 4 seats (secondary): `zones-4 - team-4` = 24.2 [16.8, 32.2], p 0.002. `chief-4 - team-4` = 3.5
  [-7.2, 13.8] and `share-4 - team-4` = -8.2 [-17.0, 1.0] are not resolved.
- Between rules (secondary, no correction): `zones-8 - chief-8` = 2.7 [-1.2, 8.0], not resolved. `zones-4 -
  chief-4` = 20.8 [9.0, 34.0].
- All 3 rules remove the repeats: 0.00 to 0.10 for each trial, against 7.50 (`team-8`) and 5.05 (`team-4`).
- **The share rule is obeyed and does not help.** 472 of 472 opens in `share-8` and 456 of 456 in `share-4`
  are on the seat's own boxes. But 14.6 and 10.2 opens for each trial are own boxes in rooms without
  treasure. 92% and 85% of those come before the team's opens could show which 2 rooms hold treasure. 11.0
  and 7.4 flag boxes for each trial stay closed; for 216 of 220 and 130 of 148 of them the owner has no open
  left. The rule gives each box 1 owner and gives nobody the order of the search.
- Obedience of `zones`: 97.5% [96, 99] at 8 seats and 96.3% [93, 99] at 4 seats. The opens outside the rule
  (12 and 18) are in a wrong room, and 25 of the 30 are 1 error of arithmetic: the seat used box = 10 x room
  + position, not 10 x (room - 1) + position. A team that never saw the rule would be at 25% and 34%
  (`team-8` and `team-4` of E7, scored as if the rule applied).
- Obedience of `chief` is **not measured** (the pre-registered rule): the parser could not read p1's newest
  list for 165 of 468 opens at 8 seats (35%) and 129 of 475 at 4 seats (27%), above the 20% limit. Counts
  only: of the opens that could be classed, 303 of 303 (8 seats) and 336 of 346 (4 seats) are in p1's newest
  list. The blind hand check of 40 p1 posts found 0 wrong lists, 23 exact lists, 11 lists that a person can
  read and the parser refused, and 6 posts without a list. It also found that the parser does not see an
  order to 1 seat ("p4: open box 16, then box 17") as a list: 5 of 10 control posts.
  Post hoc (2026-11-24, not the registered result): parser v2 (`crowd_rules.py --parser v2`, commits
  `f1ed0404c` and `01f6f2b3e`) reads more forms of a list. It was tuned on 34 E7 posts (34 exact, 0 wrong)
  and checked on 60 other posts with blind labels: it read 1 of 48 lists wrongly before a fix and 0 after
  it. No unseen E7 post is left to test that fix. With v2 the opens that cannot be classed are 16.0% at 8
  seats and 10.7% at 4 seats, and obedience is 0.993 [0.98, 1.00] and 0.953 [0.88, 0.99].
- In all 40 `chief` trials p1 posted boxes for 2 or more seats before the first open of the trial. The first
  list of `chief-8` came after 19 s (median).
- **Why `chief-4` is weak: 1 reading of the rule text.** In 7 of 20 trials, p1's first list gave each of the 4
  seats 1 box in each of the 6 rooms: 24 boxes, all the opens. Each of these trials scores 0.40. The other 13
  trials average 93.5%, like `chief-8`. The sentence "First give the agents 1 box in each of the 6 rooms" can
  mean that each agent gets 1 box in every room. At 8 seats that reading is not possible (6 boxes, 3 opens).
- Seats that stop with opens left: 15 in `share-4` (24 opens), 8 in `chief-8` (12), 7 in `share-8` (8), 3 in
  `team-4` (9), 2 in `chief-4` (5). In `chief-8` most say that p1 gave them no further box. In the 4-seat
  arms several say that both treasure rooms are found, as if that completed the task.
- Cost and time: `zones-8` costs $0.0074 for a trial, about a third of `team-8` and 5 times `solo-24`. It
  uses 13 messages for each trial against 31. A `zones-8` trial takes 118 s, a `team-8` trial 164 s.
- The anchor: E7 `team-8` scores 58.2%; E5 `team-8` scored 54.2% (paired difference 4.0 [-1.7, 9.5]). The
  difference is below the 10 points of Amendment 10. E5 `team-8` ran before the mirror snapshot existed.
- The pre-registered expectations: `zones-8` near 85% (97.0%); `share-8` near 70% (45.0%); `chief-8` near
  65% (94.3%) and likely not resolved (it is resolved); all 3 rules beat `team-8` (2 of 3); no rule passes
  the 5-point margin against `solo-24` (true); the errors under `zones` are position counts (no: room
  numbers); obedience of `zones` above 90% (97.5%), of `share` near 85% (100%), of `chief` near 80% (not
  measured). I was wrong about `share` and about `chief`.
- Process and deviations:
  - Lane A ran `solo-24`, `zones-8`, `chief-8`, `zones-4`, `chief-4`; lane B ran `team-8`, `share-8`,
    `team-4`, `share-4`; 9:28 PM to 10:36 PM ET; at most 8 trials at once; no other muster trial ran in that
    time. Every seat has the provider pin, thinking off and the 30-turn cap; no seat reached the cap. The
    task copy equals the landed build. The stop rule did not fire.
  - Amendment 10a says that the 3 pilot trials were running at its commit. 2 of the 3 had ended, 79 s and
    21 s before; I had not read their results. The first scored trial started 12 s after the push.
  - I ran the analysis on the 8 complete arms at 10:30 PM ET, 5 minutes before `chief-4` ended. No arm, order
    or rule could change at that point.
  - The rerun script `e7_fill_lane.sh` was written after the lanes started and has smaller reserves than
    Amendment 10. It did not run: there were 0 reruns.

INFERRED. At 8 seats the loss of F19 is not a property of the team size. It goes away under a rule
paragraph that gives each open 1 owner AND fixes the order of the search, by a plan in the prompt (`zones`)
or by 1 agent that hands out boxes (`chief`). Ownership alone (`share`) removes every repeat and does not
raise the score, because nobody is told who explores. Limits: each rule paragraph also tells the seats to
post results and to read the board, so the gain belongs to the whole paragraph, not to ownership alone.
`zones` puts the solution into the prompt: it shows what the team can reach when the plan is given, not that
a team can find the plan. `chief` needs no knowledge of the layout in the prompt and works at 8 seats; at 4
seats its text has 2 readings, so `chief-4` does not show that a coordinator is worse for a small team. E7
has no primary test that ranks the rules; "share hurts" and "zones is the best rule" are not supported. No
rule is shown to reach the score of 1 agent. 1 model, 1 harness, 1 synthetic task, 10 layouts.

## F25. E5, the arms that were left open: the post-first rule helps at every team size, loses 13 points for each doubling, and is within 5 points of 1 agent only at 2 agents (2026-11-24, $1.68)

The arms of Amendment 7 that the money stop of 2026-11-23 left open, in the order of Amendments 9 and 10
(after E7). 1 lane, 4 trials at once, Daytona with the mirror snapshot, 10:45 PM ET on 2026-11-23 to 1:46 AM
ET on 2026-11-24. Roster, turn cap, task bytes and Harbor checkout as in E5. 249 new trials with a reward.
Analysis: `analysis/crowd_scaling.py --total-budget 24` over 17 arms (`muster-runs/e5/analysis/e5-full.txt`
and `.json`). An independent audit recomputed every number from the event logs before this text.

OBSERVED (score in %, 24 opens in total; `solo-24` scores 100.0):

| Agents k | 1 agent with 24/k opens | k silent agents (`indep-k`) | k talking agents (`team-k`) | Post-first rule (`claim-k`) | Union of k separate single agents |
|---|---|---|---|---|---|
| 2 | 45.2 | 67.5 (18 trials) | 84.0 | 98.2 | 52.0 |
| 3 | 26.3 | 42.5 | 79.2 | 94.3 | 38.5 |
| 4 | 19.8 | 38.8 | 69.2 | 86.8 | 27.0 |
| 8 | 6.9 | 25.8 | 54.2 | 73.5 | 9.0 |

| Arm | Trials | Repeats | Unused opens | Messages | Cost per trial | Seats at the 30-turn cap |
|---|---|---|---|---|---|---|
| `claim-2` | 20 | 0.25 | 0.95 | 29.8 | $0.0120 | 8 |
| `claim-3` | 20 | 0.65 | 0.35 | 32.5 | $0.0170 | 2 |
| `claim-4` | 20 | 0.95 | 1.70 | 52.1 | $0.0306 | 5 |
| `claim-8` (afternoon) | 20 | 1.25 | 2.60 | 78.4 | $0.0502 | 1 |
| `indep-3` | 20 | 9.90 | 0.00 | 0 | $0.0076 | 0 |
| `indep-4` | 20 | 10.90 | 0.00 | 0 | $0.0096 | 0 |
| `solo-8` / `solo-6` / `solo-3` | 30 / 40 / 80 | 0 | 0.00 / 0.12 / 0.00 | 0 | $0.0020 / $0.0023 / $0.0011 | 0 |

- **The post-first rule against 1 agent** (5-point margin, secondary): `solo-24 - claim-2` = 1.8 [0.0, 4.3],
  pass. `solo-24 - claim-3` = 5.8 [3.3, 8.5], inconclusive. `solo-24 - claim-4` = 13.2 [7.2, 20.0], fail.
  `solo-24 - claim-8` = 26.5 [20.8, 32.3], fail (the primary test of F19). These 4 are robust against the
  time of day: `solo-24` scores 100% in the afternoon and in the evening.
- **The rule against the plain team:** `claim-k - team-k` = 14.2 [4.0, 24.2] (p 0.039), 15.0 [7.5, 23.0]
  (p 0.0098), 17.5 [8.0, 26.0] (p 0.014) and 19.2 [14.0, 24.5] (p 0.002) at k = 2, 3, 4, 8.
- **The rule loses with team size:** the `claim-k` score falls by 12.8 points for each doubling of k
  [-15.6, -10.2], p 0.002 (secondary). The `indep-k` score falls by 19.4 [-24.9, -14.2]; `team-k` by 15.5
  (F19). No test compares the 3 slopes.
- **The blocking queue of F20 starts at 3 to 4 agents.** Seats that stop with opens left: `claim-2` 11 (19
  opens), `claim-3` 7 (7), `claim-4` 20 (34), `claim-8` 31 (52). At 4 agents 15 of 16 early stops cite
  claims ("all treasure-room boxes ... already claimed ... would violate the coordination rule"); at 3
  agents 5 of 6. At 2 agents the stops come after all 20 flags are found (5 of 6; 1 says "all 20" with 19
  found). The 30-turn cap also binds the claim arms, because each open needs a post and a read: in
  `claim-2`, 8 seats reach the cap and 5 of them have opens left (9 opens).
- **Talk helps at every size:** `team-k - indep-k` = 16.5 [5.3, 29.2], 36.8 [28.0, 44.0], 30.5 [20.0, 41.8]
  and 28.5 [22.0, 35.2].
- **Headcount at a fixed budget for each agent:** `indep-k - solo-(24/k)` = 22.2, 16.2, 19.0 and 18.9
  points. k silent agents with k times the opens beat 1 small agent.
- **The union control.** The distinct flags of k separate single agents with 24/k opens each (`union-k`) are
  52.0, 38.5, 27.0 and 9.0%. Each layout has exactly k such trials, so the value does not depend on a
  grouping. `indep-k - union-k` = 15.5 [4.2, 25.0], 4.0 [-3.8, 12.5], 11.8 [6.3, 18.0] and 16.8
  [13.2, 20.0]. The pre-registration calls this contrast "interference". That name is wrong: a silent seat
  never sees the effect of another seat, so `indep-k` and `union-k` differ only in the instruction text (a
  seat name, and "k - 1 other agents ... cannot communicate" against "You work alone"). The contrast
  measures how far that text makes silent agents spread out. `union-k` is at the floor of identical agents
  (50, 33.3, 23.3 and 8.7%): 99% of the `solo-3` trials start at box 1.
- **The route anchor.** `team-3-e1` (the frozen E1 `boxes` tasks for 3 agents, same task checksums in 10 of
  10 instances) scores 84.8% (20 trials, SE 3.3) on OpenRouter and Daytona. E1 `team-3` scored 85.7% and
  F14c 83.8% on DeepSeek's own API and local Docker. The pre-registered 10-point rule passes. The paired
  interval against E1 is [-11.4, 9.1], so the anchor cannot exclude a route difference of 10 points.
- **Time of day.** `team-k`, `claim-8`, `indep-8`, `indep-2`, `solo-24` and `solo-12` ran in the afternoon.
  `claim-2`, `claim-3`, `solo-8`, `solo-6`, `solo-3` and the anchor ran 8 hours later. `indep-3` (7 and 13
  trials), `indep-4` (11 and 9) and `claim-4` (4 and 16) are mixed. The checks for a shift: E7 `team-8`
  minus E5 `team-8` = +4.0 [-1.5, 9.75]; E7 `team-4` minus E5 `team-4` = +2.0 [-8.5, 11.75]; inside
  `indep-4`, the evening trials minus the afternoon trials of the same layouts = -8.3; the median model call
  was 10% to 15% faster in the evening. A shift of up to about 10 points in either direction is not
  excluded. The signs of `claim-k - team-k` (lower ends 4.0 to 8.0) and of `team-k - indep-k` at k = 3 and
  4 (lower ends 28.0 and 20.0) hold under such a shift. Their exact sizes, the differences between k, and
  the 2 new slopes do not.
- **The pre-registered expectations** (Amendment 7): the rule passes the margin at k = 2 (met), at k = 3 (not
  met: inconclusive) and at k = 4 (not met: fail). Messages grow about linearly in k for `team` (6.5 to
  31.2) and slower for `claim` (29.8 to 78.4).
- **Accounting and deviations:**
  - First runs: `solo-6` 40 (1 failed in `docker compose up` before the agent started and ran again once),
    `solo-3` 80, `claim-3` 20, `team-3-e1` 20. Reruns of trials that Docker Hub's pull limit failed before
    any model call (F21): `solo-8` 30, `claim-2` 20, `claim-4` 16, `indep-3` 13, `indep-4` 9. The folder
    names (`.r1-*`) do not show which is which. No trial ran again twice. No trial with a reward was
    replaced or dropped. 0 seats with an exception and 0 stalled seats.
  - `indep-2` keeps 18 trials. The 2 trials that failed at the empty account were reruns already, so under
    Amendment 7 they stay unmeasured; the rerun "if credits return" of Amendment 9 was not applied.
  - 1 `solo-6` seat wrote a script plan, used 1 of 6 opens and ended. The trial counts (reward 0.05).
  - Money: recorded cost over E5, E6 and E7 was $8.33 before the lane and $10.01 after it, under the $12
    cap. The lowest balance at an arm start was $14.35.

INFERRED. The post-first rule is a full fix at 2 agents and a partial fix above that. It removes the
repeats at every size (0.25 to 1.25 for each trial) and replaces them with boxes that are claimed and never
opened; that cost starts at 3 to 4 agents and grows. Not supported: "the claim rule fixes small teams" (it
is shown at 2 agents; 3 is inconclusive), "its gain grows with team size" (not tested, and the arms ran at
different times), "silent teams lose faster than talking teams" (the slopes are not compared), "union-k
measures interference". With E7: at 8 agents a fixed plan or a coordinator reaches 94% to 97%, the post-first
rule 73.5% (on another day). 1 model, 1 harness, 1 synthetic task, 10 layouts.

## F26. A blind coding of 96 agent transcripts: 42% of the agents without a rule repeat a box that they never saw reported; a script reproduces the counts for 27 arms (2026-11-24, no trial)

Not pre-registered: an analysis of the stored E5, E6 and E7 trials. Files:
`muster-runs/forensics-2026-11-24/` (`REPORT.md`, `codebook.md`, `ratings/`, `analysis.txt`, `detect.py`,
`all-arms/all-arms.txt`). Script: `analysis/seat_codes.py` (commit `ab8ccb8a6`).

OBSERVED:
- 96 seat transcripts (stratified over 10 arms) were rendered without the arm name, the score or the rule
  text and coded by 4 raters with a fixed codebook. The 4 raters are copies of 1 model; no person checked
  the codes. Agreement on the primary code: 0.84, kappa 0.81.
- A repeat of a box that the agent never saw reported (`DUP_UNSEEN`) is the primary code of 24 of 96
  transcripts: 42% of the agents without a rule and 0% of the agents under a rule of E7.
- A wrong report to the team (`MISREPORT`) is in 21 of 96 transcripts, 7 of the 10 from `share-8`. A wrong
  reading of the goal (`OBJECTIVE_MISREAD`, for example "both treasure rooms are found" as the end of the
  task) is in 8. An agent that follows a teammate's wrong statement without a check (`BLIND_OBEY`) is in 1.
- The event log shows that 39 of the 96 agents repeated a box. 10 of them got no repeat code from the
  raters: a transcript alone hides repeats.
- Rules that read only the event log (`detect.py`) against the raters: an early stop with opens left has
  precision 0.75 and recall 1.00; any repeat 0.74 and 0.88. The rules were tuned once on the same 96 items,
  so these values are optimistic.
- `seat_codes.py` applies the log rules to every seat of 27 arms. Examples: in E7 `team-8`, 59% of the seats
  repeat a box and 62% of the opens follow a board read by the same seat since its last open; in `share-8`,
  0 repeats, 98% of the opens follow a read, and 27% of the seats open boxes in rooms that the board already
  shows as empty. In E5 `team-8`, 118 repeats hit a box that no post read by the seat had named, against 43
  repeats of a box that a post read by the seat had named as opened, claimed or assigned (a plan or an
  intent counts here). F20 uses a narrower rule, a result report of the box by its first opener that the
  seat had read, and counts 0 of 168.

INFERRED. The main failure of these teams is an action on a view of the shared state that is 1 turn old,
not a refusal to read: most opens do follow a read. The behaviour probes of the `standin` family (a change
that arrives between a read and an open, a wrong list, a promised report, a claim that nobody uses) come
from these codes. Limits: 1 model as the rater, 96 items, rules tuned on the test items.
