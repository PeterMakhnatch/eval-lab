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
