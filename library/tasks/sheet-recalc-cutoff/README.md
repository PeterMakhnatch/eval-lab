# `peter/sheet-recalc-cutoff`

The harder sibling of [`../sheet-recalc`](../sheet-recalc/): same engine, same
API, plus **early cutoff**. A cell is only re-evaluated when its value could
actually change: it was edited, it just left a cycle, or one of the cells it
reads changed value. Unchanged results stop propagation, which is what makes an
incremental engine fast on real sheets and what most first implementations get
wrong.

See [instruction.md](instruction.md) for the agent-visible contract.

## What makes it hard

The cutoff rule turns a worklist into a fixpoint with equality checks, and it
interacts with everything else in the engine:

- the change test is on `get()` values, so clearing a cell that held `0` is not
  a change, and an error that stays an error is not a change;
- an edited cell is always evaluated even if its value ends up identical;
- a cell that leaves a cycle must be evaluated even when **none** of its inputs
  changed value, because its old value was `#CYCLE`;
- the cells a formula reads come from its parse, not from its values, so ranges
  over empty cells still participate;
- `set_many` compares against the values from before the batch, so two edits
  that cancel out must still evaluate the edited cells.

The shipped engine (identical to the one in `../sheet-recalc`) fails 2 of the 5
visible tests and, by construction, cannot pass the cutoff tests.

## Environment

`python:3.12-slim`, one CPU, 2048 MB, 1800 s agent timeout, public network (the
task needs none). The agent sees `/app/sheet`, `/app/tests` and
[instruction.md](instruction.md). Run the visible tests with
`cd /app && python -m pytest -q tests`.

## Verifier

Same structure as `../sheet-recalc`, with 24 tests:

- `environment_mode = "separate"`; the submission never enters the pytest
  process. It runs as `nobody` in a scratch package with the grader's pristine
  `sheet_formula.py`, behind a per-run nonce sentinel on a duplicated fd.
- The oracle recomputes every affected cell **without** cutoff into its own
  cache, then derives the expected evaluation set from the contract: affected,
  not on a cycle, and (edited, or just left a cycle, or reads a cell whose value
  changed). Probes compare the engine with a from-scratch recompute and also
  self-check the oracle's cache, so an oracle bug fails as `ORACLE BUG`, not as
  a submission failure.
- Tests: everything from `../sheet-recalc` plus cutoff chains through `*0`,
  stable errors, zero/empty equivalence, a cell leaving a cycle while its
  remaining input stays `#CYCLE`, `set_many` change detection across a batch,
  the 10,000-cell chain, wide fan-out, and six seeds × 300 randomized operations
  biased toward formulas whose value survives an input change.

## Controls

`uv run evallab hack run library/tasks/sheet-recalc-cutoff --execute` (local
Docker, no model calls), measured 2026-10-28 on Harbor 0.24.0:

| Script | expect | measured |
|---|---|---|
| reference solution (oracle agent) | 1 | 1 |
| nop agent | 0 | 0 |
| `alt-recursive-dfs` (different algorithm, correct, with cutoff) | 1 | 1 |
| `partial-no-cutoff` (the fully correct `sheet-recalc` engine) | 0 | 0 |
| `partial-no-cycle-exit` (cutoff without the cycle-exit rule) | 0 | 0 |
| `game-recompute-all` | 0 | 0 |
| `game-lazy-get` | 0 | 0 |
| `game-own-evaluator` | 0 | 0 |
| `game-exit-zero` | 0 | 0 |
| `game-forged-result` | 0 | 0 |
| `game-patch-formula-and-tests` | 0 | 0 |

Randomized tests catch `partial-no-cycle-exit` on all six seeds without the
targeted cycle-exit test, so the verifier does not rest on a single hand-built
case.

## Model calibration

Claude Code trials on the lab's subscription gateway, 2026-10-28:
`claude-sonnet-5-5` 0/2 (over-evaluation on the randomized sequences),
`claude-haiku-5-5` 2/2, `claude-opus-5-5` 2/2. Numbers, failure analysis and
method:
[`research/experiments/sheet-recalc-model-runs`](../../../research/experiments/sheet-recalc-model-runs/README.md).

## Known limits

- The exact cutoff rule is an authoring choice. It is the property that makes
  incremental recomputation worth having, and it is stated in the instruction,
  but a production spreadsheet would also need dirty flags for volatile
  functions (random, now) that the contract deliberately excludes.
- The rubric measures evaluation *counts*; it does not time the engine. A very
  slow but exactly-once implementation passes on the 10,000-cell chain.
- Only the scripted exploit channels above are measured. The verifier has not
  been mutation-tested.
