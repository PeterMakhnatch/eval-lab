# Pre-registration: first muster experiments

Written 2026-11-19, 04:11 AM ET, before any scored trial with a model.
One unscored plumbing trial exists (hello-seats, 1 seat with a model, $0.004). It is not part of any result.

## Fixed for every experiment

- **Runner:** `olamlabs/harbor-multiagent` at commit `6aeb609e`, local Docker.
- **Harness and model:** 1 harness and 1 model in every seat of an arm. The roster files in
  `experiments/rosters/` record the exact settings. They do not change after the first scored trial.
- **Design:** 10 instances for each family, 3 attempts for each instance and arm. So 1 arm has 30 trials.
- **No early stop.** The number of trials is fixed. A second batch is a separate replication, reported apart.
- **Counting:** every trial counts. A trial without a valid reward scores 0 (`tidegauge --trials all`).
  A trial where no seat reached its model (provider error) is not a trial of the model: it is rerun, and
  the report says how many were rerun.
- **Analysis:** `tidegauge decompose` and `tidegauge compare`, 95% level, both questions (rerun and tasks).
  An effect is "resolved" only when the rerun interval excludes 0 and the test agrees.
- **Spending cap:** $20 for everything in this session. No new arm starts when the measured spend is $15.

## E1. boxes: more agents, or coordination?

Arms: `solo-a`, `solo-b` (the same condition twice), `solo-x3`, `indep-3`, `team-3`.

- **Primary metric:** `reward`, the share of the 20 flags that the team finds.
- **Primary effects:** headcount = `indep-3` minus `solo-a`. Coordination = `team-3` minus `indep-3`.
- **Controls:** `solo-b` minus `solo-a` measures the noise floor directly. `solo-x3` is 1 agent with the
  work budget of 3 agents.
- **Reference values:** see Amendment 1. `tasks/boxes/family.toml` has the exact numbers, computed by enumeration.
- **Directional expectations:** headcount effect above 0. Coordination effect above 0.
  `indep-3` is nearer to the score of 3 identical agents than to the score of the tacit convention,
  because agents of 1 model make the same choices.
- **Secondary, exploratory:** `duplicate_opens`, `empty_room_repeats`, `messages`, evenness of `seat.*`.

## E2. boxes: can a difference between agents replace a channel?

Arms, all without a channel: `indep-3` (from E1), `indep-3-salt` (each instruction has 1 more line with a
random number for that seat), `indep-3-mixed` (3 different models, if 3 model routes work).

- **Primary metric:** `reward`. **Primary effects:** each new arm minus `indep-3`.
- **Secondary:** `duplicate_opens`.
- **Directional expectation:** both new arms score above `indep-3` and below `team-3`.

## E3. commons: does a channel with pledges make a shared pool work?

Arms: `indep-3`, `team-3`.

- **Primary metric:** `reward`, the share of the pool that the seats get (0 after an overdraw).
- **Primary effect:** `team-3` minus `indep-3`.
- **Secondary:** `feasible`, `broken_pledges`, `kept_pledges`, `max_take`, `min_take`,
  `unseen_pledges_at_take`.
- **Directional expectation:** `team-3` above `indep-3`. At least 1 broken pledge in the 30 `team-3` trials.

## E4. ledger: pooled information (only if the budget allows)

Arms: `solo`, `indep-3`, `team-3`, 10 instances with 2 attempts.

- **Primary metric:** `reward`. **Primary effect:** `solo` minus `team-3`, the cost of split information.
- **Secondary:** `premature`, `selection_gap`, `seen_before_answer`.

## What these experiments cannot show

- The tasks are small and synthetic. A result says how this harness and this model behave on these rules.
- The per-seat budget makes a part of the headcount effect mechanical. `solo-x3` shows that part.
- 30 trials for each arm detect only large effects. The report gives the detectable effect for each design.

## Amendment 1 (04:18 AM ET, before any scored trial)

The first text gave wrong reference values for E1. It assumed that the best single agent opens 1 box in
each room. That is not the best play: a treasure room has 10 boxes and a seat has 9 opens, so a seat that
finds 1 treasure room does best when it stays there. The family builder found this by enumeration.

Corrected reference values (means over the 10 instances; `tasks/boxes/family.toml` has the exact numbers):

- **solo, best play:** about 0.38 (0.25 to 0.45 by instance).
- **3 identical agents:** the same as solo. They empty the same room, so the headcount effect is 0.
- **Tacit convention (no channel, each seat keeps to its own boxes):** about 0.89.
- **team-3 and solo-x3, best play:** 1.00.

The arms, the metrics, the effects and the number of trials do not change.

## Amendment 2 (04:54 AM ET, before any scored trial): pilot trials

Pilot trials choose the roster settings (thinking on or off, the turn cap) and test the plumbing.
They use instance `i01` of a family, with 1 attempt for each arm and each candidate setting.
They are not scored. The scored trials of `i01` run again with the final settings, like all other instances.
The report lists the pilot trials and their cost.

Since the first text, the task kit gives each seat its own internal network. A seat reaches the world
service and nothing else. So a condition without a channel has no network path between seats.

## Amendment 3 (05:13 AM ET, before any scored trial): final reference values and an instruction fix

- The family builder found a better rule for 3 agents without a channel. Seat 1 scans the rooms upward,
  seat 2 scans downward, and seat 3 opens the boxes that the 2 others cannot reach. This rule scores
  1.00 on all 10 instances. So the best play without a channel equals the best play with a channel.
  The measured difference between `team-3` and `indep-3` is then the practical value of the channel
  for this model, not a difference in what is possible.
- Exact reference values for the 10 instances (`tasks/boxes/family.toml`): solo best play 0.38
  (0.30 to 0.45). 3 identical agents 0.38. 3 agents that open boxes at random 0.385. Tacit rule 1.00.
  Team 1.00. 1 agent with 27 opens 1.00.
- The first instruction text had an example answer that named box 17 as empty. That is false for 2
  instances. The example now has placeholders. No model saw the first text in a scored trial.

## Amendment 4 (11:04 AM ET, before any scored trial): the roster and the runs

- **Roster for E1 and E2:** `experiments/rosters/t2-deepseek-flash-plain.json` (terminus-2, DeepSeek Flash,
  thinking off, at most 30 turns). Reason: the pilot cost $0.003 to $0.022 for each trial with thinking off
  and $0.010 to $0.148 with thinking on. With thinking off, 150 trials cost about $1.50.
- **E2 mixed arm:** `experiments/rosters/t2-mixed-3.json` (p1 DeepSeek Flash with thinking off, p2 Claude
  Haiku 5.5, p3 GPT-6 Luna). All 3 routes answered in the pilot.
- **E3 is dropped.** In the `commons` pilot every seat took the equal share, with and without a channel
  (reward 1.00 in both arms). The symmetric game has a ceiling. A family with conflicting needs
  (`scarce`) replaces it. Its pre-registration comes before its first scored trial.
- **Runs:** each arm is 1 Harbor job over 10 instances with 3 attempts. Up to 6 trials run at the same time.
  A trial with a provider error in every model seat is rerun, and the report counts the reruns.

## Amendment 5 (10:48 PM ET, 2026-11-19, before any scored trial): approval and cost guard

- Peter approved about $2 more for E1 and E2 (210 trials). The runs start in DeepSeek's off-peak window
  (04:00 UTC), when DeepSeek bills half of its peak price.
- A watchdog stops all runs when the estimated real cost reaches $1.85. The estimate counts DeepSeek seats
  at half of Harbor's recorded cost in off-peak hours and at the full recorded cost in peak hours, and
  OpenRouter seats at the recorded list price.
- Arm order, so the most important arms run first: solo-a, indep-3, team-3, solo-x3, solo-b, indep-3-salt,
  indep-3-mixed. An arm that the cap stops is reported as incomplete. Design, metrics and analysis do not change.
- The 2 trials of the stopped start at 11:04 AM ET are not part of any result.

## Amendment 6 (10:58 AM ET, 2026-11-21, before any E4 trial): E4 `ledger` runs

- Peter approved E4 ("run e4"). Balance before the runs: $4.69. Saturday, so DeepSeek bills off-peak.
- **Arms:** `solo`, `indep-3`, `team-3` from `muster/tasks/ledger/build/`, 10 instances x 2 attempts = 60
  scored trials. Roster `rosters/t2-deepseek-flash-plain.json` (the E1 roster). No change to the tasks.
- **Pilot:** instance `i01`, 1 attempt for each arm, not scored. It checks the plumbing and the cost per trial.
- **Cost guard:** the runs stop when the estimated real cost of E4 (pilot included) reaches $0.80.
- **Runs:** at most 2 Docker trials at the same time, 1 trial at a time for each arm.
- **Primary effect:** `solo` minus `team-3`, with a 95% interval from a paired bootstrap over the 10 instances
  (10,000 resamples, seed 0). "Resolved" means the interval excludes 0.
- **Test 1, does the team pool well?** Pass if the `team-3` mean is at least 2/3 of the `solo` mean.
- **Test 2, what loses the team points?** Among `team-3` trials with reward below 1, count the trials with
  `premature` = 1 (an answer before the messages of both other seats were read). "Premature answers explain the
  losses" if they are at least half of those trials. The others are split into: no answer, a wrong sum after
  all messages were read, and a better earlier attempt that was refused (`selection_gap` > 0).
- **Check:** `indep-3` must score about 0. It cannot pool, so any reward above 0.2 in a trial is a leak to study.

## Amendment 7 (2:50 PM ET, 2026-11-23, before any E5 trial): E5 `crowd`, the number of agents at a fixed total budget

An independent review checked a draft of this text against the E1 and F14 logs before any trial. Its changes
are in this text: the 2 primary tests, the k = 3 arms, the route anchor, the union control, the fixed turn cap,
the counting rule and the run order.

**Approval and limits**
- Peter approved up to $50 of credits for experiments on 2026-11-23. E5 caps: $12 of recorded model cost and
  $3 of Daytona sandbox time. No arm starts when the OpenRouter account holds less than $6 of credits (the
  account is shared with other lanes).
- Ledger: `muster-runs/e5/spend-ledger.jsonl`. Each arm is reserved before it starts and settled after it ends.
- Cost basis: $5.2 expected and $13.2 in the high case, from E1 and F14 tokens at the price of the roster's
  endpoint ($0.14 input, $0.0042 cached input, $0.42 output per million tokens).

**Question.** E1 and F13 to F17 used 3 seats. At an equal total number of world actions, how does a team's
loss against 1 agent change with the number of seats k, and does the F14 claim rule still remove it at k = 8?
No lane has varied k at a fixed total budget (library: `multi-agent/2026-11-23-executed-work-map-and-open-gaps.md`).
Design evidence: `multi-agent/2026-11-23-agent-count-scaling-designs-and-evidence.md`.

**Design**
- Family `muster/tasks/crowd`: the `boxes` world (60 boxes, 6 rooms of 10, 2 treasure rooms, reward = distinct
  flag boxes opened / 20) with the 10 layouts of `boxes` i01 to i10. Every arm has 24 opens in total. k seats
  get 24/k opens each: 24, 12, 8, 6, 3 at k = 1, 2, 3, 4, 8.
- Arms: `solo-24`; `indep-k` (no channel), `team-k` (free broadcast channel) and `claim-k` (`team-k` plus the
  F14 sentence "Before you open a box, post its number with POST /msg and read GET /msgs. Do not open a box
  that another agent posted first.") at k = 2, 3, 4, 8; `solo-12`, `solo-8`, `solo-6`, `solo-3` (1 seat with
  the per-seat budget); and the anchor `team-3-e1` (the frozen E1 `boxes/build/team-3` tasks, 27 opens).
- Game facts, computed before any trial (review script and the family's reference table): best play with the
  channel scores 1.0 at every k. Scouting 1 box in every room wastes exactly 4 opens and leaves no spare open.
  Scanning the rooms in order and inferring the last rooms leaves a mean of 1.1 spare opens on i01 to i10.
  Without a channel the best possible score is 1.0 at k = 2, 0.92 to 0.933 at k = 4 and 0.693 at k = 8.
  Identical agents score 0.50 / 0.233 / 0.087 and random choosers 0.36 / 0.344 / 0.337 at k = 2 / 4 / 8.
  So the channel can add nothing at k = 2, about 0.07 at k = 4 and 0.307 at k = 8.
- Trials: 10 instances x 2 attempts = 20 for each arm. Exceptions, for the union control: `solo-3` 8 attempts
  (80 trials), `solo-6` 4 (40), `solo-8` 3 (30). The counts are fixed. No early stop on results.
- Roster: `rosters/t2-or-flash-deepinfra.json` (terminus-2, DeepSeek V4.1 Flash through OpenRouter, pinned to
  the DeepInfra fp8 standard endpoint, thinking off). At most 30 turns for each seat in every arm. The cap does
  not change after the pilot.
- Backend: Daytona sandboxes (Docker inside the sandbox, 2 vCPU, 4 GiB) through the Olam fork at `6aeb609e`,
  for every E5 trial, the pilot and the anchor included. No arm changes its backend. At most 8 trials at once.
- Pilot (not scored): i01, 1 attempt each of `solo-24`, `claim-2`, `team-8`, `claim-8`, `indep-8`. It checks the
  plumbing on Daytona, that thinking is off, and the cost for each trial. No design value changes after it. A
  plumbing fault and its fix go in the log before the first scored trial.
- Run order: `solo-24`, `solo-3`, `solo-6`, `solo-8`, `solo-12`, `indep-2`, `indep-3`, `indep-4`, `indep-8`,
  `team-3-e1`, `team-2`, `team-8`, `claim-8`, `team-3`, `claim-3`, `team-4`, `claim-4`, `claim-2`. An arm that
  the cap or the time stops is reported as incomplete, and its contrasts as not available.

**Counting**
- The rule of Eval Lab's root `AGENTS.md` (newer than this file's first "Counting" rule) applies: a trial
  without a valid reward is unmeasured and stays out of the primary means. The report also gives each result
  with such trials counted as 0, as a check.
- A trial is rerun once when a seat made no model call because of a provider error. A rerun that fails again
  is unmeasured. The report counts the reruns and the seats with an error for each arm.

**Analysis** (`analysis/crowd_scaling.py`)
- Unit: the instance mean over its attempts (10 units for each arm). Interval: 95% bootstrap over instances
  (10,000 resamples, seed 0). Test: exact two-sided sign-flip over the paired instances.
- **Primary test 1 (the loss grows with k).** For each instance, the least-squares slope of the `team-k`
  instance mean on log2 k over k = 2, 3, 4, 8. Pass if the mean of the 10 slopes is below 0 and the sign-flip
  test over the 10 slopes gives p < 0.05. It needs at least 3 of the 4 `team-k` arms.
- **Primary test 2 (the claim rule holds at k = 8).** d = `solo-24` minus `claim-8`. Pass if the upper end of
  the interval of d is below 0.05 (1 flag for each trial). Fail if the lower end is above 0.05. Otherwise
  inconclusive. The same rule is secondary at k = 2, 3, 4.
- Secondary contrasts, without a correction for their number: `claim-k - team-k`; `team-k - indep-k` (shown
  next to what the channel can add at that k); `indep-k - solo-(24/k)` (headcount at an equal per-seat budget);
  `indep-k - union-k` (interference; `union-k` is the distinct flag boxes over k separate `solo-(24/k)` runs of
  1 instance); `solo-24 - team-k`; the slopes of `claim-k` and `indep-k`.
- Anchor: the `team-3-e1` mean against E1 `team-3` (85.7%) and F14c (83.8%), which ran on DeepSeek's own API
  and local Docker. It is a description. A difference above 10 points means that E5 levels must not be
  compared with E1 to F18 levels.
- Mechanism measures for each arm, from the world event log: the split 24 = new flags + new empty opens +
  repeats + unused opens; the F13 split of repeats; stranded opens; seats that used all 30 turns; messages for
  each trial and seat; `GET /msgs` calls; the share of opens after a post and a read; the first box by seat
  name; the first poster; the first-open collision share; `k_eff`; cost and wall time for each trial.

**Directional expectations (written before any trial)**
- `solo-24` near 98%. `team-k` falls as k grows: about 95% at k = 2 and about 70% at k = 8, because 8 seats
  cannot each take a room and the plan needs 2 rounds of messages.
- `indep-8` near the identical-agent floor (10% to 20%), far below its possible 69.3%.
- `claim-k` passes test 2's rule at k = 2, 3 and 4. At k = 8 I expect inconclusive or fail: 8 agents post
  claims before they read, and each seat has only 3 opens.
- Messages for each trial grow about linearly in k. The bytes that `GET /msgs` returns grow about with k squared.

**What E5 cannot show**
- 1 model, 1 harness, 1 synthetic task, 10 layouts, 20 trials for each arm. Pairs of arms near 100% have little
  test power, because most instance differences are 0.
- A fixed total budget makes the per-seat budget small at large k. At k = 8 the game differs in kind: more seats
  than rooms, no seat can empty a room, and the best silent play falls to 69.3%. E5 measures the loss at equal
  world actions, not at equal tokens or equal time. Tokens, cost and wall time are reported.
- E5 uses a prompt rule only. Server-side claim locks at 3 seats are the subject of another lane's campaign
  (`eval-lab-results/multiagent-campaign-2026-11-23/PREREGISTRATION.md`, written 2:05 PM ET the same day).
- Daytona adds latency to each seat command. All arms share it, but timing-dependent numbers (repeats before
  a post) may differ from local Docker runs.

## Amendment 7a (3:00 PM ET, 2026-11-23, before any scored E5 trial): run order, floor and task copy

- OBSERVED at 2:56 PM ET: the OpenRouter account fell from $23.19 to $13.95 in 31 minutes. E5 had spent $0.03
  of that; other lanes use the same account. At that rate the account is empty before E5 ends.
- **Run order.** The arms of the 2 primary tests go first: `solo-24`, `team-8`, `claim-8`, `team-2`, `team-4`,
  `team-3`. Then `indep-8`, `indep-4`, `indep-2`, `indep-3`, `claim-4`, `claim-2`, `claim-3`, `team-3-e1`,
  `solo-3`, `solo-6`, `solo-8`, `solo-12`. 2 lanes run at once with 4 trials each, so at most 8 trials at once.
- **Floor.** No arm starts when the account holds less than $2 (was $6). The $6 floor was meant to leave
  credits for other lanes; they now spend much faster than E5.
- **Task copy.** The family is still in the gate. The trials use a copy of `muster/tasks/crowd/build` at commit
  `bef4f0d08` of branch `fable/crowd` (`generate.py --check` passes there), stored in `muster-runs/e5/tasks/crowd`
  with its hash in `muster-runs/e5/tasks/crowd.pin.json`. If the landed build differs from that copy by 1 byte,
  the trials are void.
- Nothing else changes.

## Amendment 8 (3:33 PM ET, 2026-11-23, before any E6 trial): E6 `field`, agents added to a fixed large world

Written while E5 runs. E5 results seen so far (not final): `solo-24` 100%, `team-2` 84%, `team-4` 69%,
`team-8` 54%, `claim-8` near 75%, `indep-8` near 26%. No `field` trial with a model exists.

**Question.** Published sweeps over the number of agents (Bespoke MAC Bench, Replit) keep the task fixed and
give every agent its own budget, so the total budget grows with k. They report 1 agent, k silent agents and k
talking agents. They do not report 1 agent with the whole budget of the team. E6 runs that sweep on a muster
world and adds the missing control. Design evidence:
`research-context/multi-agent/2026-11-23-agent-count-scaling-designs-and-evidence.md`.

**Design**
- Family `muster/tasks/field` (branch `fable/field`, commit `dfbec6c05`): 240 boxes in 24 rooms of 10, 8
  treasure rooms, reward = distinct flag boxes opened / 80, 10 layouts. Every seat has 9 opens. k = 1, 2, 4, 8.
- Arms: `solo-9` (1 agent); `indep-k` (k silent agents); `team-k` (k agents with the channel); `scout-k`
  (`team-k` plus "When you learn what a room holds, post the room number and what it holds with POST /msg at
  once. Read GET /msgs before you choose a room."); `solo-18`, `solo-36`, `solo-72` (1 agent with the budget
  of 2, 4 and 8 agents). The family also has `claim-k`; E6 does not run it (cost; E5 covers the claim rule).
- Reference values (best scripted play, mean over the 10 layouts): `solo-9` 0.0875, `solo-18` 0.174, `solo-36`
  0.379, `solo-72` 0.754; `indep-2/4/8` 0.186 / 0.348 / 0.615; `team-2/4/8` 0.181 / 0.361 / 0.7475. The world
  never runs out of flags. Best play of 8 agents equals best play of 1 agent with 72 opens, so the task gives a
  team no advantage in opens. Identical agents that all scan from room 1 score 0.0875 at every k.
- Trials: 10 instances x 2 attempts for each arm; `solo-9` has 8 attempts (80 trials) for the union control.
- Roster, turn cap (30 for each seat), backend (Daytona, 2 vCPU, 4 GiB) and counting rules as in E5
  (Amendment 7). The cap does not change after the pilot. A seat that reaches the cap or the 600 s time limit
  with opens left is counted and reported for each arm.
- Pilot (not scored): i01, 1 trial each of `solo-72`, `team-8`, `scout-8`, `indep-8`.
- Run order: `solo-72`, `team-8`, `indep-8`, `scout-8`, `solo-9`, `solo-18`, `team-2`, `indep-2`, `solo-36`,
  `team-4`, `indep-4`, `scout-4`, `scout-2`. An arm that money or time stops is reported as incomplete.
- Money: E6 starts only when the E5 arms are done or stopped. No arm starts when the OpenRouter account holds
  less than $2. E5 and E6 together stay under the E5 cap of $12 of model cost. Expected E6 cost: about $3.

**Analysis** (`analysis/crowd_scaling.py --per-seat-budget 9 --boxes 240 --flags 80`)
- Unit, interval and test as in Amendment 7.
- **Primary 1 (does talk help at k = 8?):** `team-8 - indep-8`. Resolved if the 95% interval excludes 0.
- **Primary 2 (the missing control at k = 8):** `solo-72 - team-8`. "1 agent is better" if the interval is
  above 0. "The team is better" if it is below 0. Otherwise not resolved.
- **Primary 3 (does the team fall behind 1 agent as it grows?):** for each instance,
  (`solo-72 - team-8`) - (`solo-18 - team-2`). Pass if the mean is above 0 and the sign-flip p < 0.05.
- Secondary, without a correction: the same contrasts at k = 2 and 4 and with `scout-k`; `scout-k - team-k`;
  MAC Bench's parallelism gap `indep-k - solo-9` and communication gap `team-k - indep-k` at each k;
  `indep-k - union-k` (`union-k` = distinct flag boxes over k separate `solo-9` runs of 1 instance); flags for
  each open; each arm's reward divided by its reference value.
- Mechanism: the E5 measures, plus rooms opened by more than 1 seat, treasure rooms found, empty rooms probed
  more than once, and the share of seats whose first open is in room 1.

**Directional expectations (written before any trial)**
- `solo-9` near its reference (0.09). `solo-72` at 0.55 to 0.70: 1 agent scans in order and loses little.
- `indep-8` far below its reference of 0.615, near 0.15 to 0.25: most seats start in room 1.
- `team-8` between them, near 0.35 to 0.45. So I expect "1 agent is better" in primary 2, a positive primary
  1, and a pass in primary 3.
- `scout-8` above `team-8` by 5 to 10 points.

**What E6 cannot show**
- 1 model, 1 harness, 1 synthetic world, 20 trials for each arm.
- Opens are the only budget that E6 equalizes. A team of 8 has 8 times the turns and the wall time of 1 agent,
  and it uses more tokens. Tokens, cost and time are reported for each arm.
- The world rewards search only. It has no subtask that needs a specialist, so E6 says nothing about teams
  whose members differ.

## Amendment 8a (3:36 PM ET, 2026-11-23, before any E6 trial): the E6 pilot may run while E5 ends

- The 4 pilot trials of E6 (not scored, about $0.10) run while the last E5 arms run. The scored E6 arms still
  wait until every E5 arm is done or stopped. Reason: the shared account still falls, and a plumbing fault
  found now costs no E5 money.
- The E6 trials use a copy of `muster/tasks/field/build` taken with `git archive` at commit `dfbec6c05`
  (tracked files only), stored in `muster-runs/e6/tasks/field` with its hash in `field.pin.json`.
- Scripted checks on Daytona before the pilot, each equal to `metadata.oracle_reward`: `solo-72` 0.75,
  `team-8` 0.7125, `scout-8` 0.7125, `indep-8` 0.65, `solo-9` 0.1 (i01).

## Amendment 9 (3:50 PM ET, 2026-11-23): Docker Hub pull limit, reruns, and the order for the last credits

- OBSERVED: from 20:38Z, new Daytona trials failed before any seat started: `docker compose build` got
  `429 Too Many Requests` from Docker Hub for `python:3.13-slim`. 111 E5 trials failed that way (no model call,
  no reward): `claim-2` 20, `solo-8` 30, `claim-4` 16, `indep-2` 16, `indep-3` 13, `indep-4` 9, `solo-12` 7.
  (Corrected at 4:09 PM ET: the first text said 131 and 20:34Z. 131 was an addition error; 20:38Z is the start
  time of the first failed trial. The counts for each arm did not change.)
  The arms of both primary tests were complete before that (`solo-24`, `team-2/3/4/8`, `claim-8`, 20 trials
  each), and so was `indep-8`.
- **Fix.** A Daytona snapshot `muster-dind-mirror-v1`: the same `docker:28.3.3-dind` image, 2 vCPU, 4 GiB, plus
  a Docker registry mirror (`mirror.gcr.io`). Wrapper: `muster-runs/e5/harbor-daytona-mirror.sh`. Scripted
  trials through it equal their reference values, `muster regrade` matches, and the seat isolation probe
  passes. Task bytes and the Harbor checkout are unchanged. The base image now comes from the mirror, which can
  serve another patch build of `python:3.13-slim`. All later E5 and E6 trials use this wrapper.
- **Reruns.** A trial that failed with the pull limit made no model call. It is run again once, under the rule
  of Amendment 7. The analysis uses every trial with a reward. The report gives the rerun counts for each arm.
- **Money.** OBSERVED at 3:48 PM ET: the account holds $2.38 and still falls by about $0.20 each minute while
  E5 is stopped. The floor is now $1. The remaining order is: (1) E5 `solo-12`, `solo-8`, `solo-6`, `solo-3`,
  then the reruns of `indep-2`, `indep-3`, `indep-4`; (2) E6 in its registered order (Amendment 8); (3) E5
  `claim-4`, `claim-2`, `claim-3`, `team-3-e1`; these 4 arms are stopped for lack of money and run only if
  credits are added. Reason for (2) before (3): the claim rule at 3 seats is known (F14), and its test at 8
  seats is done; E6 has no result yet. Group (1) and group (2) run at the same time, because the account may
  be empty in minutes.
- A trial whose seats fail because the account is empty is unmeasured. It is rerun once if credits return.

## Amendment 10 (5:14 PM ET, 2026-11-23, before any E7 trial): E7 `crowdrules`, rule paragraphs that give each box 1 owner

Written after E5 (F19, F20) and its audit. No `crowdrules` trial with a model exists. E7 waits for credits: the
OpenRouter account is empty at this time. An independent review of this text, of the family and of the run
scripts came before this commit. It found 4 blockers and 13 smaller points; all are closed here (`LOG.md`).

**Question.** F20: 8 talking agents lose 45.8 points to 1 agent, mostly by repeats before a report of the box
could be read; their plans do not bind; the claim rule of `claim-8` (a message for each box) blocks boxes and
still loses 26.5 points. Does a written rule paragraph that gives each box exactly 1 owner, without a message
for each box, recover part of the loss? How well does this model obey each kind of rule?

**Design**
- Family `muster/tasks/crowdrules` (landed at `e356b421a`): the `crowd` world, the same 10 layouts, 24 opens in
  total, k = 8 and k = 4. The channel is on in every arm.
- Task copy: `muster-runs/e7/tasks/crowdrules`, taken with `git archive` at `e1deddb19` (the same `crowdrules`
  tree as `e356b421a`; hash in `crowdrules.pin.json`). If the copy differs from the landed build by 1 byte, the
  trials are void. Runner: worktree `fable-e5-run` at `e2a07d8fd`; Harbor checkout as in F19.
- Arms at each k. Each rule is 1 paragraph at the end of the `crowd` `team-k` text:
  - `team-k`: the `crowd` `team-k` seat texts, byte-identical (120 of 120 files). Only the task name, the seat
    tokens and the world class name differ. It is the anchor on the day of E7. All E7 contrasts use E7
    `team-k` only. If E7 `team-8` differs from E5 `team-8` (54.2%) by more than 10 points, the report says so.
  - `share-k`: "Each box belongs to exactly 1 agent. Box n belongs to the agent whose number is ((n - 1) mod K)
    + 1 [...] Open only your own boxes. Post each result [...] and read GET /msgs before each open [...]".
  - `chief-k`: p1 is the coordinator and posts a list that gives each agent its exact boxes; the others open
    only what p1's latest list gives them.
  - `zones-k`: a fixed plan by seat number. At k = 8: p1 to p6 each open the first box of their own room and
    then 1 fixed position in each treasure room; p7 and p8 wait for the reports and then open positions 8 to
    10 of the lower-numbered and of the higher-numbered treasure room.
  - Plus `solo-24` from `crowd` on the same day.
- Best score when every seat obeys the rule (mean of the 10 layouts; best play that does not know which 10 of
  the 15 layouts are in use): `team` 1.0, `chief` 1.0, `zones` 1.0, `share-8` 0.98, `share-4` 0.995. In
  layouts 1-5 and 2-6, 2 seats of `share-8` own 4 flag boxes each and have 3 opens. Over all 15 layouts, no
  fixed ownership by box reaches 1.0 at 8 seats.
- Scripted check (done, $0): the `oracle` policy scores its reference value on Daytona through the mirror
  snapshot in all 8 conditions, 1 trial each. `chief-8` needed a second trial: in the first, the image build
  in the sandbox failed (`rpc error: code = Unavailable`).
- 10 instances x 2 attempts for each arm. Roster and turn cap (30) as in Amendment 7. Backend: Daytona through
  the mirror snapshot (Amendment 9); the E7 runner cannot use another image.
- Pilot (not scored): i01, 1 trial each of `zones-8`, `share-8`, `chief-8`. Pilot trials go to
  `muster-runs/e7/pilot` and are in no score. The pilot can stop E7 for a plumbing fault. It cannot change the
  texts, the arms, the order or the analysis.
- Lanes: 2 lanes run at once with 4 trials each, so at most 8 trials at once. Lane A runs `solo-24`,
  `zones-8`, `chief-8`, `zones-4`, `chief-4`. Lane B runs `team-8`, `share-8`, `team-4`, `share-4`. Each lane
  starts an arm only when its last arm ends. Commands:
  `E5_FLOOR=1.00 ~/Developer/muster-runs/e7/e7_lane.sh 4 solo-24 zones-8 chief-8 zones-4 chief-4` and
  `E5_FLOOR=1.00 ~/Developer/muster-runs/e7/e7_lane.sh 4 team-8 share-8 team-4 share-4`.
  No other muster lane runs at the same time. An arm that money or time stops is reported as incomplete.
- Order against the waiting arms: E6 first (Amendment 8; it has begun), then E7, then the E5 arms of
  Amendment 9.
- Stop rule (the lesson of F21): when 3 E7 trials in a row (by end time, over both lanes) end without a
  reward, E7 stops. The running arms stop at once, and no new arm starts. A trial that writes no `result.json`
  is not counted.
- Counting and reruns as in Amendments 7 and 9. A trial without a reward is unmeasured, never 0. A trial that
  failed before a model call (sandbox, build, provider error, empty account) runs again once, in a new arm
  folder `<arm>.r1`, after the lanes end. The report gives the reruns for each arm.
- Money: expected model cost $3 to $6, plus about $1 of Daytona time. An arm starts only when the account holds
  at least $1 plus the reserve of the arm ($1.10 for 8 seats, $0.50 for 4 seats, $0.05 for `solo-24`). When the
  balance cannot be read, no arm starts. E5, E6 and E7 together stay under $12 of recorded model cost ($2.99
  before E7); the runner stops an arm at that cap.

**Analysis** (code landed at `d02fae0e9`, before the pilot: `analysis/crowd_scaling.py` with its RULES
block, and `analysis/crowd_rules.py` for obedience)
- Unit, interval and test as in Amendment 7. Instances pair by their number, so `crowd-i01` of `solo-24` pairs
  with `crowdrules-i01`.
- A contrast needs a reward in at least 1 trial of each of the 10 instances in both arms. Otherwise it is "not
  available", not "failed". Every contrast is also reported with unmeasured trials counted as 0.
- **3 primary contrasts at 8 seats:** `zones-8 - team-8`, `share-8 - team-8`, `chief-8 - team-8`, each with the
  exact two-sided sign-flip p over the 10 instance differences. Holm's rule runs on the 3 p values alone, with
  a fixed family of 3 even when a contrast is not available: 0.05 / 3 for the smallest p, then 0.05 / 2, then
  0.05, and it stops at the first failure. A rule "helps" if (a) Holm's rule rejects its test and (b) the lower
  end of its 95% interval is strictly above 0. It "hurts" if (a) holds and the upper end is strictly below 0.
  Otherwise it is not resolved.
- **Secondary, without a correction:** `solo-24 - rule-8` against the 5-point margin of Amendment 7 for each
  rule; the same 3 contrasts at 4 seats; each rule against the others; cost and wall time for each trial;
  seats at the 30-turn cap. `claim-8` of E5 is quoted for context only (another day).
- **Obedience** (from the event log; `analysis/crowd_rules.py`). The unit is 1 successful open by a seat. Every
  open counts, also a repeat of a box. Each open is "allowed" or "outside" under the rule of the arm:
  - `share`: allowed when (box - 1) mod k = seat number - 1.
  - `zones`: allowed when the box is the seat's scout box or is in the seat's plan for the true treasure rooms.
  - `chief`: a message by p1 is a list when it gives seats exact box numbers. The parser returns "not parsed"
    when it is not sure. A post that gives rooms only is a "room plan" and gives no boxes. Strict (the
    headline): the box is in the seat's boxes in p1's newest list before the open. Lenient: the box is in the
    newest list that names the seat. An open made while p1's newest list-like post is not parsed is
    unmeasured and leaves the ratio.
  - For each instance, obedience is the allowed opens divided by the classified opens over its trials with a
    reward. The arm value is the mean of the 10 instance values, with a bootstrap interval over instances.
  - Reference values for a team that never saw the rule (E5 `team-k` trials scored as if the rule applied):
    `share-8` 14%, `zones-8` 28%, `share-4` 26%, `zones-4` 27%, `chief-8` 9% and `chief-4` 9% of the
    classified opens.
  - On 40 hand-labelled plan posts of E5 the parser gave 20 exact lists, 17 "not parsed" and 0 wrong lists.
    If more than 20% of the opens of a `chief` arm are unmeasured, or if a hand check of 40 p1 posts of E7 by
    a worker that does not see the scores finds a wrong list, `chief` obedience is reported as "not
    measured", and only the counts are given.
  - Also: repeats, unused opens, messages, room plans, and for `chief-k` the time to p1's first list and the
    opens before it.
- An independent audit recomputes the primary numbers before the results are written.

**Directional expectations (written before any trial)**
- `zones-8` is the best rule, near 85%. Its errors are position counts (position 2 read as box 2).
- `share-8` near 70%: few repeats, but unused opens and opens in empty rooms, because the rule gives no
  scouting order.
- `chief-8` near 65%: p1 is slow or posts a list that changes, and the others wait or act without a list.
- All 3 rules beat `team-8` (near 54%). No rule passes the 5-point margin against `solo-24`.
- Obedience: `zones` above 90%, `chief` near 80% (strict), `share` near 85%.

**What E7 cannot show**
- These are prompt rules for 1 model and 1 harness on 1 task. A rule that wins is evidence about how this
  model reads a rule, not about teams in general.
- Each rule paragraph also tells the seats to post each result at once and to read GET /msgs. The `team-k` text
  has no such sentence. So `rule-8 - team-8` measures the whole paragraph, not ownership alone.
- The rules are compared with each other only in secondary contrasts without a correction. E7 does not show
  which rule is best.
- `zones` puts the solution of the coordination problem into the prompt. It shows what a team can reach when
  the plan is given, not that a team can find the plan.
- No rule here is enforced by the world. A claim that the world enforces and that expires is another lane's
  study.
- Power: in E5, the paired instance difference between 2 arms with 8 seats has an SD of 8.8 to 11.3 points.
  With 10 instances, a difference of about 10 to 13 points has 80% power at 0.05 / 3. The expected gain of
  `chief-8` (11 points) is near that size, so "not resolved" is a likely result for it.
- The 30-turn cap counts reads. A rule that makes seats wait (`chief`, `zones`) can end a seat with opens left.

## Amendment 10a (9:26 PM ET, 2026-11-23, before any scored E7 trial): a model call that does not return

Written after E6 ended and while the 3 pilot trials of E7 run. No scored E7 trial exists. The pilot results
are not known.

- OBSERVED in E6: the first 8 trials of `team-8` started together at 00:08:02Z. 14 of their 64 seats made 2
  to 4 steps, got no model response after 00:08:55Z to 00:09:00Z, and ended at the 600 s limit with
  `AgentTimeoutError`. 6 trials have such a seat. No other E6 arm and no E5 arm has one. This is a fault of the
  model route, not a behaviour of the team.
- Rule for E7: a seat is "stalled" when it ends with `AgentTimeoutError` and its trajectory has no step in the
  last 300 s of the agent phase (`muster-runs/stalled.py`, sha256 `3dffc70332e44489`). A terminus-2 command
  cannot be silent that long, so the seat was waiting for a model response.
- A trial with a stalled seat is unmeasured. It runs again once, in `<arm>.r1`, after the lanes end, like a
  trial that failed before a model call. The report gives the count for each arm. Every contrast is also
  reported with the stalled trials counted with their rewards.
- A seat that reaches the 600 s limit while it still makes steps is not stalled. Its trial counts.
- E6 does not change: its trials count as Amendment 8 says. The E6 report names the 6 trials and gives the
  primary tests without them as a check after the result.
- Nothing else changes.

## Amendment 11 (2:50 AM ET, 2026-11-24, before any scored E9 trial): E9 `horde`, the plain team from 8 to 48 agents at 96 opens, and 2 rules at 48 agents

Written after E5 to E7 and the E5 open arms (F19, F23, F24, F25). 3 pilot trials with a model exist and I know
their results (below). No scored `horde` trial exists. An independent review of the draft, of the task copy
and of the run scripts came before this commit. It found 2 blockers and 9 smaller points; all are closed here
(`LOG.md`).

**Question.** F19: at 24 opens in total, a talking team loses 15.5 points for each doubling from 2 to 8
agents. F24: at 8 agents a fixed plan or a coordinator removes most of that loss. Does the loss of the plain
team continue from 8 to 48 agents in a world that is 4 times as large? Does each rule still beat the plain
team when 48 agents share the board?

**Design**
- Family `muster/tasks/horde` (landed at `1994fccbf` after 2 independent reviews): 240 boxes in 24 rooms of
  10, 8 treasure rooms, 80 flag boxes, 96 opens in total. 1 agent has all 96 opens, or k agents have 96 / k
  each: 12 at k = 8, 6 at k = 16, 2 at k = 48. The message channel is on in every team arm; `GET /new` returns
  the messages that the agent's earlier `GET /new` calls did not return. Every team text has the same hint
  how to wait (`sleep 10` and a read as 1 command). The `team-8`, `team-16` and `team-48` texts differ only
  in their numbers. The best play that obeys the text scores 1.0 in every arm (`family.toml`, `[reference]`).
- Arms (6): `solo-96`; `team-8`, `team-16`, `team-48` (the plain team text); `zones-48` (a fixed plan by seat
  number: who scouts which room, and which positions of which treasure room each agent opens once the 8
  treasure rooms are known); `chief-48` (p1 is the coordinator and posts a scouting list and a treasure list;
  each other agent opens only the boxes under its name in p1's posts).
- Not run for money in E9: `team-32`, `zones-8`, `zones-16`, `zones-32`, `chief-8`, `chief-16`, `chief-32`.
  The family has them; the night's money does not.
- Instances `horde-i01` to `horde-i07`, 1 attempt each: 7 trials for each arm, 42 trials. The choice of 7
  layouts and 1 attempt is a money limit, fixed here before any scored trial.
- Task copy: `muster-runs/night-2026-11-24/e9/tasks/horde`, taken with `git archive` at `1994fccbf` (tree of
  the build `2c88a26454ea`). The lane uses the subset `e9/tasks-i01-i07/<arm>/horde-i0N`, copied from it
  (1,771 files, hash in `e9/horde.pin.json`). If a task differs from the landed build by 1 byte, the trials
  are void.
- Runner: worktree `fable-night-run` at `1994fccbf`; Harbor checkout as in F19 (`6aeb609e`). Roster and turn
  cap (30) as in Amendment 7: terminus-2, DeepSeek V4.1 Flash through OpenRouter, provider pin
  `deepinfra/fp8`, thinking off. Backend: Daytona with the snapshot `muster-dind-mirror-4c8g` (4 vCPU, 8 GiB)
  for every arm.
- Scripted check (done, $0 of model cost): the `oracle` policy scores 1.0 on Daytona in 8 of 8 trials on the
  2 vCPU mirror snapshot `muster-dind-mirror-v1`: `zones-48`, `chief-48` and `team-48` on `horde-i01`;
  `chief-8`, `solo-96`, `zones-16`, `team-32` and `chief-32` on `horde-i02`. No scripted check ran on the 4
  vCPU snapshot or for `team-8` and `team-16`. The 3 pilot trials are the only trials on the E9 backend.
- Pilot (done, not scored, `e9/pilot/`): 1 trial each on `horde-i01`, 1 trial at a time. `zones-48` 0.9875 (1
  repeat, 32 messages, $0.048, 168 s); `team-48` 0.4875 (36 repeats, 162 messages, $0.107, 231 s);
  `chief-48` 1.0 (0 repeats, 113 messages, $0.081, 293 s). 0 of 144 seats ended with an exception, reached
  the turn cap or ended with opens left; the most agent steps of a seat were 17. The pilot trials are in no
  score. They changed nothing in the texts or in the family. They did set which arms the money allows:
  `chief-48` is in E9 because its pilot cost was low. The scored arms run `horde-i01` again. The pilot had no
  `team-8`, `team-16` or `solo-96` trial: their cost, time and turn-cap behaviour are not checked.
- Lane: 1 lane, 2 trials at once, as 2 commands. Command 1 runs `team-8`, `team-48`, `zones-48`, `chief-48`
  in this order. Command 2 runs `team-16`, `solo-96`. The 2 arms with the largest weights in the slope
  (`team-8`, `team-48`) and the 3 arms with 48 agents run next to each other, so the primary tests are the
  least exposed to a change over the night. Command 1:
  `NIGHT_CAP=14.20 NIGHT_FLOOR=1.00 /Users/petermakhnatch/Developer/muster-runs/night-2026-11-24/night_lane.sh
  e9 2 1 1.00 /Users/petermakhnatch/Developer/muster-runs/night-2026-11-24/e9/scored
  /Users/petermakhnatch/Developer/muster-runs/night-2026-11-24/e9/tasks-i01-i07
  /Users/petermakhnatch/Developer/muster-runs/e5/rosters/t2-or-flash-deepinfra.json
  /Users/petermakhnatch/Developer/muster-runs/night-2026-11-24/harbor-daytona-mirror-4c8g.sh
  /Users/petermakhnatch/Developer/eval-lab/.worktrees/fable-night-run team-8 team-48 zones-48 chief-48`.
  Command 2 is the same with the arms `team-16 solo-96`. No other muster trial runs at the same time. No arm
  starts after 6:30 AM ET: if an arm would start later, I write the STOP file. An arm that money or time
  stops is reported as incomplete; its finished trials enter no contrast and are shown in the arm table with
  their count.
- Stop rule: when the newest 3 finished E9 trials have no reward, E9 stops (`night_arm.sh`; it leaves the
  file `night-2026-11-24/STOP`, which also holds E8 until I remove it).
- Counting and reruns as in Amendments 7, 9, 10 and 10a. A trial without a reward is unmeasured, never 0. A
  trial that failed before a model call is unmeasured. A trial with a stalled seat (Amendment 10a,
  `muster-runs/stalled.py`) is unmeasured: with 48 seats in a trial, 1 stalled seat voids the trial. An
  unmeasured trial runs again, at most 2 times (`<arm>.r1`, then `<arm>.r2`), after command 2 ends, if the
  money rule below allows it. Before a rerun, `python3 ~/Developer/muster-runs/stalled.py e9/scored/<arm>
  --move e9/stalled` (run from `~/Developer/muster-runs/night-2026-11-24`) moves each trial with a stalled
  seat out of the arm. Only the unmeasured instances go to `e9/tasks-rerun/<arm>.rN/`, copied from
  `e9/tasks-i01-i07/<arm>/`; the lane command then runs with that tasks root and the arm name `<arm>.rN`.
  The arm value uses `<arm>` and its reruns (linked into
  `e9/final/<arm>`); a rerun fills the unmeasured trial of its instance and never replaces a trial with a
  reward. The report gives the counts for each arm, and every contrast also with the stalled trials counted
  with their rewards (from `e9/stalled`).
- Money: Peter's limit for the night is $10: the model cost recorded since 10:45 PM ET on 2026-11-23 plus
  Daytona time at list price ($0.168 for an hour of the 2 vCPU sandbox, $0.336 for the 4 vCPU sandbox), as
  `night-2026-11-24/night_money.py` computes it. $3.81 is used at this time. Expected for E9: $2.3 of model
  cost and $0.9 of Daytona time. Worst case: when the model route stalls, every trial runs to its 600 s limit
  with little model cost, and the 42 trials cost $2.8 of Daytona time.
  - An arm starts only when the OpenRouter account holds at least $1 plus $1 of reserve; when the balance
    cannot be read, no arm starts.
  - The runner stops an arm when the recorded model cost of E5, E6, E7 and the night reaches $14.20. This cap
    counts model cost only.
  - A watcher (`night_watch.sh 1.40`) runs `night_money.py` every 60 s and writes the STOP file when less
    than $1.40 of the limit is left or when the number cannot be read. Trials in flight are not counted yet.
    Before each command and each rerun I check that the watcher runs (`pgrep -f night_watch.sh`) and that
    `night-2026-11-24/WATCH-END` does not exist; else the command does not start.
  - Command 2 and each rerun start only if `night_money.py` shows at least $2.60 left. E8 needs about $2.1;
    if less is left for it, E8 shrinks as Amendment 12 says.
  - A trial that a stop kills before its `result.json` exists has a cost that no file records.

**Analysis** (code landed at `2ff1a4366` on `main` before any scored trial:
`analysis/horde_scaling.py --instances i01-i07 --ks 8,16,48 --arm solo-96=<dir> --arm team-8=<dir> --arm
team-16=<dir> --arm team-48=<dir> --arm zones-48=<dir> --arm chief-48=<dir>`, with the arm folders of
`e9/final/`; the output starts with the definitions that it uses)
- Unit: 1 trial for each instance and arm, in points (reward x 100). The instance set is `horde-i01` to
  `horde-i07` in every test. The arm value is the mean of the 7 instance values with a 95% percentile
  bootstrap interval over the instances (`random.Random(0)`, 10,000 resamples of indices, ends at the sorted
  values 250 and 9,749). With 7 values this interval covers less than 95%; the sign-flip test is the gate.
- Contrasts pair by instance. The test is the exact two-sided sign-flip test over the 7 differences
  (`sign_flip` in `crowd_scaling.py`: the statistic is the sum; a sign pattern counts when its absolute sum
  is at least the observed one; zero differences stay in).
- A contrast needs a reward in each of the 7 instances in both arms. Otherwise it is "not available". Every
  contrast is also reported with unmeasured trials counted as 0.
- **3 primary tests, Holm's rule with a fixed family of 3** (0.05 / 3 for the smallest p, then 0.05 / 2, then
  0.05; it stops at the first failure; the family stays 3 when a test is not available; equal p values keep
  the order P1, P2, P3):
  - P1: the slope of the `team-k` score on log2 k over k = 8, 16, 48 (x = 3, 4, 5.585; least squares for each
    instance; the mean of the 7 slopes). "Falls" if Holm's rule rejects and the 95% interval is strictly below
    0; "rises" if it rejects and the interval is strictly above 0; else not resolved. The least-squares
    weights of the 3 sizes are -0.35, -0.06 and +0.41, so P1 is almost `team-48 - team-8` divided by 2.6, and
    `team-16` has little effect on it.
  - P2: `zones-48 - team-48`. P3: `chief-48 - team-48`. A rule "helps" if Holm's rule rejects its test and
    the lower end of the interval is strictly above 0; it "hurts" if Holm's rule rejects and the upper end is
    strictly below 0; else not resolved.
  - With 7 instances the smallest p is 2/128 = 0.0156 and the next is 4/128 = 0.031. So steps 1 and 2 of
    Holm's rule (0.0167 and 0.025) need all 7 differences to be non-zero and of 1 sign. Step 3 (0.05) accepts
    p up to 6/128 = 0.047. With 6 instances the smallest p is 0.031, so 1 missing instance stops a test at
    steps 1 and 2.
- **Secondary, without a correction:** `zones-48 - chief-48`; `solo-96 - arm` for each of the 5 team arms
  against the 5-point margin of Amendment 7; `team-16 - team-8` and `team-48 - team-16`. For each arm, as a
  mean for each trial: opens, new flags, new empty boxes, repeats (`duplicate_opens`), unused opens (96 minus
  opens), messages, board reads (events of kind `msgs` plus `new`), seats that end with opens left, seats at
  the 30-turn cap (agent steps at or above 30), seats with an exception, seats at the 600 s limit that are
  not stalled, the median model-call time and the median time between agent steps, cost and wall time.
- Obedience of `zones-48`: every open counts, a repeat too; an open is allowed when it is the seat's scout
  box or in the seat's plan for the true treasure rooms (the plan of `muster/tasks/horde/reference.py`).
- `chief-48`: the time to p1's first list (p1's first post that the parser reads as a list) and the opens
  before it; strict obedience with parser v2
  (`f1ed0404c`, `01f6f2b3e`; it was tuned on E7 posts after the E7 result). When more than 20% of the opens of
  the arm cannot be classed, obedience is "not measured" and only the counts are given.
- An independent audit recomputes the primary numbers from the event logs before the results are written.

**Directional expectations (written after the 3 pilot trials, before any scored trial)**
- `team-48` 45% (35 to 55), `team-16` 55% (45 to 65), `team-8` 65% (55 to 75). The slope is between -4 and
  -12 points for each doubling: less steep than in E5, because the world is 4 times as large and the first
  scouting round has work for 24 agents.
- `zones-48` above 90%. `chief-48` 85% (75 to 95): I expect 1 or 2 of the 7 trials with a late or wrong list.
  Both rules help against `team-48`, each by 30 points or more.
- `solo-96` 92% (85 to 100). In E6 the single agent with 72 opens scored 70.5% in the same world, 0.94 of
  the best scripted play (75.4%). With 96 opens the best play scores 100%; in `horde-i02` and `horde-i06` it
  leaves no spare open, and in the other 5 layouts it leaves 1 to 7.
- No team arm is above `solo-96`. `zones-48` and `chief-48` are within 15 points of it.

**What E9 cannot show**
- 7 layouts, 1 trial each, 1 model, 1 harness, 1 synthetic search task. The intervals are from 7 values.
- The total budget is fixed, so the opens for each agent fall as the agents grow (12, 6, 2). The slope is
  about that whole exchange, as in E5. It does not separate "more agents" from "fewer opens for each agent".
  The game also changes in kind: at 8 agents each agent can scout 3 rooms; at 48 agents there are 2 agents
  for each room, and 24 agents have no room to scout.
- The world, the budget and the `team-k` text differ from E5 and E7 (240 boxes, 96 opens, `GET /new` and a
  hint how to wait). Levels must not be compared across the experiments; only the direction can be.
- The slope has 3 team sizes (8, 16, 48) and is almost the difference of the 2 ends. It cannot show the shape
  between 16 and 48.
- P2 and P3 test whether a rule beats the plain team at 48 agents. They do not test whether it reaches the
  94% to 97% of F24.
- Each rule paragraph also tells the agents to post each result at once and to keep reading until a stated
  event. The `team-k` text has no such sentence. `rule-48 - team-48` measures the whole paragraph. `zones`
  puts the solution of the coordination problem into the prompt. `chief` does not, but its text tells p1 what
  kind of list to post.
- The load grows with k: a trial with 48 agents starts 48 model sessions together, 2 such trials run at
  once, and the pilot ran 1 at a time. 1 to 48 seat containers share 1 sandbox of 4 vCPU. Repeats depend on
  timing (F20), so a slower route at 48 agents can change the score. The median model-call time for each arm
  is reported. The stall of F23 came at 64 sessions that started together; such a stall shows as unmeasured
  trials, not as a result.
- The arms run over about 1.5 hours of 1 night. F25 does not exclude a shift of up to about 10 points over 8
  hours on this route. `team-16` and `solo-96` run last.
- The 30-turn cap binds the single agent with 96 opens more than any team agent. `solo-96` is the control
  "1 agent with the whole budget under the same turn cap", not "the best a single agent can do".
- `horde-i01` was seen in the pilot. The texts did not change after it.
