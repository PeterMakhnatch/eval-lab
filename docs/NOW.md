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

**Harbor-native synthetic benchmarks are now.** They are measurement tasks on
the platform, not a training stack. Three construct families are in flight on
other branches:

| Vertical | Construct | Do not rewrite here |
|---|---|---|
| A | Context / actionable memory | `feat/action-memory-v1` (PR #262) |
| B | MCP-FuncDAG tool composition | `feat/mcp-funcdag-v1` (PR #263), shared substrate PR #268 |
| C | MCP single-fault recovery | `feat/mcp-recovery-v1` (PR #261) |

**Later, not now:** synthetic *training* sets and SFT/RLVR
(`docs/path-forward-2026-08.md` stages S3–S5). Do not start a trainer, a
preference dataset, or an RL loop. The word "synthetic" in those PRs means
Harbor task generators, not fine-tuning data.

**Reward Alignment & Verifier Validity Truth:** Hint-based minimax regret estimates
task solvability/difficulty and guides curriculum selection; it does **not** certify
verifier validity or eliminate reward hacking. Any claim of reward alignment strictly
requires:
1. An independent held-out verifier;
2. NOP and negative mutant baseline controls;
3. Strict prompt/environment contamination separation.

**Benchmark Boundary Distinction (TB3 vs. Tau3):**
- **Terminal-Bench v3 (TB3):** Tracked separately under `role/tbench3-screen@79dd74af`.
- **Tau-Bench 3 (tau3):** In `.worktrees/tau-agentic-canary` on branch `feat/tau-agentic-canary@45484c4af` (8 `banking_knowledge` tasks digest-pinned, frozen pending `wH:p9` remote/provider reconciliation; no repo runs directory or local process).
- **Never combine TB3 and Tau3 evidence, execution, or status reporting.**

## Honest bottleneck

The bottleneck is **runs and populated columns**, not more methods or more
docs. The mechanical tables (`steps`, `tool_calls`, `observations`) have data.
The semantic/capability tables (`capability_opportunities`,
`paired_condition_facts`, and siblings) are empty schemas. Error-timing
columns (`step_to_first_error` and siblings) are all-null on the current
corpus because those trials predate state-journal instrumentation.

The usable analysis corpus is the `status = 'featured'` slice, not the full
feature table. Do not add a feature without a named consumer and a
denominator. Do not report rates over rows with `status != 'featured'`.

## Do not rebuild

- Do not move `src/evallab/` packages. Module locations are frozen until Peter
  approves a new migration.
- Do not merge or delete the two TrajectoryIR modules
  (`src/evallab/trajectory_ir.py` and
  `src/evallab/interpretation/trajectory_ir.py`). That is a named
  Peter-approved gate (PR-0 in the three-vertical program). No facade
  re-export.
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

Skip `docs/research/` and `agents/archive/` (closed-mission notes) unless you
were sent there. `research/inbox/` is a drop box, not a map.

## Live writers — do not collide

One writer per worktree. Before editing, check `gh pr list` and
`git worktree list`. As of 2026-08-28, leave these alone unless you own them:

- Benchmark / MCP families: PRs #268, #267, #263, #262, #261.
- Three-vertical spec: `architecture/three-vertical-program`.
- Architect: `lane/architect` (overnight ledger / ADR-030, PR #230).
- OMP skills: `research/repo-standards-pstack` (PR #260).
- Storage / execution / data lanes under `.worktrees/lane-*`.
- Do not revive `hardening/repo-lean-v1` as a prune; it is old CI/type
  hardening that still touches live `src/` files.

If a path is in someone else's open PR, stop.

## What to add next

Prefer a type, test, or governance check over a new markdown file. If a
correction repeats, encode it in `tests/` or `src/evallab/governance.py`.
Optional agent procedures go in scoped `.omp/skills/` after that root is on
`main`, not in sticky prompt catalogs.
