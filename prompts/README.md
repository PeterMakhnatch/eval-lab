# Component implementation briefs

These prompts are scoped handoffs for coding agents. They are ordered because
later components depend on evidence contracts established earlier.

| Order | Brief | Outcome |
|---:|---|---|
| 1 | [01-atif-index.md](01-atif-index.md) | validated ATIF facts and Parquet projection |
| 2 | [02-cohort-compare.md](02-cohort-compare.md) | deterministic cohort comparison |
| 3 | [03-analysis-pipeline.md](03-analysis-pipeline.md) | provenance-bearing model analysis wrapper |
| 4 | [04-proposal-gate.md](04-proposal-gate.md) | follow-up proposals with an execution approval gate |

Use one writing agent per Git worktree and one brief per branch. Before starting,
the agent must read `AGENTS.md`, `docs/architecture.md`,
`docs/analysis-loop.md`, and the selected brief. Do not run a paid model, cloud
sandbox, large sweep, deployment, or publication as part of implementation or
validation without Peter's explicit approval.

The briefs intentionally require runnable increments rather than an all-at-once
platform build. Each one ends with fixture-based tests and a documentation
update. If current Harbor behavior contradicts a brief, inspect the installed
version and adapt the implementation while documenting the discrepancy; do not
invent compatibility behavior from memory.
