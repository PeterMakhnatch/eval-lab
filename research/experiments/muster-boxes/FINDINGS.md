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
