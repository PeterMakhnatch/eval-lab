# `peter/sheet-recalc`

A hand-authored SWE task about incremental recomputation. The agent repairs the
recalculation core of a spreadsheet engine to an exact contract: values current
when the edit returns, ranges that depend on cells that are still empty, cycle
detection and recovery, atomic batches, and **exactly one** `formula.evaluate`
call per affected cell, after its inputs are final.

See [instruction.md](instruction.md) for the agent-visible contract.

## Why this is hard

The shipped engine (a plausible legacy implementation) has the bugs that make
this family hard to get right, not a single planted line:

- dependencies are recorded only for cells that already exist, so `SUM(A1:A5)`
  never wakes when `A3` is filled in later;
- replacing a formula never unlinks its old reads, so dead edges fire;
- propagation is a recursive DFS with no visited set, so a diamond evaluates
  `C1` twice and `D1` three times, and a 10,000-cell chain dies on
  `RecursionError`;
- cycle state is written into the cycle member only and never cleared, so a cell
  stays `#CYCLE` after the cycle is broken;
- `set_many` is a loop over `set`, so a batch evaluates intermediate states.

Recursive naive implementations produce **correct values in the fuzz tests and
still fail**: the contract is about which cells are evaluated, not only what
they return. That is the property the grader measures.

## Environment

`python:3.12-slim`, one CPU, 2048 MB, 1800 s agent timeout, public network (the
task needs none). The agent sees `/app/sheet` (with the correct, immutable
`formula.py`), `/app/tests` (four visible smoke tests; three pass and
`test_each_dependent_evaluated_once` fails on the shipped engine), and
[instruction.md](instruction.md). Run them with
`cd /app && python -m pytest -q tests`.

## Verifier

`environment_mode = "separate"`. The verifier image builds from `tests/`, gets
`/app` as an artifact, and never imports the submission into the pytest process.

- `/tests/test_sheet.py` (mode 700, root) owns an independent oracle: it parses
  with its own pristine `sheet_formula.py`, recomputes all values from scratch,
  finds cycles with Kosaraju SCC, and derives the expected evaluation set as
  the readers-closure of the edits minus cycle members.
- Each check sends an operation list to `/tests/runner.py`, which copies
  `sheet_formula.py` plus the submitted `engine.py` into a scratch package and
  runs it as `nobody` with `python3 -I -S`. The runner wraps
  `formula.evaluate`, records the cell name of every call, and reports values
  from `get()` after each operation.
- The result comes back on a duplicated stdout fd behind a per-run 128-bit
  nonce sentinel, so a submission that writes to any inherited fd cannot forge
  a result. A run without exactly one sentinel fails the test.
- Tests: literals/arithmetic, invalid input atomicity, transitive chains,
  diamonds, live ranges over empty cells, stale-edge removal, cycles and their
  downstream, cycle breaking, false-cycle suppression, error propagation and
  recovery, `set_many` atomicity, a 10,000-cell chain edited three times (head,
  full cascade, cycle, recovery), wide fan-out with 2,000 readers, and six
  seeds × 300 randomized operations against the oracle.

## Controls

`controls/` is the standard matrix, replayed with
`uv run evallab hack run library/tasks/sheet-recalc --execute` (local Docker,
no model calls). Measured 2026-10-28 on Harbor 0.24.0:

| Script | expect | measured |
|---|---|---|
| reference solution (oracle agent) | 1 | 1 |
| nop agent | 0 | 0 |
| `alt-recursive-dfs` (different algorithm, correct) | 1 | 1 |
| `game-recompute-all` (right values, wrong work) | 0 | 0 |
| `game-lazy-get` (recalculate on read) | 0 | 0 |
| `game-own-evaluator` (private copy of the evaluator) | 0 | 0 |
| `game-exit-zero` (`os._exit(0)` at import) | 0 | 0 |
| `game-forged-result` (write a fake result to every fd) | 0 | 0 |
| `game-patch-formula-and-tests` (tamper with the agent image) | 0 | 0 |

Static scan (`evallab hack scan`) reports four claims, all accepted with
reasons: the "test material in the agent image" is the visible smoke suite the
ticket deliberately gives the agent; the agent container runs as root with
public network, which is the Harbor baseline and touches no grader state, since
only `engine.py` is collected.

## Model calibration

Claude Code trials on the lab's subscription gateway, 2026-10-28:
`claude-haiku-5-5` 2/2, `claude-sonnet-5-5` 1/1, `claude-opus-5-5` 1/1. This
contract did not separate them. Numbers and method:
[`research/experiments/sheet-recalc-model-runs`](../../../research/experiments/sheet-recalc-model-runs/README.md).

## Known limits

- The engine executes inside the verifier container as `nobody`. It cannot read
  `/tests` (mode 700) and cannot forge the result channel, but it shares the
  container with pytest; a submission that exploits a Python or OS bug rather
  than the task is not in scope and is not claimed to be blocked.
- "Exactly once per affected cell" is an authoring choice. It is the property
  that separates incremental recomputation from recomputation, and it is stated
  in the instruction, so it is fair, but a real spreadsheet need not promise it.
- The 10,000-cell test bounds recursion depth and gross algorithmic shape, not
  worst-case complexity.
- The verifier has not been mutation-tested (`evallab hack mutate`); the
  controls above measure the scripted channels, not every weaker-check family.
