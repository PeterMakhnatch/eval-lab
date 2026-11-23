# Running log for Peter

Times are New York time (ET). Newest entry first. Each entry says what is done and what is observed.

## Next 3 items (written 11:02 AM ET, 2026-11-19)

1. Finish the 4 lanes that stopped at the Fable usage limit (end-of-play freeze, wrapped-secret redaction,
   tidegauge seat support, the `scarce` family). New agents on Opus 5.5 continue from the saved work.
2. Push the per-seat secret scrub fix and open draft pull requests inside the fork for the finished fixes.
3. Start the first scored experiment (E1, `boxes`, 5 arms x 10 instances x 3 attempts, DeepSeek Flash
   with thinking off, about $1.50) and report it with `tidegauge`.

## 2026-11-23

**2:50 PM. Plan for today, after research and an independent review ($0.008 spent so far).** Peter asked for
research, a plan, a double check and then work on multi-agent infrastructure, with up to $50 of credits.
- Research: 9 subagents wrote 6 evidence files and 28 source files in `research-context/multi-agent/`
  (commit `a42f6d2a`): executed-work map, agent-count scaling designs, coordination primitives, MAC Bench run and
  port plan, Olam upstream delta, new-threads intake. The synthesis with 12 corrections to older shelf text is
  `multi-agent/2026-11-23-evidence-wave-synthesis-and-muster-plan.md`.
- Another lane pre-registered at 2:05 PM ET (`eval-lab-results/multiagent-campaign-2026-11-23/`): server claim
  locks and unread guards at 3 seats (`boxes_v2`), `scarce` with real models, a `relay_v1` family and MAC Bench
  runs. It uses the muster kit. I do not repeat it. My part: the number of seats at a fixed total budget (E5).
- OBSERVED: the OpenRouter key shows $46.27 under its own limit, but the account holds $23.19 of credits. The
  account figure limits all lanes together. DeepSeek's own API has $2.54, which the other campaign reserved.
- Infrastructure results today (all $0 of model cost):
  - The kit runs unchanged on upstream `olamlabs/harbor-multiagent` `711ca65a`: 16 scripted trials on each
    runner over the 4 families give the same rewards, and `muster regrade` matches 16 of 16. The new runner is
    in `/tmp/fable-infra/HarborHeadCompat/`.
  - Muster trials run on Daytona with no code change (Docker inside the sandbox). 13 scripted trials, 0
    failures, rewards equal to the local reference, seat isolation holds (a probe policy: 21 checks pass for
    each seat), 8 trials at once in 69 s, about $0.001 of sandbox time for each trial. Spend: $0.015. The
    wrapper `muster-runs/e5/harbor-daytona.sh` adds `--env daytona` to `harbor run`.
  - Fork PRs against upstream head: #33 is superseded by upstream `a9c661c0` (closed). #34 is still needed.
    #13 is partly superseded. #35 needs a redesign on top of upstream's kill. Each PR has a comment with the
    details. The fork's `main` is still at `6aeb609e`; a sync changes the base of 83 open PRs of 4 lanes, so
    Peter decides it.
  - `analysis/crowd_scaling.py` (landed `e282e7e8e`) reproduces E1 and F14 from the event logs: rewards
    98.5 / 85.7 / 51.5 / 30.8, the F13 split 69% / 27% / 4%, and F14's +15.1 points (exact sign-flip p 8/1024).
  - Route pilot (not scored, `muster-runs/e5/route-pilot`): E1 `team-3` i01 and `solo-x3` i01 on the E5 roster
    through local Docker: reward 1.0 and 1.0, thinking off (0 of 28 agent steps have reasoning text), cost
    $0.0078 and $0.0009, 62 s and 31 s.
- E5 is pre-registered as Amendment 7. The review (`go with changes`) replaced both primary tests, added the
  k = 3 arms, the E1 route anchor and the union control, fixed the turn cap, and moved the cheap arms first.

## 2026-11-21

**12:12 PM. E4 `ledger` done (Peter: "run e4"). Real cost $0.72 (63 trials, pilot included). F18.**
Pre-registered as Amendment 6 (`a06875d2`, landed before the first trial). Runs 15:53 to 17:09 UTC, Saturday,
off-peak. Outputs: `muster-runs/e4/{pilot-*,solo,indep-3,team-3}`, tasks copied to `muster-runs/e4/tasks`.
- Result: `solo` 1.000, `team-3` 1.000, `indep-3` 0.000 (20 trials each). Test 1 passes, test 2 does not
  apply (no team losses), no leak in `indep-3`. Every answering seat had read both other reports first.
- Cost from tokens, at half of Harbor's recorded peak price: pilot $0.018, `solo` $0.016, `team-3` $0.083,
  `indep-3` $0.609 (Harbor recorded $1.449 in total). The balance went from $4.69 to $3.63 ($1.06); the rest of
  the drop is other agents' spend in the same window, which I cannot separate.
- Incident: my shell job hit its 60-minute limit and killed the run script and its cost watchdog, but not
  `harbor run` (it continued as an orphan, PID 5550). I found it within 1 minute and restarted a watchdog with
  the same $0.80 cap, and the job finished the 20 `indep-3` trials. No trial was lost or counted twice.
  Next time: start long arms with no shell deadline, and kill the whole process group on a timeout.
- Next for this question: `ledger` is at the team's ceiling. A harder version (more items, a message limit
  or a deadline) can find where pooling fails. It needs a new family version and its own pre-registration.

**4:17 AM. $0 follow-up to F14: how agents follow the report-first rule.** Question and test, fixed before I
looked at the numbers: the rule is "followed" if at least 90% of opens come after a post and a read by the
same seat since its previous open; claims "work" if at most 10% of opens hit a box that another seat
claimed first. New columns in `analysis/races.py`: `post_read_share`, `post_then_read_share`.

| Arm | Post and read before the open | Post, then read, then open | Repeats |
|---|---|---|---|
| `team-3-reportfirst` | 78% | 69% | 0.9 |
| `team-3-rerun` | 28% | 6% | 5.7 |
| `team-3-roles` | 27% | 2% | 2.8 |
| `team-3-shared` | 22% | 3% | 3.9 |
| `team-3-shared-single` | 17% | 0% | 5.55 |

- OBSERVED: "followed" fails (78%, below 90%). "Claims work" passes: 6.5% of report-first opens hit a box that
  another seat had claimed first ("will open box N", "opening box N", "claim box N"); 27 of those 47 opens came
  after the actor had read the claim. In a sample of report-first opens without a fresh post, the box was in an
  earlier batch claim of the same seat ("will open 14,15,16,17,18 now"; "claims room4 boxes 36,37,38,39,40").
- INFERRED: agents keep the purpose of the rule, not its letter. They claim a batch, then open it. That is
  enough to cut repeats from 5.7 to 0.9. Box numbers in free text are a weak signal (room numbers look like
  box numbers), so only the structural measures above are in the table.
- E4 (`ledger`) stays unstarted. muster now runs from Eval Lab, and a paid `muster run` is a billable Eval Lab
  command, which needs Peter's OK.

**4:12 AM. muster now lives in Eval Lab (`main` at `2ad853fc1`, $0). This log moved with it.** 4 commits,
landed with `make land` after `make gate` passed:
1. `8bc316398` imports the muster repository at `eed7a2f` unchanged, so all of it stays in Eval Lab history.
2. `902216776` makes `muster/` a top-level package (`uv run muster ...`). The task families sit in
   `muster/tasks/` as frozen task supply, byte-identical: the gate runs each family's `generate.py --check`.
   The studies (this log, findings, results page, pre-registration, rosters, analysis) are in this folder.
3. `2b8ab7ef8` puts the analysis scripts in Eval Lab style. The F13 script now reads the frozen build when a
   recorded task path is gone, and gives the recorded numbers again (69% of `team-3` repeats "not sent").
4. `2ad853fc1` applies the Eval Lab rules to the code (no comments or docstrings, functions at most 70 lines,
   asserts) and replaces the ~6,000 lines of unit tests with a seeded simulation (`muster/sim.py`: 48 seeds,
   12 task conditions, 26,232 invariant checks in 5 s). Checks: CLI output byte-identical on 144 cases over
   20 real job folders; the old suite passes on the new code (722 passed); the simulation catches 2 planted
   bugs.
- The simulation found 2 real bugs, both fixed and replayed in every gate: `muster regrade` exited 0 when no
  trial could be compared (for example, "30 no-task" on E1, whose old task paths are gone); it now exits 3.
  An event log with invalid UTF-8 crashed `regrade` and `events`; it is now reported as a log problem.
- `muster regrade muster-runs/e1run/team-3 --task-root muster/tasks/boxes/build/team-3` gives 30 of 30 match:
  the frozen build reproduces the E1 rewards.
- The GitHub repository `PeterMakhnatch/muster` stays as it is, with a note that points here. You decide
  whether to archive it.

**4:10 AM. Next real-agent experiment, proposed, not started (needs about $0.40 of DeepSeek budget).**
`boxes` favors 1 agent, because 1 agent can hold the whole state. The pre-registered E4 (`ledger`, a hidden-
profile task) tests the opposite case: each seat holds 1 of 3 warehouse ledgers, every item is in at least 2
ledgers, and the first accepted answer is final. Arms `solo` (1 seat holds all 3 ledgers), `indep-3`,
`team-3`, 10 instances x 2 attempts, the E1 roster. Primary effect: `solo` minus `team-3`, the cost of split
information. Pass/fail test to fix before the run: the team pools well if `team-3` scores at least 2/3 of
`solo` and the difference is resolved, and `premature` (an answer before every other seat's message was
read) explains most team losses. I start it when the budget allows and Astra's claim is clear.

**3:55 AM. tidegauge now lives in Eval Lab (`main` at `ee91f81ff`, $0).** Its GitHub repository was gone, and
`~/Developer/tidegauge` was the only copy. Peter's rules put new systems in top-level Eval Lab packages.
3 commits, landed with `make land` after `make gate` passed:
1. `563112515` imports the tidegauge tree at `8ced196` unchanged (source, 203 tests, docs, scripts), so all of
   it stays in Eval Lab history.
2. `db181202a` makes `tidegauge/` a top-level package (`uv run tidegauge ...`), registers it in AGENTS.md,
   and adds it to the gate (ty, compileall, `python -m tidegauge.sim check`).
3. `ee91f81ff` applies the Eval Lab rules: no comments or docstrings, functions at most 70 lines, asserts.
   The unit tests become a seeded simulation (`tidegauge/sim.py`: 25 seeds, 819 scenarios, 8,578 invariant
   checks in 11 s, with file faults, null calibration and power). Checks: the CLI output is byte-identical on
   83 commands over real muster jobs, and the old suite passes on the new code (317 passed, 3 skipped).
   The simulation catches 2 planted bugs.
- The simulation found 1 real bug: an arm with uneven attempts and no valid reward made `audit` and `score`
  exit 3 ("no trials"). Fixed; its seed replays in every gate.
- `~/Developer/tidegauge` is unchanged; its untracked `data/` (2.7 GB of leaderboard downloads) stays there.
  You can remove the folder when you want; I did not.

**3:50 AM. The race numbers of F15 to F17 are now reproducible ($0).** `experiments/analysis/races.py JOB_DIR ...`
prints, per arm, the reward, new flags, new empty boxes, repeats (seen or not seen by the actor before it
opened), the busiest seat's opens, messages, opens per batch, and the share of "obvious next box" opens.
On the 7 arms of F14 to F17 it gives every number in this log and in `docs/FINDINGS.md`.

**3:40 AM. F17 result (real agents, paid run): fixed roles close most of the gap, but not as well as a claim.**
20 trials of `team-3-roles`, 3:06 to 3:34 AM ET (Saturday, off-peak).

| Arm | Score | New flags | New empty | Repeats | Obvious next box |
|---|---|---|---|---|---|
| `team-3-rerun` | 83.8% | 16.75 | 3.95 | 5.7 | 69% |
| `team-3-roles` | 94.5% | 18.9 | 4.65 | 2.8 | 35% |
| `team-3-reportfirst` (F14) | 98.8% | 19.77 | 3.33 | 0.9 | 54% |

- OBSERVED: +10.8 pts over `team-3-rerun`; the tasks question is resolved (+1.9 to +19.6, sign-flip p 0.040),
  the rerun question is not (p 0.059). `team-3-reportfirst` is +4.3 pts above `team-3-roles`, resolved on the
  tasks question (sign-flip p 0.011). 91% of the opens follow the seat's order. Repeats fall by half. Empty
  opens rise from 3.95 to 4.65: each seat scouts rooms on its own, with different boxes. 20 of 20 rewards
  replay exactly. No key in the outputs.
- Pass/fail test from 3:08 AM: 94.5% is below 94.6%, so the test fails, by 0.1 pts. Roles do not work as
  well as a claim; F14 is still resolved above them.
- INFERRED: a role breaks the tie inside a room. It does not split the scouting of rooms, so 2 seats still
  scout the same room with different boxes, and some still meet in a treasure room.
- **Cost of this paid run: $0.271** (from tokens at the off-peak price; Harbor records $0.542). Balance $6.11
  at 3:34 AM. My paid spend today: $1.32; in total under the $10 budget: $2.06. This is my last paid run tonight.

**3:08 AM. F17 design, written before any trial (my own choice; the queue is empty).** I said at 3:00 AM that
I would make no more paid runs tonight. I change that for 1 small run: it answers the open question of F16,
costs about $0.25, and the balance is $6.52. Question: does a fixed role per seat break the tie between alike
agents as well as a claim does, without any message rule? Arm `team-3-roles`: the plain `team-3` tasks (own
opens only, channel) plus 1 line per seat. p1: "Your order inside every room: open its boxes from the lowest
number upward (box 1 of the room first)." p2: the same, "from the highest number downward (box 10 of the room
first)". p3: "from the middle outward (boxes 5, 6, 4, 7, 3, 8, 2, 9, 1, 10 of the room)". The roles say
nothing about which rooms to scout. Tasks from `muster-runs/f17/derive.py`. 10 instances x 2 attempts.
Pass/fail test, fixed now: roles work as well as a claim if the arm scores at least 94.6% and `tidegauge
compare` against `team-3-rerun` resolves the gain as positive (tasks question, both tests p < 0.05).
Secondary: repeats per trial (F14 report-first: 0.9; `team-3-rerun`: 5.7). Budget: stop at $0.30 real.

**3:03 AM. $0 follow-up to F16: the repeats are the "obvious next box".** For each open I ask whether the box
is the lowest unopened box of its room, as far as the actor knew (its own opens, and `team_opens` where shown).

| Arm | Opens that are the obvious next box | Repeats that are the obvious next box |
|---|---|---|
| `team-3-rerun` | 69% | 75% (86 of 114) |
| `team-3-shared` | 84% | 51% (40 of 78) |
| `team-3-shared-single` | 87% | 93% (103 of 111) |
| `team-3-reportfirst` | 54% | 52% (14 of 27) |

INFERRED: with 1 open at a time, agents almost always take the lowest free box of the treasure room, so 2
agents in the same room take the same box. The report-first agents spread out (54% obvious). Candidate next
experiment (needs about $0.25, not started): a seat rule without messages, for example p1 takes boxes from
the low end, p2 from the high end, p3 from the middle. It tests whether a fixed role breaks the tie as well as
a claim does. E2 showed that a random number per seat does not.

**3:00 AM. F16 result (real agents, paid run): 1 open at a time does not stop repeats. Alike agents choose the
same box at the same time.** 20 trials of `team-3-shared-single`, 2:32 to 2:59 AM ET (Saturday, off-peak).

| Arm | Score | New flags | Repeats | Opens per batch | Repeats where the earlier open was already shown |
|---|---|---|---|---|---|
| `team-3-shared` (F15) | 88.5% | 17.7 | 3.9 | 2.4 | 35 of 78 |
| `team-3-shared-single` | 90.3% | 18.05 | 5.55 | 1.02 | 7 of 111 |
| `team-3-reportfirst` (F14) | 98.8% | 19.77 | 0.9 | 1.2 | |

- OBSERVED: agents follow the rule: 1.02 opens per batch, 1% of opens in batches of 3 or more. The score is
  +1.7 pts over `team-3-shared` (not resolved, sign-flip p 0.55) and +6.5 pts over `team-3-rerun` (not
  resolved, p 0.18). 104 of the 111 repeats happen before the earlier open reaches the actor; the other 7
  come 0.3 to 1.0 s after it. 20 of 20 rewards replay exactly. No key in the outputs.
- Pass/fail test from 2:31 AM: the rule is followed and the arm stays below 94.6%. So batching is not the main
  cause. INFERRED: alike agents choose the same box at nearly the same time. A claim before the open (F14)
  removes most of these repeats; seeing the opens after they happen does not.
- **Cost of this paid run: $0.254** (from tokens at the off-peak price; Harbor records $0.509). Balance $6.57
  at 3:00 AM. My paid spend today: $0.26 + $0.53 + $0.25 = $1.05; in total under the $10 budget: $1.79.
  I make no more paid runs tonight, so that Astra keeps her first claim.

**2:40 AM. F15 result (real agents, paid run): neither budget nor fragmentation alone explains the gap.**
60 trials, 1:23 to 2:32 AM ET (Saturday, off-peak).

| Arm | Score | New flags | New empty | Repeats | Max opens of 1 seat | Opens per batch | Real cost |
|---|---|---|---|---|---|---|---|
| `solo-x3-rerun` | 100.0% | 20.0 | 3.75 | 0 | 23.8 | 12.5 | $0.037 |
| `team-3-rerun` (F14c) | 83.8% | 16.75 | 3.95 | 5.7 | 9 | 2.5 | $0.256 |
| `team-3-shared` | 88.5% | 17.7 | 3.7 | 3.9 | 9 | 2.4 | $0.253 |
| `team-3-pool` | 93.2% | 18.65 | 3.65 | 4.65 | 15.6 | 2.9 | $0.244 |

- OBSERVED: G = +16.2 pts (`solo-x3-rerun` against `team-3-rerun`, resolved, sign-flip p 0.008).
  `team-3-shared`: +4.7 pts, not resolved (sign-flip p 0.44), 29% of G. `team-3-pool`: +9.5 pts, 59% of G;
  the tasks question is resolved (+2.1 to +16.9, sign-flip p 0.032), the rerun question is not (p 0.082).
  60 of 60 rewards replay exactly. No key in the outputs.
- Pass/fail test from 1:25 AM: neither arm reaches 94.6% (2/3 of G). So neither factor alone explains the gap.
- OBSERVED: in `team-3-shared`, all 78 repeats are races: 43 happen before the earlier open reaches the actor,
  35 come 0 to 1.5 s after the answer that shows the box, in the same batch of commands. In `team-3-pool`
  repeats stay (4.65), but the seat that finds a treasure room keeps opening there (the busiest seat makes
  15.6 opens on average), so fewer flags stay out of reach.
- INFERRED: the gap has 2 parts. Pooling the budget recovers flags that a 9-open seat cannot reach; it does
  not stop repeats. The shared view removes few repeats, because agents act in batches before they read.
  F16 tests the batching part.
- **Cost of this paid run: $0.534** (from tokens at the off-peak price; Harbor records $1.068 at the peak
  price). Balance $7.90 at 1:21 AM, $6.91 at 2:33 AM; other lanes ran at the same time. My paid spend today:
  $0.26 (F14c) + $0.53 (F15) = $0.79; in total under the $10 budget: $1.53.

**2:31 AM. F16 design, written before any F16 trial (my own choice; the queue is empty).** Observed at $0 in
F15 (before the `team-3-pool` result): in `team-3-shared` every repeat is a race. 43 of 78 repeats happen
before the earlier open reaches the actor, and the other 35 come 0 to 1.5 s after the answer that shows the
box, in the same batch of commands. Agents send 2.4 opens per batch (a batch = opens less than 2 s apart),
`solo-x3` sends 12.5, `team-3-reportfirst` 1.2. Question: does the team reach the single agent when each
seat opens 1 box at a time and reads the shared list between opens, without any message protocol?
Arm `team-3-shared-single`: the `team-3-shared` tasks plus 1 instruction line: "Open only 1 box per command.
Read its answer, with `team_opens`, before you choose the next box. Do not open a box that `team_opens`
already lists." Tasks from `muster-runs/f16/derive.py`. 10 instances x 2 attempts, same roster.
Pass/fail test, fixed now: batching explains the rest of the gap if the arm scores at least 94.6%
(`team-3-rerun` 83.8% plus 2/3 of G = 16.2 pts) and `tidegauge compare` against `team-3-shared` resolves the gain as
positive. Check that the rule was followed: opens per batch at most 1.5. If the rule is followed and the arm
stays below 94.6%, the cause is simultaneous choices by alike agents, which only claims (F14) remove.
Budget: stop at $0.45 real.

**1:25 AM. F15 design, written before any trial (Peter's queue item 2, real agents).** Question: why does 1
agent with the budget of 3 (`solo-x3`, 98.5%) beat the talking team (`team-3`, 83.8% to 85.7%)? Both have 27
opens in total. 2 things differ: (a) budget: the agent holds all 27 opens, the team has 9 per seat; (b) context
fragmentation: the agent sees all its opens in 1 context, each team seat sees only its own opens and the
messages. 2 new arms change 1 factor each, from `team-3` (same instances, tokens, channel, roster):

- `team-3-pool`: the 3 seats share 1 pool of 27 opens. Fragmentation stays.
- `team-3-shared`: 9 opens per seat, but the world shows every open of every seat: each open answer has
  `team_opens`, and `GET /opened` returns it. The budget split stays.
- Controls: `team-3-rerun` (F14c, 11-21, 83.8%) and a new `solo-x3-rerun` today.

Each arm: 10 instances x 2 attempts = 20 trials, DeepSeek Flash, thinking off, 1 trial at a time.
Tasks come from `muster-runs/f15/derive.py` (copies of `build/team-3`, a world subclass, 1 or 2 changed
instruction sentences); a $0 world check passes. Gap G = `solo-x3-rerun` minus `team-3-rerun`.
Pass/fail test, fixed now:

- Fragmentation explains the gap if `team-3-shared` closes at least 2/3 of G and `tidegauge compare`
  against `team-3-rerun` resolves it as positive (tasks question, t-test and sign-flip both p < 0.05).
- Budget explains the gap if `team-3-pool` meets the same 2 conditions.
- If neither arm meets them, neither factor alone explains the gap.

Prediction from F13 (the team leaves only 0.6 opens unused, and repeats cost the flags): `team-3-shared`
closes the gap, `team-3-pool` does not. Budget: DeepSeek direct, Saturday off-peak, caps on real cost:
`solo-x3-rerun` $0.10, `team-3-shared` $0.45, `team-3-pool` $0.40; total under $1. Balance at 1:21 AM: $7.90.

**1:20 AM. F14c result (real agents, paid run): F14 holds. The plain team did not improve over time.**
20 trials of `team-3-rerun`, 12:54 to 1:19 AM ET (Saturday, off-peak).

| Arm | Score | Repeats per trial | Repeats before the first opener posts |
|---|---|---|---|
| E1 `team-3` (11-19) | 85.7% | 4.9 | 69% |
| `team-3-rerun` (11-21) | 83.8% | 5.7 | 62% (3.55 of 5.7) |
| `team-3-reportfirst` (11-20) | 98.8% | 0.9 | 48% (0.43 of 0.9) |

- OBSERVED: `tidegauge compare` E1 `team-3` against `team-3-rerun`: -1.9 pts, not resolved (p 0.66).
  `team-3-rerun` against `team-3-reportfirst`: +15.1 pts, resolved for both questions (rerun +2.3 to +27.9,
  tasks +6.6 to +23.6, sign-flip p 0.0075); higher on 8 of 10 instances. 20 of 20 rewards replay exactly.
  No key in the outputs.
- Pass/fail test from 1:00 AM: `team-3-rerun` 83.8% is at most 92%, and the compare is resolved as positive.
  So F14 holds: the gain comes from the report-first protocol, not from drift after 11-19.
- Fork PR #34: the rerun of the failed CI job passes. All checks on #13, #33, #34 and #35 are green.
- **Cost of this paid run: $0.256** (from tokens at the off-peak price; Harbor records $0.512 at the peak price).
  The DeepSeek balance went from $8.34 to $7.94; other lanes ran at the same time. My total paid spend under
  the $10 budget: $0.74 + $0.26 = $1.00. I make no more paid runs without a new budget.

**12:58 AM. Fable now runs as an OMP chat on Opus 5.5.** Checked at $0: the 4 fork draft pull requests have no
new review comment since 11-20, 2:44 PM. #13, #33 and #35 are green. #34 had 1 failed CI test,
`test_superserve.py::test_cancellation_kills_the_command` (`ProcessLookupError`). #34 does not touch that
file, so I started a rerun of the failed jobs. `docs/TOOLS-COMPARISON.md` exists (written 11-20).
DeepSeek balance at 12:52 AM: $8.34.

**1:00 AM. F14c design, written before any trial (time control for F14, real agents).** Question: is the F14
gain (+13.2 pts for `team-3-reportfirst` over E1 `team-3`) real, or did the model or the provider change
after 11-19? Arm `team-3-rerun`: the committed E1 `team-3` tasks without change, run again on 11-21, after F14,
with the same roster `t2-deepseek-flash-plain`, 10 instances x 2 attempts = 20 trials, 1 trial at a time.
Primary metric: reward. Pass/fail test, fixed now: F14 holds if `team-3-rerun` scores 92% or less and
`tidegauge compare` against `team-3-reportfirst` resolves the gain as positive. F14 is drift if `team-3-rerun`
scores 95% or more, or the compare is not resolved. A result between 92% and 95% is undecided. Secondary:
repeats per trial from
`experiments/analysis/budget_identity.py` (E1 `team-3`: 4.9). Budget: DeepSeek direct only, Saturday off-peak,
stop at $0.26 real, so my total paid spend stays under $1.

## 2026-11-20

**3:58 PM. F14 result (real agents, paid run): when agents report before they act, the team matches the
budget-matched single agent.** 30 trials of `team-3-reportfirst`, DeepSeek Flash, 3:08 to 3:57 PM ET (off-peak).

| Arm | Score | Repeats per trial | Messages per trial | Real cost per trial |
|---|---|---|---|---|
| E1 `team-3` (11-19) | 85.7% | 4.9 | 14.0 | $0.012 |
| `team-3-reportfirst` | 98.8% | 0.9 | 34.1 | $0.025 |
| E1 `solo-x3`, 1 agent with 27 opens | 98.5% | 0 | 0 | $0.002 |

- OBSERVED: `tidegauge compare`: +13.2 pts, 95% rerun interval +9.0 to +17.3, resolved for both questions
  (sign-flip p 0.002); higher on all 10 instances. 2 or more agents start with the same box in 10% of the trials
  (47% in `team-3`). 30 of 30 rewards replay exactly. No key in the outputs.
- INFERRED: this confirms the F13 mechanism: the gap was acting before reporting, not a lack of ability. The
  line is an explicit protocol, so it measures the value of that protocol, not what agents find alone. The
  protocol doubles the messages and the cost. Time is not controlled (E1 ran on 11-19).
- **Cost of this paid run: about $0.74** (estimated from tokens at the off-peak price; the DeepSeek balance went
  from $11.49 to $10.76). My total paid spend under the $10 budget: $0.74.

**3:06 PM. F14 design, written before any trial (the F13 follow-up, real agents).** Question: does the team
lose to the budget-matched single agent because agents act before they report (F13)? Arm `team-3-reportfirst`:
the E1 `team-3` tasks (10 instances, 3 attempts, the same roster `t2-deepseek-flash-plain`) with 1 added
instruction line: "Before you open a box, post its number with POST /msg and read GET /msgs. Do not open a box
that another agent posted first." Primary metric: reward, compared with E1 `team-3` (85.7%) by
`tidegauge compare`. Secondary: repeats from `experiments/analysis/budget_identity.py`. Expectation: fewer
repeats and a higher reward. Caveat: E1 `team-3` ran on 11-19, so time is not controlled. Budget: DeepSeek
direct only, off-peak, stop at $0.80 real; balance before the run $11.49.

**2:48 PM. Handoff: I stop here because the weekly Claude quota is almost used.** Peter's new rules apply from now:
work only in Eval Lab or the fork, no cross-review, prefer real LLM agents in seats, no paid calls without a
budget. The 3 assigned items are done (F13, review answers on #13 #33 #34 #35, `docs/TOOLS-COMPARISON.md`).
The review of #20 was posted before the new rules; I do no more cross-reviews.
Next item that fits the rules (needs a budget or a free model route): the F13 follow-up, a `team-3` variant
whose instruction asks each agent to post an open before it acts (30 real-agent trials, about $0.35 at
DeepSeek off-peak). Open decision for Peter: where tidegauge lives, because its GitHub repository is gone.

**2:47 PM. Review of Opus pull request #20 posted (merge, no blocker).** A probe with a fake secret shows that the
branch removes the secret in 3 cases where `main` leaves it or changes other bytes (a character across byte 8192,
invalid bytes after the sample, CRLF lines). It works together with #34. Next: stop here, because the weekly
Claude quota is almost used.

**2:46 PM. Done at $0:** F13 in `docs/FINDINGS.md` (the talking team loses to the budget-matched agent through
repeated opens; 69% happen before the first opener reports). Fork PR #34: the reviewer's new P1 (quadratic
scan) is fixed, 120 KB of escapes from 28.3 s to 0.08 s. Review residuals on #13 and #35 answered as stated
limits; #33 has "merge as is". One-page tool comparison: `docs/TOOLS-COMPARISON.md`.
Observed: `gh` cannot find the GitHub repositories of tidegauge, harbor-sextant and harbor-control-gate.
Next: an adversarial review of 1 Opus pull request, if the quota allows.

## 2026-11-19

**11:46 PM. E1 and E2 are done: 210 trials, real cost about $1.22 of the approved $2**
(Harbor records $2.25 at DeepSeek's peak price; the runs used the off-peak hours, which cost half). Results in
`docs/FINDINGS.md` F11 and F12. Talking adds 34.2 pts and is resolved, but 1 agent with the same total budget
scores 12.8 pts above the team. All 210 rewards replay exactly.

**12:43 PM. tidegauge: a tasks verdict now needs the t-test and the exact sign-flip test** (PeterMakhnatch/tidegauge#3,
merged, CI green). On the flotilla c1 data 4 verdicts change to "not resolved". On the Terminal-Bench 2.0
leaderboard 17 of 2,775 `compare` verdicts change, all near p 0.04 to 0.05. `gate` now says "undecided"
instead of exiting with an error when no task has 2 or more trials.

**12:09 PM. The fork's own CI passes on all 4 draft pull requests** (#13: 11 checks, #33, #34, #35: 12 checks each,
with Windows and the Docker and Podman runtime tests). Next: `decompose` should not call a small 0/1 effect
resolved from the t-test alone; an agent adds the exact sign-flip test to the tasks verdict.

**12:02 PM. tidegauge `--task-key` merged** (PeterMakhnatch/tidegauge#2, CI green on Python 3.12 to 3.14).
All lanes of this session are finished. Open work: the 4 draft pull requests in the fork wait for their CI
and for your rewrite of the upstream text; E1 and E2 wait for a budget decision.

**12:01 PM. First measured result with the new tools, at $0 (F10):** `tidegauge decompose` on 276 trials that the
flotilla lane already ran reproduces that lane's coordination table exactly. The exact test resolves the
coordination gain for 1 of 4 models (DeepSeek, +91.7 pts, p 0.001). The t-test also calls 3 smaller gains
resolved; the exact test does not. No talking team is resolved as different from 1 agent with all the facts.
tidegauge gets `--task-key` for this (PeterMakhnatch/tidegauge#2, CI running).

**11:55 AM. Pull request #13 answers its review:** 5 new commits fix 4 of the reviewer's points (roster checked
against the tasks before any trial, server keeps each participant's env, every failing seat is named, the
default agent no longer masks a seat). 2 points are stated as limits. A reply with the table is on the pull
request. `muster matrix --policy-env WAIT_SEC=5` gives scripted seats a shorter wait. The ledger lane measured 29 s
instead of about 140 s for 1 roster with a waiting oracle. I checked only the job config (dry run).

**11:53 AM. Fourth draft pull request in the fork:** seat processes freeze when play ends, before grading
(https://github.com/PeterMakhnatch/harbor-multiagent/pull/35). A seat that acts 12 s after play: rewards 1, 1, 1 on
`main`, 0, 0, 0 on the branch. A seat that cannot be frozen makes the reward invalid. 85 seat tests pass.

**11:44 AM. Third draft pull request in the fork:** wrapped API keys stay out of trial output
(https://github.com/PeterMakhnatch/harbor-multiagent/pull/34). mini-swe-agent now gets the key from a private
file, and the scrub also matches a key that a line break splits. 154 touched tests pass.
**F9 (no model, $0):** in `scarce`, a seat that understates its need always scores, and an honest seat pays.

**11:25 AM. tidegauge reads multi-seat trials and has `decompose`** (merged, PeterMakhnatch/tidegauge#1,
CI green on Python 3.12 to 3.14). `decompose` splits a team's gain into headcount and coordination, each
with both error bars. On the 1-trial pilot it prints the numbers and says why it cannot decide.
Next, at $0: `decompose` on the real trials of the flotilla lane, which already paid for them.

**11:18 AM. The `scarce` family is done** (finished by an Opus agent, verified here): 157 tests pass,
4 real Docker runs give the expected reward with the expected seat paying, and `muster regrade` matches 10 of 10
trials. A task can now declare the findings that its design produces (`[metadata] expected_findings`).
`scarce` declares `tested-seat-not-needed`, because only 2 of 3 seats can score. muster CI is green.

**11:08 AM. Paid runs stopped: the shared $20 ceiling is reached.** The 4 agents share 1 ceiling. The other
lanes count about $19.3 (OpenRouter and Modal). This lane spent about $0.45 to $0.62 (mostly DeepSeek).
E1 stopped after 2 trials ($0.01). E1 and E2 need about $2 more: that is your decision.
The 5 leftover containers and 2 networks of the 2 stopped trials are removed (11:12 AM).

**11:04 AM. Second draft pull request in the fork:** every seat's model key is now scrubbed
(https://github.com/PeterMakhnatch/harbor-multiagent/pull/33). The 2 new tests fail on `main` (3 failed)
and pass on the branch (5 passed). **E1 and E2 started:** 7 arms, 210 trials, about $2, pre-registered.

**11:01 AM.** The Fable usage limit stopped 5 subagents at about 6:50 AM. Their work is saved in their
worktrees. The session continues on Opus 5.5.

**06:49 AM. Control matrices on the 3 families (no model, $0).** Every all-oracle trial gives the exact
expected reward. In `boxes` and `commons`, the `spoof` policy (impersonation, a guessed token, a second take,
a call to close the world) gives exactly the rewards of `nop` in every roster: no exploit found.

**06:00 AM. Control matrices on 4 seat tasks of the other agents (no model, $0).**

| Task (branch) | Observed |
|---|---|
| `hello-seats` with world-issued tokens (`flotilla/hello-seats-identity`) | Fixed. Impersonation now gives 0. On the upstream example it gives the full reward. |
| `kit-check` (`pm/multiagent-control-fixtures`) | 7 of 8 control rosters end with `RewardFileNotFoundError` or a timeout. No reward is written when a seat does nothing. |
| `cramped-room-parity` (`pm/overcooked-parity`) | 3 of 4 control rosters end with `RewardFileNotFoundError`. |
| `dispatch-handoff` (`experiments/seatlab-handoff`) | 3 of 4 control rosters end with `RewardFileNotFoundError`. |

Inferred: a verifier that writes no reward when a seat fails makes the trial invalid instead of 0. Harbor's
job mean then leaves the trial out, and the team score goes up. The fix is to write reward 0.

**05:30 AM. Pilot with real models (not scored, $0.38).** `boxes` instance 1, 1 attempt for each arm.

| Arm | Thinking on | Thinking off |
|---|---|---|
| 1 agent | 0.45 ($0.010) | 0.25 ($0.003) |
| 3 agents, no channel | 0.95 ($0.148) | 0.75 ($0.012) |
| 3 agents, channel | 0.85 ($0.105) | 0.90 ($0.022) |

Observed: without a channel, the 3 agents with thinking on each invented a split of the rooms. With thinking
off, 2 of 3 agents opened the same first boxes. With a channel, agents sent 15 to 20 messages, and 13 of 25
actions happened while a message of another agent was posted and not yet read.
Claude Haiku 5.5 alone: 0.45 ($0.002). GPT-6 Luna alone: 0.30 ($0.002).

**05:25 AM. Kit: each seat has its own internal network.** Before this, seats shared 1 network, and a seat
could read a file that another seat served. So no "no channel" condition was silent. Now a seat reaches the
world and nothing else. Verified on real Docker. Since 11:00 AM the networks are IPv6 only: Docker's IPv4
pool ran out at about 30 networks, which stopped 1 trial of a control matrix.

**05:20 AM. Draft pull request in the fork:** participants are now checked before a trial starts
(https://github.com/PeterMakhnatch/harbor-multiagent/pull/13). On `main` a missing model key fails after
47 s of setup and play. On the branch it fails in about 2 s.

**04:30 AM. A model key was written to a local log in plain text** (smoke test, OpenRouter key, about 8 minutes,
then redacted in place). 2 causes in the fork: the scrub set reads the key of the first seat only, and the
scrub does not match a key that a harness prints across 2 lines. Both have fix branches. A scan of 2,865 run
files and 21,675 repository files found no other copy. You decide if the OpenRouter key needs rotation.
