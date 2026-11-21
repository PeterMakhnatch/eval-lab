# tidegauge: instructions for agents

tidegauge is a small command line tool. It reads the job directories that the
[Harbor](https://github.com/harbor-framework/harbor) framework writes and reports numbers that a
reader can trust: an audit, a score with error bars, a paired comparison, rank intervals, a run
plan, and a pass or fail gate.

## Rules that do not bend

1. **Deterministic.** The same input gives the same output. No model call decides or writes a
   result. Exact tests come first. A Monte Carlo fallback uses a fixed seed.
2. **Standard library only at runtime.** `dependencies = []` stays empty. Tests may use pytest.
3. **No Harbor import.** Read the JSON files that Harbor writes. Harbor is a heavy install, and
   its users must be able to run tidegauge anywhere.
4. **Read only.** tidegauge never writes into a job directory, never starts a trial, never opens
   a network connection, and never spends money.
5. **Small.** The source budget is 4,000 lines in `src/`. If a change needs more, delete
   something first or split a separate package. Prefer one clear function to a framework.
6. **Honest words.** State what the data shows and what it cannot show. An unknown is "n/a",
   never 0. Every number in a document comes from a command that the reader can run again.

## Layout

| Part | Files | Role |
|---|---|---|
| Kernel | `src/tidegauge/model.py`, `load.py` | `Trial`, `Job`, `Arm`; the only code that opens Harbor files |
| Kernel | `src/tidegauge/stats.py` | Pure statistics. No I/O. `docs/STATISTICS.md` derives each formula |
| Kernel | `src/tidegauge/audit.py` | Checks that yield `Finding` objects |
| Kernel | `src/tidegauge/render.py`, `commands/_common.py` | Text tables and shared arguments |
| Commands | `src/tidegauge/commands/<name>.py` | One module per command: `configure`, `build`, `text`, `run` |
| Tests | `tests/test_*.py`, `tests/harbor_fixture.py` | The fixture writes small Harbor job directories |

A command reads kernel objects and returns a plain `dict` report from `build(...)`. `text(report)`
renders that dictionary, and `--json` prints it. A command never opens a Harbor file itself.

## How to add a command

1. Write `src/tidegauge/commands/<name>.py` with `HELP`, `configure(parser)`, `build(...) -> dict`,
   `text(report) -> str` and `run(args) -> int`. Copy the shape of `commands/score.py`.
2. Add the module to `COMMANDS` in `src/tidegauge/commands/__init__.py`. This is the only shared line.
3. Write `tests/test_<name>.py`. Build inputs with `tests/harbor_fixture.py`. Compute each expected
   number by hand, not with the code under test.
4. Add 1 row to the command table in `README.md`. The `--help` text of the command is its reference.

## How to add an audit check

Write one function `check_<name>(arm) -> Iterator[Finding]` in `audit.py`, add it to `ARM_CHECKS`
(or `PAIR_CHECKS`), and add 1 test that triggers it and 1 that does not. Use `error` only when the
count itself is wrong, `warn` when the score depends on a condition, `note` for a useful fact.

## How several agents work at the same time

- Work comes from `docs/BACKLOG.md`. Each card names the files that it owns. Two cards never own
  the same file, so two agents never edit the same file.
- Take 1 card. Write your name and the date on its `Owner` line in the same commit that starts the work.
- Work in your own Git worktree and branch: `git worktree add ../tidegauge-wt/<card> -b <card>`.
  Do not edit the primary checkout.
- Kernel files are shared. Only 1 open pull request may change them. Put `kernel:` at the start of
  its title and keep the change as small as the card needs. A command card that needs a new
  kernel field asks for it in the card first.
- Deliver through a pull request. Merge when CI is green on the exact head. Then delete the branch
  and the worktree.
- A card is done only when its `Done when` list is true and a person who did not write the code can
  rerun each number.

## Checks before every push

```sh
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

## Words

Tool output and documents use short sentences, active voice, simple present tense, and digits for
numbers. One word has one meaning: `trial` (one attempt), `task`, `arm` (one configuration),
`job` (one `harbor run`), `rerun` and `tasks` (the 2 error bars), `valid` and `all` (the 2
counting rules). Do not introduce a second name for any of these.

## Real data

`scripts/fetch_tb2_leaderboard.sh` downloads the public Terminal-Bench 2.0 leaderboard submissions
(about 320 MB of `result.json` files). Use it to check a change against 32,803 real trials. Never
commit downloaded data. Name the dataset commit next to every number that comes from it.
