# Rules for agents in this repository

Read [docs/DESIGN.md](docs/DESIGN.md) first. It is the contract.

## Fixed rules

1. No model decides any result. Reports come from files and arithmetic.
2. The core (`src/muster/` except `runner.py` and `agents/`) imports only the standard library.
3. Only `runner.py` starts a process. Only `load.py` opens trial files.
4. Statistics (intervals, tests) belong to `tidegauge`. Do not copy them here.
5. Never commit run outputs, tokens of real services, or API keys. `runs/` is ignored.
6. Never run a model on this machine. Real-agent trials call hosted APIs and need the owner's budget approval.

## Parallel work

- One writer per path. A lane owns the paths named in its brief or in its `docs/BACKLOG.md` card.
- Do not edit `model.py`, `docs/DESIGN.md`, `pyproject.toml` or `cli.py` from a lane. Ask the integrator.
- A lane runs its own tests and `ruff` on its own paths. The integrator runs the whole suite and commits.
- Lanes in the shared checkout do not run `git add`, `git commit`, `git stash`, `git reset` or `git checkout`.

## Starting trials

Trials run on the pinned fork of Harbor, never on the global `harbor` command:

```sh
export MUSTER_HARBOR=/Users/petermakhnatch/Developer/harbor-multiagent-muster/.venv/bin/harbor
```

The local Docker VM has 4 GB of memory. Run at most 2 trials at a time and remove what you start
(`environment.delete: true`). Never touch the containers `eval-lab-postgres-1` and `fxtr-postgres`.

## Checks

```sh
uv sync && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
MUSTER_DOCKER=1 uv run pytest -q -m docker     # real containers, no model, $0
```

## Words

seat, roster, policy, condition, family, instance, world, event, trial, job, arm.
