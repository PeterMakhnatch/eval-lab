# Design contract

This file is the contract between the parts of `muster`. Every module and every task family obeys it.
Change it only together with the code and tests that depend on it.

## 1. What muster is

`harbor-multiagent` (Olam Labs' fork of Harbor) runs several agents in one environment. Each agent
sits in a Compose service, a *seat*. One verifier in `main` grades the trial.

muster answers the questions that come before and after such a run:

| Question | Part |
|---|---|
| Does this multi-seat task give credit to the right seat? | control matrix (`muster matrix`, `muster attribute`) |
| Is the task package sound before any run? | static checks (`muster lint`) |
| Did the team gain come from more agents, or from coordination? | condition families (`tasks/`), `muster run`, then `tidegauge decompose` |
| Is the recorded reward what the event log gives? | replay grading (`muster regrade`) |
| What did each seat do, and when? Which messages did it receive before it acted? | exact event measures (`muster events`) |

Rules, fixed:

1. **No model decides any result.** Every number comes from files and arithmetic.
2. **The core imports only the standard library.** Only `runner.py` and `agents/` touch Harbor,
   and only by starting the `harbor` command or by being imported by it.
3. **Analysis is read-only and deterministic.** The same files give the same bytes.
4. **Statistics live in `tidegauge`.** muster writes ordinary Harbor job directories; `tidegauge`
   reads them. muster does not copy interval or test code.
5. **Run outputs stay outside Git** (`runs/` is ignored). Commit only summaries without secrets.

## 2. Layout

```
src/muster/
  model.py        dataclasses shared by all modules (section 3)
  load.py         the only code that opens trial files
  roster.py       rosters and the control matrix (pure)
  runner.py       the only code that starts a process (the pinned `harbor`)
  attribution.py  control-matrix analysis (pure)
  lint.py         static checks of a seat task directory (pure)
  events.py       exact measures over a world event log (pure)
  regrade.py      recompute rewards from collected event logs (imports a task's tests/verify.py)
  render.py       text tables and JSON
  agents/script.py  ScriptAgent: a Harbor agent that runs one shell script in its seat
  commands/<name>.py  one module per command: configure(parser), build(args) -> dict, text(report) -> str, run(args) -> int
  cli.py
tasks/<family>/   condition families (section 5)
tests/            pytest; real-container tests are opt-in with MUSTER_DOCKER=1
docs/             DESIGN.md (this file), FINDINGS.md, BACKLOG.md
experiments/      configs and committed summaries of real runs
```

## 3. Data model (`model.py`)

```python
@dataclass(frozen=True)
class SeatRun:
    seat: str                    # service name
    under_test: bool
    agent: str                   # harness name, e.g. "oracle", "terminus-2", "script"
    model: str | None            # "provider/name" or None
    policy: str | None           # ScriptAgent: the script's stem; otherwise None
    exception: str | None        # exception type on this seat
    started_at: str | None       # ISO, agent execution
    finished_at: str | None
    tokens_in: int | None; tokens_cache: int | None; tokens_out: int | None
    cost_usd: float | None
    trajectory: Path | None      # seats/<seat>/agent/trajectory.json when present
    logs: Path                   # seats/<seat>/agent

@dataclass(frozen=True)
class SeatTrial:
    path: Path
    trial_name: str
    task: str                    # last part of task_name, lower case
    task_checksum: str | None
    rewards: dict[str, float]    # every key the verifier wrote; {} when there is none
    reward_valid: bool
    exception: str | None        # trial-level exception type
    seats: tuple[SeatRun, ...]   # task declaration order
    artifacts: Path              # <trial>/artifacts
    started_at: str | None; finished_at: str | None
    def roster_key(self) -> str  # "p1=oracle,p2=nop": seat=policy-or-agent[/model], declaration order
```

A directory that is not a readable seat trial becomes `Problem(path, reason)`. The loader never raises
on bad input files and never guesses a missing value.

## 4. World kit: what every muster task's world does

A muster task has one world process in `main` (stdlib `http.server`, port 8080, alias `world`).

1. **Identity comes from a token, never from the request body.** Each seat service has
   `SEAT=<seat>` and `SEAT_TOKEN=<32 hex>` in its Compose `environment`. `main` has
   `SEAT_TOKENS={"<token>": "<seat>", ...}`. A request sends `Authorization: Bearer <token>`.
   A request without a valid token gets 401 and is logged with `seat: null`.
2. **Every request is one event.** The world appends one JSON line to `/logs/world/events.jsonl`:
   `{"seq": 1, "t": 1763541500.123, "seat": "p1", "kind": "open", "ok": true, ...}`.
   `seq` starts at 1 and has no gaps. `t` is `time.time()` in the world. Extra keys are per `kind`.
   `task.toml` lists the file under `artifacts`.
3. **The world can be closed.** `POST /admin/close` from inside `main` (source address is loopback)
   stops all changes. Later seat requests get 409 and are logged with `"late": true`. The verifier
   calls it first, then grades from the event log, not from live state.
4. **Per-seat budgets are world rules,** counted per token and logged. Time is never the budget.
5. **The verifier writes `/logs/verifier/reward.json`.** `reward` is the team score in [0, 1].
   `seat.<name>` is that seat's own credit when the family defines one. Other numeric keys are
   diagnostics (for example `messages`, `duplicate_opens`). All values are finite numbers.
6. **Seat images** carry `bash`, `curl`, `python3`, `tmux`, `git`, `ca-certificates`, a non-root user
   `agent` with home `/home/agent`, and `init: true`. They run `sleep infinity`.
7. **Grading is a pure function of the event log.** `tests/verify.py` defines
   `score(events, instance) -> dict[str, float]`. `events` is the full event list. `instance` is the
   parsed `environment/world/instance.json` (in `main`: `/world/instance.json`). The function reads no
   file and no clock. So `muster regrade` can recompute every reward offline from a trial's collected
   `events.jsonl`, and a new diagnostic never needs a new run.
8. **Instructions tell the full rules:** the API, the budget, how many other agents there are, whether a
   message channel exists, and how the team is scored. An instruction never names a file in `tests/`.

## 5. Condition families (`tasks/<family>/`)

A family is one task idea in several *conditions*, generated from one template:

```
tasks/<family>/
  family.toml      name, description, instances, seed, the conditions and their seat counts
  template/        world.py, verify.py, Dockerfiles, instruction templates, policies/
  generate.py      deterministic: python tasks/<family>/generate.py writes build/
  build/<condition>/<family>-iNN/   generated Harbor task directories, committed
```

- The Harbor task name is `muster/<family>-iNN` in every condition, so the conditions pair by task.
- Standard conditions: `solo` (1 seat), `indep-N` (N seats, no message channel), `team-N` (N seats,
  message channel `POST /msg`, `GET /msgs`). A family can add more, for example `solo-xN` (1 seat
  with N times the budget).
- Instance `iNN` uses the same world layout (same seed) in every condition.
- `template/policies/<name>.sh` are scripted seat policies for the control matrix. Every family ships
  `oracle.sh` (best legal play for this seat, in every condition) and can ship adversaries such as
  `spoof.sh` (act as another seat) or `late.sh` (act after play ends). `solution/solve.sh` is `oracle.sh`.
- `python tasks/<family>/generate.py --check` fails when `build/` differs from what the generator writes.

## 6. Control matrix

A *roster* gives every seat one policy. The matrix is the set of rosters over a list of policies.

- Built-in policies: `oracle` (Harbor's oracle agent, runs `solution/solve.sh`), `nop` (Harbor's nop agent).
- Any other policy name `x` means `template/policies/x.sh` or `<task>/policies/x.sh`, run by `ScriptAgent`.
- `muster matrix <task> --policies oracle,nop --attempts 2 --out runs/x` writes one Harbor job per
  roster under `runs/x/<roster_key>/` and starts them with the pinned `harbor`.
- `muster attribute runs/x` reads the jobs and prints, per reward key: the reward of every roster, each
  seat's effect (mean change when only that seat goes from `nop` to `oracle`), and findings.

Findings (codes are stable):

| Code | Severity | Rule |
|---|---|---|
| `unsolvable` | error | every seat `oracle`, and the reward is below 1 |
| `free-pass` | error | every seat `nop`, and the reward is above 0 |
| `tested-seat-not-needed` | error | a tested seat is `nop`, and the reward equals the all-oracle reward |
| `credit-leak` | error | `seat.<s>` is above 0 while seat `s` is `nop` |
| `decorative-seat` | warn | no reward key changes with that seat's policy |
| `adversary-wins` | error | a roster with an adversary policy raises a reward key above the same roster with `nop` in that seat |
| `not-deterministic` | warn | one roster gives different rewards across attempts |
| `trial-error` | warn | a trial has an exception or no valid reward |

A task can list codes in `[metadata] expected_findings` when its design produces them on purpose, for
example `tested-seat-not-needed` in a game where only 2 of 3 seats can score. Those findings stay in the
report with severity `expected` and do not count as errors or warnings. `trial-error` is never expected.

## 7. Words

seat, roster, policy, condition, family, instance, world, event, trial, job, arm. One word per thing.
