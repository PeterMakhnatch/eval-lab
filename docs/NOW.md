---
status: living
audience: [builder, analyst, runner, operator]
---

# Start here

Eval Lab finds and measures reward hacking: agents that pass a task by cheating.
The centre of the lab is labelled data where the cheating step is pinpointed.
A verifier reward does not prove an honest solve.

## The 5 functions and their entry commands

| Function | Purpose | Entry command |
|---|---|---|
| run | Execute a task and keep its evidence | `uv run evallab run --help` |
| gold | Read the labelled trace registry | `uv run evallab failures bank gold-counts --json` |
| detect | Judge a finished run | `uv run evallab line --help` |
| watch | Flag a run while it happens | `uv run evallab watch --help` |
| certify | Check a task and its grader | `uv run evallab certify-gold --help` |
| score | Measure the 4 QA contracts below | `uv run evallab score --help` |
| doctor | Check tools, data, and operations | `uv run evallab doctor` |

## Gold today

`evallab.gold.load_rows` and `load_trajectory` provide the public data API.
The source adapters live in `src/evallab/failure_library/bank/sources/`; the registry state lives in `~/.local/state/evallab-failure-library/bank/`.
Action-v2 labels live in `research/evidence/ev1110-gold-grow/action-v2-part-a.jsonl`; full receipts live in `~/Developer/eval-lab-results/gold/`.
`gold-counts` reports 853 rows: 460 binary, comprising 168 executed-positive and 292 reviewed-control.

| Source | Protocol | Rows | Executed-positive | Reviewed-control | Attempted | Unjudged |
|---|---|---:|---:|---:|---:|---:|
| goodfire-action-v1 | action-v1 | 150 | 11 | 70 | 23 | 46 |
| impossible-task-final | action-v1 | 173 | 43 | 32 | 0 | 98 |
| goodfire-action-v2 | action-v2 | 69 | 0 | 0 | 1 | 68 |
| terminal-wrench-action-v2 | action-v2 | 304 | 100 | 121 | 7 | 76 |
| cheatbench-action-v2 | action-v2 | 150 | 13 | 67 | 0 | 70 |
| c1c5-action-v2 | action-v2 | 7 | 1 | 2 | 0 | 4 |

Protocol totals are 323 action-v1 rows and 530 action-v2 rows. These are row views, not independent tasks.

## QA measures and limits

The 4 QA measures cover offline detection, online detection, task QA, and grader QA.
Their current values and coverage are in `~/Developer/eval-lab-results/score/current/scoreboard.json` and its `SCOREBOARD.md`.
[Score](score.md) gives the reproduction command and explains the separate denominators. No pooled accuracy is available.
No untouched test cohort exists: every Goodfire DEV run has stored detector outputs.
Online model monitors have no stored outputs on Gold prefixes. Native deterministic replay is available.
The 2 certifier generations are not merged because the strict study's frozen manifest is lost.
Restore removed code from `pre-deslop-2026-11-10`; [research/ARCHIVE.md](../research/ARCHIVE.md) maps the removals.

## Manuals and delivery

Read `AGENTS.md`, the affected package instructions, and the relevant manual.
Use [Execution tiers](execution-tiers.md), [Line](line.md), [Judge guide](judge-guide.md), [Live watch](live-watch.md), or [Task quality](task-quality.md).
[System](SYSTEM.md), [Data architecture](data-architecture.md), and [Analysis loop](analysis-loop.md) explain retained contracts and analysis.
[Operations](operations.md), [Third-party tools](third-party.md), [Ship](ship.md), and [Generated/cache policy](GENERATED-CACHE-POLICY.md) cover operations and delivery.
A merge does not authorize execution, deployment, or spend. Follow `agents/CHECKS.md` for exact-head delivery.

## Documentation metadata

The unused builder context compiler is retired. Read the relevant manual directly.
There is no replacement compiler, alias, or context-pack command.
Markdown metadata remains available through `evallab.storage.attach.parse_doc` and `parse_front_matter`.
Lineage uses the same parser. The z4 attachment still scans all Markdown files under `docs/`.

Manuals use YAML front-matter with `status: living` and an `audience` list.
The audience values are `builder`, `analyst`, `runner`, and `operator`.
Historical artifacts can use `status: historical`. Untagged documents remain `unclassified`.
Metadata describes a document. It does not grant authority or execution permission.

## Evidence and comparison boundaries

- Preserve raw jobs, task versions, source hashes, and exposure records.
- Keep missing rewards and unavailable measurements distinct from zero.
- Use the `status = 'featured'` slice for feature-based analysis rates.
- Name the corpus, source revision, query, denominator, and unavailable sources with each result.
- Keep Terminal-Bench and Tau-Bench evidence separate. TB3 and TB4 scores are not interchangeable.
- Hint-based minimax regret does not certify a grader or remove reward hacking.
- Require independent held-out verification, negative controls, and contamination separation for validity claims.
- A catalog snapshot in `docs/STATUS.md` is not current runtime evidence.

## Placement and delivery boundaries

Keep `cli.py`, the native runner, and the queue in their current locations.
The reserved `cli/` and `execution/` namespaces do not authorize a package migration.
`src/evallab/trajectory_ir.py` owns the retained TrajectoryIR contract (`interpretation/trajectory_ir.py` is retired).
Use an owned worktree. Preserve another writer's changes and unique ignored evidence.
Follow `agents/WORKFLOW.md` and `agents/CHECKS.md` for exact-head delivery.
Current MiMo study navigation remains in [mimo/README.md](mimo/README.md).
