# MAC Bench matched-budget comparison: design

Status: design only. No trial in this file has been run. Every command is marked "NOT RUN".
Every flag that does not exist today is marked "MISSING".

Labels: OBSERVED means I read the file or the output on 2026-11-24. INFERRED means a reasoned step from
OBSERVED facts. "Unknown" means no stored file gives the value. A missing value is never 0.

Short names for the source folders:

- B = `~/Developer/bespoke-macbench-lab` (lab; `upstream/` is Bespoke at `dbec43f`).
- H = `B/upstream/harness/src/multi_agent_harness` (the harness).
- C = `~/Developer/eval-lab-results/multiagent-campaign-2026-11-23` (stored pilot and ledger).
- P = `C/bespoke/study4-holdout/jobs` (the Holdout pilot trials).
- L = `~/Developer/research-context/multi-agent` (the library).

The pilot denominator: 8 clean scored trials (`study4-{solo,silent,team,guard}-{1,2}`), n = 2 for each arm.
Excluded: 2 canaries (`study4-canary-team`, `study4-canary-haiku`) and 3 Wave-3 aborts (`study4-*-3`,
OpenRouter HTTP 402 at start). Source: `C/spend-ledger.jsonl`, `L/benchmarks/2026-11-23-bespoke-holdout-daytona-and-zero-fork-guards.md`.

## Part 2.1: how the harness enforces money, time and marks

### Model budget (`--budget-usd`)

- OBSERVED. `run_hardened.py run --budget-usd B` passes `--ek budget_usd=B` to Harbor (`B/run_hardened.py:278-288`).
  `TeamEnvironment` stores it (`H/harbor/environment.py:207,246`) and starts the hub proxy with
  `PROXY_BUDGET_USD=B` (`environment.py:544`).
- OBSERVED. The cap is for the whole team. `Proxy.over_budget()` compares the team total (bucket `"*"`) with
  `budget_usd` (`H/proxy/server.py:182-189`).
- OBSERVED. A per-agent cap also exists: `--ek agent_budget_usd=X` sets `PROXY_AGENT_BUDGET_USD`
  (`environment.py:127,208,247,553-554`; `server.py:186-188`). Neither lab launcher exposes it as a named flag.
  `run_hardened.py run` passes it through after `--`. `run_study4_trial.py` has no pass-through.
- OBSERVED. What happens at the cap: the check runs before the request is forwarded (`server.py:298-301`).
  The proxy answers HTTP 429 `budget_exceeded` with `x-should-retry: false` (`server.py:73-91`). It writes a
  row with `status: 429, reason: "budget"` to `proxy-usage.jsonl` (`server.py:299`).
- OBSERVED. Overshoot: requests already in flight still finish and are billed (`server.py:21-22`). The
  hardened environment limits in-flight requests to 2 for each agent (`B/run_hardened.py:17,100-103`;
  `server.py:194-201` counts in-flight requests per agent).
- OBSERVED. Where spend is recorded: each call is 1 row in `<trial>/proxy-usage.jsonl` (`ts`, `agent`,
  `model`, `status`, `tokens.{input,output,cache_read,cache_write,cache_write_1h}`, `usd`, `latency_ms`).
  The totals are in `<trial>/agent/proxy-usage.json` (`budget_usd`, `agent_budget_usd`, `total`, `agents`).
  `run_hardened.py settle()` copies `total.usd` into the `settle` row of `C/spend-ledger.jsonl`
  as `model_usd`, beside Daytona `compute_usd` (`run_hardened.py:233-275`).
- OBSERVED. The orchestration also reads the budget. Holdout `keepalive.py` polls `/admin/usage` every 30 s.
  It blocks normal wakes and retries once a poll sees the team total at 97% of `budget_usd` or more
  (`BUDGET_STOP = 0.97`, `B/upstream/tasks/holdout/task/environment/hub/keepalive.py:56,190-195,233-241`).
  The audit of 2026-11-24 found 1 exception: `_respawns` runs before the block check and can still wake an
  agent (`keepalive.py:265-273,320-335`). It reads only the team total, not a per-agent cap.
- OBSERVED. In the pilot, no trial reached its cap. The caps were $0.75 (2 of 2 solo trials) and $1.25 (6 of 6
  k = 2 trials) for the team, from `config.json` `environment.kwargs.budget_usd`. The highest spend was $0.1975
  (`study4-guard-1`), which is 15.8% of its cap. All 1,624 proxy rows in the 8 trials have status 200 (1,622 rows)
  or 402 (2 rows). There are 0 rows with `reason: "budget"`. The cap has never been exercised in a stored trial.

### Price: proxy USD against the real bill

- OBSERVED. The proxy computes USD from its own table, not from the provider bill. For `claude-haiku-5-5` the
  table is input $0.10, output $0.50, cache write $0.125, cache read $0.01 per million tokens
  (`H/proxy/pricing.json:19`). I recomputed 1 pilot row by hand and it matched
  (`P/study4-team-1/.../proxy-usage.jsonl` row 1: $0.00056675).
- OBSERVED. The real charge came from OpenRouter (`model_upstream=https://openrouter.ai/api` in every pilot
  `config.json`). The Study 4 ledger rows have no OpenRouter receipt; `model_usd` in the `settle` rows is the
  proxy total. Other waves in the same ledger record an OpenRouter figure that is about 1.25 times the reported
  figure (for example `e-b3-scarce-sonnet-5.5`: `actual_usd` 4.0839, `reported_usd` 3.2671). Those other waves
  do not use the hub proxy.
- INFERRED. The arms use the same price schedule and cap. They may differ in realized proxy USD.
  The real bill for each arm is unknown. Report proxy USD and billed OpenRouter USD separately.

### Time limit

- OBSERVED. `--timeout-sec T` becomes `--ak timeout_sec=T` (`run_hardened.py:286-287`). Each CLI session runs
  under `timeout -k <grace> T` (`H/harbor/agent.py:924`); exit code 124 is this timeout. The supervisor ends the
  run when the orchestration gives an end reason or at `T + 3 × grace` (`agent.py:1189-1207`).
- OBSERVED. Holdout `keepalive.py` freezes the world 30 s before the end (`FREEZE_LEFT_SEC = 30`,
  `keepalive.py:54,269-270`). It also freezes early ("run_end") when nobody is running and either the budget
  stop holds or every agent has ended its session (`keepalive.py:271-273`). Then `end_reason` returns
  `all_finished` (`keepalive.py:205-216`). With `FINISH_IS_FINAL = True` (`keepalive.py:46`) an agent that
  ends its session cleanly is not woken again.
- OBSERVED. Pilot freeze times from `result.json` `agent_result.metadata.keepalive.timeline` (t from the
  orchestration start): 7 of 8 trials froze at `time_up` between 1770.5 s and 1774.7 s. `study4-solo-1`
  froze at `run_end` at 1629.5 s, 5.3 s after its only agent ended with exit 0 at 1624.2 s. So 1 of 8 trials
  had a shorter horizon than the others.
- OBSERVED. Holdout task timeouts: agent 15,000 s, verifier 300 s (`B/upstream/tasks/holdout/task/task.toml:22,27`).
  The game runs 900 real seconds for each game day (`tests/hidden/holdout/config.py:14`).

### Grading marks

- OBSERVED. `--ak grade_at=15m,30m` grades at agent time 900 s and 1800 s (`H/harbor/marks.py:1-30`). A mark
  within 60 s of the agent timeout is not graded on its own; the final verifier is that mark
  (`marks.py:25-28,48`). In the pilot, `m30` is therefore the final verifier, not a second independent score.
- OBSERVED. A Holdout mark reads the game's own grade at that moment. The grade is the best progress so far
  and never goes down (`B/upstream/tasks/holdout/README.md:31-38`).
- OBSERVED. The retained scores between marks are discrete. `verifier/details.json.score_times` saves
  `[wall_s, score, game_t]` when the score rises by about 0.005. `verifier/replay.jsonl` saves `wall` and
  `score` to 0.1 s and 4 decimal places, usually every 5 game minutes (about 3.1 real seconds).
  The `holdout_start.ts` event in `board-events.jsonl` aligns the game and proxy clocks.
  The score between saved frames is unknown unless the surrounding evidence fixes it at the stored precision.

## Part 2.2: can 1 agent play, and do in-task resources scale with k?

### Holdout

- OBSERVED. 1 agent can play: "A team of K agents (1, 2, 4 or 8), each one survivor. One agent is K=1"
  (`holdout/README.md:64`). The same map, seed (`holdout-v1`) and instruction apply at every k
  (`README.md:23-24`). 1 seat is 1 survivor; the bearer token picks the seat (`environment/hub/game_plugin.py:5-6`).
- OBSERVED. The world does not scale with k: 20 starting walkers, hordes from 60 to 210, 14 strangers, and the
  same ladder of 12 tiers (`config.py:70-98,120-133`). The labor of each tier is in survivor-minutes
  (`config.py:117-133`). Heavy tiers go at 0.25 times the rate with 1 builder (`solo_heavy_rate`, `config.py:65`).
  The shared stock starts at 10 cans and 4 wood (`engine.py:76`). Each survivor adds 2 cans, 1 spear,
  60 radio charge and 25 kg of carrying capacity (`engine.py:271-272`, `config.py:27,33,89`).
  The totals from survivors alone are 2/4/8/16 cans and 1/2/4/8 spears at k = 1/2/4/8.
- INFERRED. In Holdout the number of agents is also the number of bodies in the world. A 1-agent run has 1/k of
  the labor and cannot do 2-person heavy work at full rate. So `solo-B` is a different game, not a fair control
  for `team-k`. The fair control for `team-k` at equal money is `silent-k`, which has the same k survivors.
- OBSERVED. In the pilot the 2 solo trials spent $0.0753 and $0.0593 by execution end.
  The 6 k = 2 trials spent $0.1216 to $0.1975 (`C/spend-ledger.jsonl`, `settle` rows).
  INFERRED: a 30-minute solo may not use a cap chosen from the k = 2 spend.

### Power Outage (library's first candidate)

- OBSERVED. 16 fixed control areas at every team size. Seat `aNN` goes to the agent at position
  (NN − 1) mod N, so 1 agent holds all 16 (`power-outage/README.md:15-17`;
  `task/environment/hub/operations_plugin.py:160-167`).
- OBSERVED. One drill pool for the whole team and the whole run: `BLACKSTART_PRACTICE_POOL`, default 40, with no
  term in k (`operations_plugin.py:18-21,85,229-246`). The library notes the board process gets no custom
  environment, so a different pool needs an edit of the default (`L/benchmarks/2026-11-23-mac-bench-local-run-and-port-plan.md:226`).
- OBSERVED. Bespoke's reference budget is $2,500 for the team at every k (`power-outage/README.md:68,75`).
  Agent sandbox 4 CPU / 8,192 MB; hub 8 CPU / 16 GB for grading; agent timeout 14,400 s; verifier 18,900 s
  (`task.toml:21,34,43-44`).
- INFERRED. A 1-agent run is the same game with the whole view and the whole pool. It is a fair `solo-B`
  control. Silent private data: each area's data is readable only by its holder (`README.md:18-20`), so a
  solo agent has more information than any team seat. That is part of the game, not a flaw of the control.

### Third task (my choice): Seed to Profit

- Why: the company and research pool do not scale with k. The task ships a board-off policy.
  The pilot reused it for Holdout (`seed-to-profit/task/environment/hub/no_messages_policy.py`).
- OBSERVED. One company, one bank account, one research budget and one `main` at every k
  (`seed-to-profit/README.md:18-19,23`). The research pool is $250,000 across all seats
  (`environment/app/docs/REFERENCE.md:538`, `environment/hub/market_plugin.py:144`).
  Seed money is $2,000,000 at every k (`README.md:3`). The reference model cap is $500 per team (`README.md:76,84`).
- OBSERVED. It is real time: a 1-hour launch phase, then 1 simulated month about every 4.7 minutes
  (`README.md:7-8`; `market_plugin.py:168-172`). A 30-minute run never leaves the launch phase.
- INFERRED. A 1-agent run is the same game (fair `solo-B`). But a valid run needs about 4 hours and an
  8 CPU / 16 GB hub, so it costs much more than Holdout. Second choice: 0 A.D. (`legion`), with one pool of 30
  practice matches for the team (`legion/README.md:12-16`), but its image is about 15 GB.
- Mindustry also holds resources fixed through k = 8. It divides 8 units among the agents.
  It has no shipped silent policy (`mindustry/README.md:14-17`; L port plan, section 2).

## Part 2.3: the arms at equal total model money

Definitions. B is the team's total proxy USD. k is the number of agents.

- `solo-B`: 1 agent, cap B.
- `silent-k`: k agents, no messages, Git sealed, cap B shared (primary) or B/k each (variant).
- `team-k`: k agents with the task's message channel, same cap as `silent-k`.

Primary cap: shared `budget_usd = B`, `agent_budget_usd` unset. Reasons: the keepalive stop rule reads only the
team total (OBSERVED, above); Bespoke's own budgets are team budgets (OBSERVED, task READMEs). Variant: per-seat
`agent_budget_usd = B/k` with `budget_usd = B`, reported as a sensitivity check.

| Task | Arm | Exists now? | Launcher and change | What stops at the cap |
|---|---|---|---|---|
| Holdout | `solo-B` | Yes | `run_study4_trial.py --arm team --n-agents 1` (the pilot solo trials used `build/holdout-team`, `board_policy.py`, no `seal_git`; OBSERVED in `config.json`; the launch line is INFERRED) | Proxy 429 for the 1 agent; then keepalive freezes at `run_end` |
| Holdout | `silent-k` shared | Yes | `run_study4_trial.py --arm silent --n-agents k --budget-usd B` (adds `comms: False` build, `no_messages_policy.py`, `seal_git=true`) | Proxy 429 for all; keepalive stops wakes at 97% of B |
| Holdout | `silent-k` per seat | MISSING in `run_study4_trial.py` | Smallest change: add `--agent-budget-usd` to `run_study4_trial.py` `main()` and append `--ek agent_budget_usd=<x>` in `build_command()`. Works today through `run_hardened.py run ... -- --ek agent_budget_usd=<x>` | Proxy 429 for each seat at B/k; the team total may stay under 97%, so keepalive may resume the seat up to 3 times (`MAX_FAILURES`) |
| Holdout | `team-k` | Yes | `run_study4_trial.py --arm team --n-agents k --budget-usd B` | Same as `silent-k` |
| Power Outage | `solo-B`, `team-k` | Yes, through `run_hardened.py run` with Bespoke's flags after `--` | No lab launcher. Short runs need task edits (instruction "4 hours", `BLACKSTART_VERIFY_SCENARIOS`, pool) as listed in the port plan step 3; a `prepare-power-outage` step is MISSING | Proxy 429; this keepalive wakes on timers and tells agents the budget (`power-outage/.../keepalive.py:142-157`) |
| Power Outage | `silent-k` | MISSING | Board-off alone still leaves Git. The verifier needs controllers from `main`, so `seal_git=true` alone removes the submission channel. Add a private controller submission plugin in `task/environment/hub/` and an orchestrator export step. Partition writes by the existing area assignment. Assemble `main` on the hub at each mark in area order. No seat sees peer submissions during play. | Same model cap. The submitted controllers still use the task's runtime hotline under the same rules. |
| Seed to Profit | `solo-B`, `team-k` | Yes, through `run_hardened.py run` | No lab launcher; needs a 4-hour cap | Proxy 429; keepalive wakes on timers |
| Seed to Profit | `silent-k` | MISSING | The board-off flag exists, but `main` still carries messages. The live company reads it throughout the trial. A private submission and merge launcher needs a fixed integration rule at each month, and must give both arms the same rule. This changes the released execution contract. Do not use branch-only sealing as the strict silent arm. | Same model cap. The live company continues on the software installed for that month. |

The following changes are needed before any E2 Holdout trial:

1. MISSING, ledger path. `run_hardened.py` appends to the hard-coded `C/spend-ledger.jsonl`
   (`run_hardened.py:15-16`). A new study would write into another lane's result folder. Smallest change: a
   `--ledger PATH` option on `run_hardened.py run` that `append_ledger()` uses.
2. MISSING, web tools. Holdout's managed settings deny `Agent`, `Task`, `Workflow`, `Skill`, `RemoteTrigger`
   and `Bash(claude *)`, but not `WebSearch` or `WebFetch` (`holdout/task/environment/claude-managed.json`,
   copied into the image by `Dockerfile:12`). The harness only adds `--disallowedTools Agent,Task`
   (`agent.py:942`). Smallest change: in `run_hardened.py prepare_holdout()`, add `WebSearch` and `WebFetch` to
   `permissions.deny` of the copied `environment/claude-managed.json`.

3. MISSING, fixed horizon and equal Git policy. In `B/orchestrations/holdout_keepalive_fixed_horizon.py`, copy
   the pinned Holdout orchestration and remove its `run_end` freeze branch (`keepalive.py:271-273`).
   Retain the 30 s freeze margin. The 1,800 s CLI limit thus has a target scoring cut at agent time 1,770 s.
   `end_reason` waits for the freeze (`keepalive.py:205-216`). Verify actual cut times.
   Add `--orchestration PATH` to `run_study4_trial.py` and forward that path instead of the fixed path at line 52.
   Add `--seal-git` to pass `seal_git=true` for **all** arms. The game does not need Git to score.
   This leaves the in-game message setting as the only policy difference between the two k-seat arms.

A fourth change makes post-cap world activity measurable:

4. MISSING, game call log. `details.json.calls` counts every game request and does not distinguish mutations.
   Add `B/plugins/game_call_log.py`. Use the REST and MCP hooks in `plugins/game_action_guard.py` as a model.
   Log the seat, timestamp, endpoint, and operation outcomes on both paths. Do not record message text.
   Have `run_study4_trial.py --game-call-log` add the plugin to the list for every arm.

## Part 2.4: estimands, measures, marks, pairing and missing scores

### Three estimands; do not mix them

- E1, descriptive replay ($0, stored pilot). Score at the first moment the uncapped trial's cumulative proxy USD
  reached U. The agents did not know about U and acted as if money were free. Nothing happens "after the cap".
  Align the completion timestamp in `proxy-usage.jsonl` with `holdout_start.ts`. Use the replay frames
  and saved grade states around the crossing. Report bounds, not interpolated scores.
- E2, cap with a fixed horizon (primary). Apply the same proxy cap B in every arm. The same CLI limit
  T = 1,800 s gives the same target score freeze at agent time 1,770 s. Use the final verifier grade.
  Between the cap and the deadline:
  - the proxy refuses every model call (429, `reason: "budget"`);
  - what Claude Code does on that 429 is unknown (open check O1); keepalive wakes nobody (budget stop) and, with
    `FINISH_IS_FINAL`, wakes nobody who ended cleanly;
  - the game API still accepts calls from any process that holds the seat token (`game_plugin.py:54-67`);
  - queued timed orders (up to 8 for each survivor, `queue_max`, `config.py:32`) keep running, helpers act,
    zombies and hordes act;
  - the grade never goes down, so the world after the cap can only add score;
  - background workers: the harness stops a CLI by its pid, its children and its process group
    (`agent.py:1097-1113`). A detached process could survive. In the 14 pilot seat logs I found 0 `nohup`,
    `setsid` or `disown` strings; `run_in_background` appears only inside Claude Code's own tool text. Whether a
    detached game script ever ran is unknown (open check O3).
  - Keep the game live until the scheduled score cut if an agent ends early. Do not wake a clean exit.
    This removes the shorter horizon observed in `study4-solo-1`.
- E3, cap with an early stop (the current launcher). The world can freeze at `run_end` before the fixed cut.
  Report this only as a secondary policy. Its score and horizon can differ from E1 and E2.

Equal-time reads (E0, what the pilot reported) stay as secondary results at `m15` and at the freeze.

### Primary measure

- Holdout: E2 score at the `time_up` freeze (final verifier) for `team-k` minus `silent-k`, at the same shared B.
- Held fixed: team proxy USD B (shared cap) and the deadline T.
- Report proxy USD at the freeze and at execution end. Report overshoot as `max(0, final spend − B)`.
  Report time to cap, uncached input, cache reads and writes, output tokens, world actions, and messages.
  Report E1 at U = B and E0 at `m15` and at the freeze. Report actions as unknown without complete evidence.
- The budget verdict compares measured spend at the same cut. Divide the larger arm mean by the smaller.
  The default tolerance is 5%. An arm can underuse the cap and still match the other arm.

### Marks

- Use `--grade-at 15m,30m` for comparable labels. The score comes from the frozen state for `m30`.
  Use the saved replay frames for interval bounds. Do not assume a continuous exact score.

### Pairing

- Holdout has 1 seed (`holdout-v1`) for every trial, so trials cannot be paired by task instance.
- Pair by wave: each wave launches 1 trial of each arm at the same time, in an order drawn at random, so the arms
  share the provider's load and latency. The paired difference is taken within a wave.
- Power Outage pairs by the 48 hidden scenarios inside the verifier (the same salt for every arm); Seed to Profit
  pairs by its 24 playbook markets. Both still pair trials by wave.

### Trials without a score

- No final grade, a lost sandbox, a provider 402 before freeze, or a Harbor error makes E2 unmeasured.
  A missing intermediate grade makes that mark unmeasured. Neither is a score of 0.
  Pre-register a limit on replacement waves and report the missing slots and their spend.
- Report measured scores even if an arm uses less than B. Budget use is an outcome, not an exclusion.
  Do not claim equal realized dollars when the comparison fails the tolerance rule.
- A 402 after the frozen score state does not void a trial. Example: `study4-silent-2` and `study4-solo-2` each
  logged 1 OpenRouter 402 at +1776.0 s and +1777.4 s from `agent_execution.started_at` (+1826.4 s and +1827.0 s
  from `result.started_at`). That is after the frozen `m30` score state, but 19.9 s and 15.0 s before the `m30`
  `details.written_at` receipt. Use the frozen state timestamp, not the receipt timestamp.

### How n = 1 is treated

- `solo-B` is a headcount and world-resource reference in Holdout. Neither of the 2 solo pilot trials spent
  $0.10. Equal caps do not fix those differences. Power Outage and Seed to Profit keep more resources fixed.
- 1 task. A Holdout result is about Holdout only. The claim "teams help or not at equal money" needs at least
  1 task where `solo-B` is fair (Power Outage) before it is stated in general.

## Part 2.5: leaks and hazards, with stored proof or missing proof

| Hazard | Effect if present | Stored proof | Missing proof or fix |
|---|---|---|---|
| Provider-side web search (`WebSearch`, `WebFetch`) | Reads the public repo, which holds the game and grader | OBSERVED by the library: 0 `WebSearch`, 0 `WebFetch` in 8 of 8 clean pilot logs (`L/benchmarks/2026-11-23-bespoke-holdout-daytona-and-zero-fork-guards.md`, Start here) | Not blocked by config (Part 2.3 change 2). Server-side search calls may also be billed outside the proxy table |
| Git as a covert channel in `silent-k` | Silent arm is not silent | `seal_git: true` in both silent pilot configs. Proofs 2 and 2b contrast open and sealed Git. | Seal Git in both Holdout arms. Git-backed tasks need a submission channel that peers cannot read during play. |
| In-game indirect signals in silent Holdout | Agents see each other's survivors, sites and stock | Not a leak by definition: `comms: False` closes `say`, `shout`, `radio`, `post_note`, `read_notes` only (`engine.py:23,2082`) | Pre-register "silent = no messages", not "no information" |
| Subscription harness bypasses the cap | No cap at all | `HardenedTeamEnvironment` refuses `claude-code` with `model_upstream=subscription` (`run_hardened.py:113-116,148-150`); every pilot `config.json` has `model_upstream=https://openrouter.ai/api`; `agent/proxy-usage.json` has `budget_usd` set | None for Claude Code. Do not use other harnesses without the same check |
| Public hidden tests | Agents read the grader | Daytona enforces `no-network`; library: `network_not_enforced = false` in 8 of 8. Hardened Docker refuses to start without opt-in (`run_hardened.py:105-107`) | Web tools (row 1) |
| Proxy price is not the bill | Matched in list price only | Table in `pricing.json:19`; 1 row checked by hand | No OpenRouter receipt for any Study 4 trial. Fix: record the OpenRouter key usage before and after each trial, run trials of a wave in series, or accept list price as the held quantity |
| Overshoot past B | Realized spend differs by arm | Largest observed call: $0.00187 in 1,622 successes. The proxy admits up to 2 in-flight requests per agent. | If future calls cost no more than that sample maximum, 2k calls could add about $0.0075 at k = 2 or $0.015 at k = 4. This is a planning scenario, not an enforced cost bound. A hard bound needs pre-admission reservation or a proven per-call maximum. |
| Spend after the freeze | Some billed calls cannot change the score | In 6 of 8 pilot trials, proxy charges continued after the freeze. They added $0.0017 to $0.0081 per affected trial. | Report cost at the score cut and at execution end. A call logged after the cut can have started before it. |
| Early `all_finished` | Shorter world horizon for 1 arm | `study4-solo-1` froze at 1629.5 s (1 of 8) | Change 3 (fixed horizon) |
| Unequal request slots | k agents have 2k in-flight slots, solo has 2 | `server.py:194-201` | Part of the treatment; report spend rate |
| Shared OpenRouter account | 402 at start voids a wave | Wave 3: 3 of 3 runs aborted at start | Check credit before each wave; void and rerun the wave |
| Budget shown to agents | Behaviour depends on the shown number | Holdout never shows money (`keepalive.py:177,349`). Power Outage, Seed to Profit and others print `${budget:,.0f}` (`power-outage/.../keepalive.py:142,250`) | At B below $0.50 these tasks show "$0". Use whole dollars there, or patch the text |
| Historic and current guard flags differ | Pilot `guard` is not today's `--arm guard` | OBSERVED: `study4-guard-{1,2}` `board_plugins` end with `coord_guard.py` only; current `run_study4_trial.py:23-25` adds `game_action_guard.py` too | `guard` is not one of the 3 arms; do not pool it |

## Part 2.6: Holdout, 30 minutes, claude-haiku-5-5, k = 2 and k = 4 (NOT RUN)

### Budget from the pilot (k = 2)

OBSERVED from `proxy-usage.jsonl`, `board-events.jsonl` and `verifier/{details.json,replay.jsonl}`.
Times are from `holdout_start`; score intervals are adjacent replay frames at 4-decimal precision.

| Trial | Freeze (s) | Proxy USD at freeze | Time at $0.10 (s) | Replay bracket at $0.10 |
|---|---:|---:|---:|---:|
| `study4-silent-1` | 1772.1 | 0.1146 | 1388 | [0.1100, 0.1100] |
| `study4-silent-2` | 1770.2 | 0.1536 | 1089 | [0.0826, 0.0829] |
| `study4-team-1` | 1772.2 | 0.1840 | 1094 | [0.1100, 0.1100] |
| `study4-team-2` | 1772.8 | 0.1619 | 1185 | [0.1100, 0.1100] |
| `study4-guard-1` | 1768.5 | 0.1932 | 1063 | [0.1100, 0.1100] |
| `study4-guard-2` | 1772.3 | 0.1692 | 1143 | [0.1100, 0.1100] |
| `study4-solo-1` | 1626.9 | 0.0753 | not reached | unknown |
| `study4-solo-2` | 1771.8 | 0.0593 | not reached | unknown |

- Use B = $0.10 for k = 2. All 6 pilot trials at k = 2 crossed $0.10 before the freeze.
  They crossed 1,063 s to 1,388 s after `holdout_start`. The smallest lead was 383.6 s (`study4-silent-1`).
  A $0.12 cap would not bind in 1 of 6 trials: `study4-silent-1` spent $0.1146 by its freeze.
- B = $0.20 for k = 4. INFERRED: no k = 4 pilot exists. Doubling B keeps nominal B/k at $0.05.
  The cap binds at an unknown time. Compare arms only within each k. To test headcount at the same dollars,
  add a k = 4 block at B = $0.10 after calibration; do not compare k = 2 at $0.10 with k = 4 at $0.20.
- The 2 pilot solos did not reach $0.10. A future solo at either cap may underuse it.
- OBSERVED: mean team and silent spend at `m15` was $0.0753 and $0.0755 (2 trials per arm).
  At the frozen final states the means were $0.1730 and $0.1341. That ratio is 1.29.

### Commands, current flags (NOT RUN)

Prerequisites: `build/holdout-{team,silent}/task` exist (built by `run_hardened.py prepare-holdout`). Set `JOBS`
to a new folder outside C. Until change 1 lands, these commands still append to `C/spend-ledger.jsonl`.

```bash
cd ~/Developer/bespoke-macbench-lab
: "${RUN_DATE:?Set RUN_DATE for an approved trial}"
export JOBS="$HOME/Developer/muster-runs/$RUN_DATE/mac-matched/jobs"
common=(--timeout-sec 1800 --grade-at 15m,30m --jobs-dir "$JOBS"
        --harness claude-code --model claude-haiku-5-5 --effort medium)

# k = 2, B = 0.10 (NOT RUN)
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-h30-k2-team-w01 --arm team --n-agents 2 --budget-usd 0.10 "${common[@]}"
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-h30-k2-silent-w01 --arm silent --n-agents 2 --budget-usd 0.10 "${common[@]}"
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-h30-k2-solo-w01 --arm team --n-agents 1 --budget-usd 0.10 "${common[@]}"

# k = 4, B = 0.20 (NOT RUN)
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-h30-k4-team-w01 --arm team --n-agents 4 --budget-usd 0.20 "${common[@]}"
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-h30-k4-silent-w01 --arm silent --n-agents 4 --budget-usd 0.20 "${common[@]}"
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-h30-k4-solo-w01 --arm team --n-agents 1 --budget-usd 0.20 "${common[@]}"
```

These give E3 (early stop) for the capped arms, because the current keepalive freezes at `run_end`.

### Commands, proposed flags (MISSING; NOT RUN)

```bash
# once: web tools denied in the copied managed settings (change 2)
.venv/bin/python run_hardened.py prepare-holdout --variant team
.venv/bin/python run_hardened.py prepare-holdout --variant silent

# E2: use the common array above. These options do not exist in the current launcher.
fixed=(--orchestration "$PWD/orchestrations/holdout_keepalive_fixed_horizon.py"
       --ledger "$JOBS/../spend-ledger.jsonl" --seal-git --game-call-log)
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-e2-k2-solo-w01 --arm team --n-agents 1 --budget-usd 0.10 "${common[@]}" "${fixed[@]}"
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-e2-k2-silent-w01 --arm silent --n-agents 2 --budget-usd 0.10 "${common[@]}" "${fixed[@]}"
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-e2-k2-team-w01 --arm team --n-agents 2 --budget-usd 0.10 "${common[@]}" "${fixed[@]}"
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-e2-k4-solo-w01 --arm team --n-agents 1 --budget-usd 0.20 "${common[@]}" "${fixed[@]}"
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-e2-k4-silent-w01 --arm silent --n-agents 4 --budget-usd 0.20 "${common[@]}" "${fixed[@]}"
keys run -- .venv/bin/python run_study4_trial.py --wave-id mb-e2-k4-team-w01 --arm team --n-agents 4 --budget-usd 0.20 "${common[@]}" "${fixed[@]}"
```

Per-seat cap with existing code only (NOT RUN). `run_hardened.py` passes `--ek` through; the key handling of
`run_study4_trial.py` (OpenRouter key as `ANTHROPIC_API_KEY`, other keys removed) must be done by hand:

```bash
keys run -- sh -c 'env -u GEMINI_API_KEY -u OPENAI_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY="$OPENROUTER_API_KEY" \
  .venv/bin/python run_hardened.py run --wave-id mb-h30-k2-silent-seat-w01 --task "$PWD/build/holdout-silent/task" \
  --backend daytona --n-agents 2 --budget-usd 0.10 --proxy-max-concurrent 2 --timeout-sec 1800 --jobs-dir "$JOBS" -- \
  --ek model_upstream=https://openrouter.ai/api \
  --ek board_plugins=$PWD/build/holdout-silent/task/build/holdout_hub_plugin.py,$PWD/build/holdout-silent/task/environment/hub/game_plugin.py,$PWD/build/holdout-silent/task/environment/hub/keepalive_plugin.py \
  --ek board_policy=$PWD/upstream/tasks/seed-to-profit/task/environment/hub/no_messages_policy.py \
  --ek hub_state_dirs=/var/lib/holdout --ek ttl_minutes=75 --ek seal_git=true --ek agent_budget_usd=0.05 \
  -a multi_agent_harness:TeamAgent --ak harness=claude-code --ak model=claude-haiku-5-5 \
  --ak orchestration=$PWD/build/holdout-silent/task/environment/hub/keepalive.py \
  --ak grade_at=15m,30m --ak checkpoint_every_min=15 --ak effort=medium'
```

### Expected cost for 1 trial (from `C/spend-ledger.jsonl` settle rows)

Daytona rate for the Holdout size (2 CPU, 4,096 MB, 10,240 MB): $0.16668 per sandbox-hour
(`run_hardened.py:19,192-197`; `holdout/task.toml:33-35`). A trial has k agent sandboxes plus 1 hub.

| Arm | Model USD | Daytona USD | Total | Basis |
|---|---:|---:|---:|---|
| `team-2`, `silent-2`, B = $0.10 | About $0.10 if the cap binds | $0.2587 | About $0.36 | Mean compute across 6 k = 2 pilot trials ($0.2566 to $0.2612). The observed-call overshoot scenario adds $0.0075. |
| `solo-B`, B = $0.10 or $0.20 | $0.0593 to $0.0753 in the pilot | $0.1641 in the pilot | About $0.23 | 2 pilot trials. One ended early; a fixed horizon can cost more. Neither trial reached B. |
| `team-4`, `silent-4`, B = $0.20 | About $0.20 if the cap binds | About $0.431 | About $0.63 | INFERRED from $0.2587 × 5/3 sandboxes. The observed-call overshoot scenario adds $0.015. No ledger row verifies k = 4. |

Ledger reservations by `reservation()` (`run_hardened.py:200-207`): k = 2 $0.475, k = 4 $0.825, solo B + $0.25.
OpenRouter's real charge may differ from proxy USD (Part 2.1); the pilot does not show by how much.

## Part 2.7: open checks

What a $0 scripted or local run can settle:

- O1. What Claude Code does on a 429 `budget_exceeded`: exit code, retry, or hang. A local fake Anthropic endpoint
  behind the proxy (the `Proxy` class takes an `httpx` transport, `server.py:99-105`) can show it without a model.
- O2. That the cap and the per-seat cap refuse at the right total, and the overshoot with 2 requests in flight:
  drive `Proxy` with a fake upstream.
- O3. Whether a detached process in an agent sandbox survives `stop()` and can still call the game API after the
  cap: a scripted seat (`agents/scripted_team_agent.py`) on Docker.
- O4. That the fixed-horizon keepalive (change 3) keeps the world running to `time_up` with nobody running, and
  that queued orders still execute: scripted seats on Docker.
- O5. Prove that sealed Git blocks peer writes in every Holdout arm. For Power Outage, check submission and export isolation.
- O6. That `WebSearch` and `WebFetch` in the managed deny list are honoured (needs the CLI to start; a fake endpoint
  is enough).

What only a paid pilot can settle:

- P1. Proxy USD against the OpenRouter charge for the same trial.
- P2. Spend rate and time to B at k = 4 (no k = 4 data exists), and so whether B = $0.20 binds before the freeze.
- P3. Whether a binding cap changes behaviour (crash and resume loops, idle survivors) and the real overshoot.
- P4. The spread of paired `team-k − silent-k` differences under E2, to fix n. The pilot's n = 2 per arm cannot
  fix it; the library gives a detectable effect of about ±0.030 at n = 2.
- P5. Real-run cost and time of Power Outage with a cheap model and a short cap (the library lists this as open).

## Experimental design that a pre-registration can copy

- Question: at the same total proxy USD, does a talking team score higher than a silent team of the same size?
- Primary contrast: `team-k − silent-k`, E2 (cap B, fixed horizon T), Holdout, k = 2 (B = $0.10). k = 4
  (B = $0.20) only after P2 confirms that B binds.
- Secondary contrasts: the same pair under E0 (`m15`, freeze) and E3; `team-k − solo-B` as a reference only.
- Held equal across arms: model (`claude-haiku-5-5`), effort (`medium`), harness (`claude-code`), task seed,
  instruction, price table, 2 in-flight requests per agent, sandbox size, deadline, marks, Daytona `no-network`,
  web tools denied.
- Allowed to differ: in-game messages (the treatment), and the number of request slots (k × 2, part of k).
- Units and pairing: 1 wave = 1 trial of each arm, launched together in a random order; paired difference within
  the wave; test as a paired t-test with a rerun CI, as in the pilot's `tidegauge decompose`.
- Stored pilot: at the $0.10 crossing, the team mean replay bracket is [0.1100, 0.1100].
  The silent mean bracket is [0.09630, 0.09645]. There are 2 trials per arm.
  This is E1 at unequal world times (1,089 s to 1,388 s), not the effect of applying the cap.
- Money gate: no trial before authorization, a funded account, and the E2 changes above.
