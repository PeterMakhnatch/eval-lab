# muster

**Controls, task QA and exact measurement for multi-seat [Harbor](https://github.com/olamlabs/harbor-multiagent) tasks.**

`harbor-multiagent` runs several agents in one environment. Each agent sits in a Compose service, a *seat*,
and one verifier grades the trial. muster answers the questions that come before and after such a run.
It uses files and arithmetic. No model decides any result.

| Question | Command |
|---|---|
| Does this task give credit to the right seat? | `muster matrix`, `muster attribute` |
| Is the recorded reward what the event log gives? Did a seat act after play ended? | `muster regrade` |
| Who acted, who talked, and who received which message before it acted? | `muster events` |
| How do I run 1 roster of real agents over a set of tasks? | `muster run` |
| Did the team gain come from more agents, or from coordination? | [`tidegauge decompose`](https://github.com/PeterMakhnatch/tidegauge) on the job directories |

muster also has a **world kit** and 4 **task families** built on it. Each family has the same task in
several conditions (1 agent, a team without a channel, a team with a channel), so a team result can be
split into its parts.

## The control matrix: who earns the reward?

A *roster* gives every seat a scripted policy: `oracle` (the reference solution), `nop` (does nothing), or
any script. The matrix runs every roster. It costs $0, because no model runs.

This is the 2-seat example task of `harbor-multiagent`. Seat `p1` is under test. The policy `idle` does
nothing. The policy `spoof` posts the note of `p1` from the seat where it runs.

```
$ muster matrix examples/tasks/hello-seats --policies oracle,idle,spoof --exploits spoof --null idle --attempts 2
task hello-seats: 18 trials in 9 rosters
seats: p1 (under test), p2

roster               n  reward  notes
p1=oracle,p2=oracle  2       1
p1=oracle,p2=idle    2       1  timeout p1 2/2
p1=idle,p2=oracle    2       0
p1=idle,p2=idle      2       0
p1=idle,p2=spoof     2       1
...

Findings
severity  code                    finding
error     tested-seat-not-needed  Roster p1=idle,p2=spoof: tested seat p1 is idle and reward is 1, the all-oracle reward.
error     adversary-wins          Roster p1=idle,p2=spoof: p2=spoof gives reward 1, above 0 with p2=idle.
```

The tested seat did nothing, and the trial has the full reward. The example trusts the `author` field of
a request. The matrix finds this in 6 minutes on a laptop.

Finding codes are stable: `unsolvable`, `free-pass`, `tested-seat-not-needed`, `credit-leak`,
`decorative-seat`, `adversary-wins`, `not-deterministic`, `trial-error`. [docs/DESIGN.md](docs/DESIGN.md) has the rules.
A task can list codes that its design produces on purpose in `[metadata] expected_findings`. They stay in the
report as `expected` and do not fail the run. `--policy-env NAME=VALUE` sets the environment of scripted seats.

## Replay grading: is the reward a function of the log?

A world-kit task grades with a pure function `score(events, instance)`. So muster recomputes the reward of
a finished trial from the event log that the trial left.

```
$ muster regrade runs/pilot
4 trials replayed from their event logs: 4 match

Every recorded reward equals the reward that its event log gives.
```

The verifier keeps the log that it graded. Harbor collects the task's artifacts a moment before that.
An action that is in the graded log and not in the collected log happened after play ended. `muster regrade`
lists these actions. A new diagnostic never needs a new run: change `score`, and replay.

## Event measures: a sent message is not a received message

Every request to a world is 1 line in its event log, with the seat that the world derived from the token.
`muster events` computes measures that need no judge:

```
$ muster events runs/commons-team-3 runs/commons-indep-3
arm              trials  actions  posts  fetches  probes  talk share  unread acts  never fetched  ...
commons-team-3      1/1     10.0    9.0     12.0     0.0         68%          7.0            6.0
commons-indep-3     1/1      3.0    0.0      0.0     0.0          0%          0.0            0.0
```

- **unread acts:** actions taken while a message of another seat was posted and not yet received.
- **never fetched:** pairs of a message and another seat where that seat never received the message.
- **informed duplicates:** repeats of another seat's act after the actor received that seat's message.
- **probes:** requests for routes that do not exist. Agents without a channel search for one.

## The world kit

`tasks/_kit/` holds the part that every muster task shares.

1. **Identity comes from a token.** A request never names its own author. Each seat has its own token.
2. **Each seat has its own internal network.** A seat reaches the world service and nothing else: no
   other seat and no internet. So a condition without a channel has no network path between seats.
3. **Every request is 1 event** in `/logs/world/events.jsonl`, with a sequence number without gaps.
4. **The verifier closes the world first.** Later requests are refused and marked `late`.
5. **Budgets are world rules,** counted for each seat. Time is never the budget.
6. **Grading is a pure function** of the event log and the instance.

## Task families

| Family | The question | Conditions |
|---|---|---|
| `boxes` | Does a team find more because it has more agents, or because it coordinates? | `solo`, `solo-x3`, `indep-3`, `indep-3-salt`, `team-3` |
| `ledger` | Can a team pool facts that no member has alone? What does a too-early answer cost? | `solo`, `indep-3`, `team-3` |
| `commons` | Do agents share a pool without an overdraw? Do they keep their pledges? | `indep-3`, `team-3` |
| `scarce` | When not every need fits, who yields? Do agents state their need truthfully? | `indep-3`, `team-3` |

Each family has 10 instances, scripted policies for the control matrix, exact reference scores, and a list
of everything that one seat can observe about another seat. `python tasks/<family>/generate.py --check`
proves that the committed tasks are what the generator writes.

## Install and use

```sh
git clone git@github.com:PeterMakhnatch/muster.git && cd muster && uv sync
export MUSTER_HARBOR=/path/to/harbor-multiagent/.venv/bin/harbor   # the fork, never a global harbor
uv run muster matrix tasks/boxes/build/team-3/boxes-i01 --policies oracle,nop
uv run muster run tasks/boxes/build/team-3 --roster experiments/rosters/t2-deepseek-flash-plain.json --attempts 3
uv run muster regrade runs/<job>
uv run muster events runs/<job>
```

`muster matrix` and `muster run` start trials through the `harbor` command of the fork. The other commands
only read files. The core imports only the Python standard library.

## Results so far

[docs/RESULTS.md](docs/RESULTS.md) is the 1-page summary of the model experiments: more agents against talking,
why 1 agent with the budget of 3 beats the talking team, and the limits.
[docs/FINDINGS.md](docs/FINDINGS.md) has the dated observations, for example: the upstream example pays a
tested seat that did nothing; seats on 1 network can talk without the channel; in `scarce`, a false claim of
need pays. [experiments/LOG.md](experiments/LOG.md) is the running log.

## Limits

- The control matrix shows what scripted policies can do. It does not prove that no other exploit exists.
- `muster regrade` imports a task's `tests/verify.py`. Use it on tasks that you trust.
- The families are small synthetic tasks. A result says how a harness and a model behave on these rules.
- Statistics are not here. `tidegauge` reads the job directories that muster writes.

## Development

```sh
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
MUSTER_DOCKER=1 uv run pytest -q -m docker     # real containers, no model
```

[AGENTS.md](AGENTS.md) has the rules for agents. [docs/DESIGN.md](docs/DESIGN.md) is the contract.
[experiments/PREREGISTRATION.md](experiments/PREREGISTRATION.md) states the experiments before the runs.

## License

Apache-2.0. muster is an independent project. It is not part of Harbor or of Olam Labs.
