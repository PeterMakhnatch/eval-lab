---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
---

# Where the lab is now

Ten-minute orientation for incoming agents. Binding rules stay in `AGENTS.md`,
`agents/CHECKS.md`, and `agents/WORKFLOW.md`. This file is the current-state
map, not a second copy of those contracts.

## What we are building

Three product layers, in this order:

| Layer | Job | Where it lives |
|---|---|---|
| **Platform** | Harbor wrapper: admit, run, preserve evidence | `src/evallab/runner.py`, `queue.py`, `cli.py`, `library/` |
| **Data pipeline** | ATIF → facts → Parquet features | `src/evallab/evidence/`, `storage/`, `interpretation/feature_registry.py` |
| **Research analysis** | Questions over those tables | `research/analysis/`, `sql/`, `src/evallab/cohort.py`, `curve.py` |

Later, not now: synthetic training sets and SFT/RLVR (`docs/path-forward-2026-08.md`
stages S3–S5). Do not start a training stack.

A separate **measurement program** (context/memory, MCP-FuncDAG, recovery)
runs *on* those layers. That program is in-flight on other branches. Do not
rewrite it here.

## Honest bottleneck

The bottleneck is **runs and populated columns**, not more methods or more
docs. The mechanical tables (`steps`, `tool_calls`, `observations`) have data.
The semantic/capability tables (`capability_opportunities`,
`paired_condition_facts`, and siblings) are empty schemas. Many registered
features are all-null or constant on the current corpus because older trials
predate state-journal instrumentation.

Do not add a feature without a named consumer and a denominator. Do not
report rates over rows with `status != 'featured'`.

## Do not rebuild

- Do not move `src/evallab/` packages. Module locations are frozen until Peter
  approves a new migration.
- Do not merge or delete the two TrajectoryIR modules
  (`src/evallab/trajectory_ir.py` and
  `src/evallab/interpretation/trajectory_ir.py`). Canonical choice is a
  separate Peter-approved gate.
- `src/evallab/cli/` and `src/evallab/execution/` are empty reserved directories.
  CLI is `src/evallab/cli.py`. Runner/queue are top-level modules.
- Do not install Cursor Pstack, Graphite, Bun, or TypeScript helpers.
- Do not add SciPy/statsmodels/lifelines without a named analysis consumer.
- Do not treat `docs/INDEX.md`, `docs/repo-map.md`, or `docs/STATUS.md` as a
  tour. The first two are generated inventories; STATUS is a catalog snapshot
  and goes stale.

## Read these, in this order

1. `AGENTS.md` — rules.
2. This file — current state.
3. `agents/OWNERS.md` and `agents/WORKFLOW.md` — who may write where.
4. `docs/architecture.md`, `docs/data-architecture.md`, `docs/analysis-loop.md`
   — why it is built this way.
5. The `AGENTS.md` next to the package you will touch — after the layout-truth
   note in `src/evallab/AGENTS.md`.

Skip `docs/research/` and `agents/archive/` unless you were sent there.

## Live writers — do not collide

One writer per worktree. Before editing, check `gh pr list` and
`git worktree list`. As of 2026-08-28, leave these alone unless you own them:

- Benchmark / MCP families: PRs around shared FastMCP substrate, trajectory
  program, FuncDAG, action-memory, mcp-recovery.
- Architect: `lane/architect` (overnight ledger / ADR-030).
- OMP skills: `research/repo-standards-pstack` (PR #260).
- Storage / execution / data lanes under `.worktrees/lane-*`.

If a path is in someone else's open PR, stop.

## What to add next

Prefer a type, test, or governance check over a new markdown file. If a
correction repeats, encode it in `tests/` or `src/evallab/governance.py`.
Optional agent procedures go in scoped `.omp/skills/` after that root is on
`main`, not in sticky prompt catalogs.
