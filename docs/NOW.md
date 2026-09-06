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

Orientation revised 2026-09-06. Live presence comes from the pickup counter and
Git/PR metadata; scientific availability requires a named corpus and exact ref.

## What we are building

Three product layers, in this order:

| Layer | Job | Where it lives |
|---|---|---|
| **Platform** | Harbor wrapper: admit, run, preserve evidence | `src/evallab/runner.py`, `queue.py`, `cli.py`, `library/` |
| **Data pipeline** | ATIF → facts → Parquet features | `src/evallab/evidence/`, `storage/`, `interpretation/feature_registry.py` |
| **Research analysis** | Questions over those tables | `research/analysis/`, `sql/`, `src/evallab/cohort.py`, `curve.py` |

**Harbor-native synthetic benchmarks are now.** They are measurement tasks on
the platform, not a training stack. Three construct families are merged on
main as benchmark packages under `library/benchmarks/`:

| Vertical | Construct | Package (PR) |
|---|---|---|
| A | Context / actionable memory | `library/benchmarks/action-memory-v1` (#262) |
| B | MCP-FuncDAG tool composition | `library/benchmarks/mcp-funcdag-v1` (#263, shared substrate #268) |
| C | MCP single-fault recovery | `library/benchmarks/mcp-recovery-v1` (#261) |

**Training scope & integration-spine distinction:** Offline trajectory-training *design*
(selection recipes, held-out freeze specifications, quarantine design, deterministic S0
fixtures) is authorized, but **no** paid API calls, GPU compute, model registration, or
training runs are authorized. Do not start a trainer, submit training jobs, or launch
unattended optimization loops.

**Integration-spine vs. Main availability:** Work merged to the integration branch
(`integrate/spine-batch1`) is NOT automatically available on `main`. Status reporting must
distinguish between implemented, reviewed, merged-to-branch, merged-to-main, and
scientifically available. Never assume missing `main` files mean spine work does not exist,
and never claim integration-spine deliverables are landed on `main` before reviewed integration.
**Reward Alignment & Verifier Validity Truth:** Hint-based minimax regret estimates
task solvability/difficulty and guides curriculum selection; it does **not** certify
verifier validity or eliminate reward hacking. Any claim of reward alignment strictly
requires:
1. An independent held-out verifier;
2. NOP and negative mutant baseline controls;
3. Strict prompt/environment contamination separation.

**Benchmark Boundary Distinction (TB3 vs. Tau3):**
- Terminal-Bench v3 (TB3) and Tau-Bench 3 (tau3) are distinct benchmark families.
- Never combine their evidence, execution, or status reporting. Resolve current
  registrations and worktree/PR refs rather than reusing an old canary snapshot.

## Honest bottleneck

The research bottleneck is trustworthy runs and populated, provenance-bound
columns, not additional method catalogs. Earlier corpus snapshots had empty
semantic/capability tables and all-null error-timing columns. Those are dated
observations, not a current database census. State the corpus digest, query,
timestamp, and unavailable sources before reporting present coverage.

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

## Read only the route for the task

Start with `AGENTS.md` and this orientation, then the affected files—not a universal
stack of architecture documents. These routes do not waive safety boundaries or
the verification gates in `agents/CHECKS.md`.

| Task | Read next, limited to the affected surface |
|---|---|
| Documentation or navigation | The document being changed and its direct references. Read `agents/STRUCTURE.md` only for placement changes; architecture only when the design claim changes. |
| Worktree, ownership, or delivery | Relevant `agents/WORKFLOW.md` / `agents/OWNERS.md` sections; `agents/CHECKS.md` at the checkpoint. Use the existing board for a backlog claim. |
| Platform, runner, queue, devloop, or CLI | `src/evallab/AGENTS.md`, the nearest scoped instructions, and the affected sections of `docs/architecture.md`. Read `docs/execution-tiers.md` before execution decisions. |
| Storage, CAS, or projections | Storage/evidence scoped instructions and the relevant `docs/data-architecture.md` sections; follow their direct authority contracts. |
| Analysis or interpretation | Analysis/interpretation scoped instructions and the relevant `docs/analysis-loop.md` sections; identify the actual corpus and consumer. |
| Task authoring or registration | The task's own contract plus relevant `docs/task-workbench.md` / `docs/task-registry.md` sections; retain execution approval and immutable-version rules. |

Use targeted lookup for a missing concept or symbol. Read `docs/research/` and
`agents/archive/` only when historical evidence is needed, not as default context.
`research/inbox/board.md` owns the pull/backlog protocol; `claims/` holds pickup
records. `agents/missions/ACTIVE.md` is a navigation link to those sources.

## Live writers — do not collide

Use `agents/WORKFLOW.md` for disjoint ownership and integration rules. Before
editing, inspect `gh pr list` and `git worktree list --porcelain`. Verify each
PR's base and head; main-targeting and integration-targeting work are different
review scopes. This orientation deliberately carries no live PR roster.

## What to add next

Prefer a type, test, or governance check over a new markdown file. If a
correction repeats, encode it in `tests/` or `src/evallab/governance.py`.
Optional procedures belong in the existing scoped project skills, not sticky
prompt catalogs or a second coordination board.
