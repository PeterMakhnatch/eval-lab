Status: building
Last: measured the real certification gap — ran `python -m evallab.task_workbench plan` against all four `library/tasks/` packages on ad67126 and recorded every diagnostic per task plus the union of eight refused `unsupported_task_configuration` keys
Next: widen the accepted surface one key at a time, each with the check that models it, then re-run all four and confirm the previously-verified refusals still fire
Blockers: none

# WORKBENCH-SURFACE handoff

Mission: **F-06** — the M007 task-quality workbench cannot certify any of the
four tasks this repository actually runs.

Agent/model: Claude Opus 4.5 (Anthropic), Oh My Pi harness, subscription-only.
Worktree `.worktrees/workbench-surface` on `role/workbench-surface`, branched
from `origin/main` `ad67126`. No paid model, cloud sandbox, Harbor run, Docker
build, deploy, or publication. No API-key environment variable read or
introduced. Nothing under `policy/` touched or weakened. Nothing under
`library/` touched — it is outside this lease and version-pinned besides. The
primary checkout was read only.

Lease: `src/evallab/task_workbench.py`, `tests/test_task_workbench.py`,
`tests/fixtures/task_workbench/`, `docs/task-workbench.md`, this file.

## Step 1 — the measured gap, before any change

Command, run once per package from this worktree at `ad67126`:

```
uv run python -m evallab.task_workbench plan library/tasks/<task> \
  --source-uri local://library/tasks/<task> --source-ref ad67126 --license proprietary
```

All four exit 1; all four `static_passed: false`.

| task | diagnostics | of those, `harness_defect` |
|---|---|---|
| `event-summary` | 8 | 3 |
| `query-optimize` | 23 | 6 |
| `terminal-bench-html-js-filter` | 16 | 1 |
| `transaction-reconciliation` | 19 | 6 |

### Correction to the F-06 record

The flight report describes `event-summary` as **six** diagnostics (3
`task_defect` + 3 `harness_defect`). It measured `86380b0`. At `ad67126` the
same package reports **eight**: the three `harness_defect` items are unchanged,
and two further `task_defect` items — `verifier_image_unpinned`
(`tests/Dockerfile`) and `verifier_network_not_isolated` (`task.toml`) — have
appeared from checks merged between those two commits. Both are genuine task
findings, not workbench limitations. F-06's substance is unaffected; the count
in the checkpoint is simply stale, and the checkpoint is append-only so it is
corrected here rather than rewritten.

### Union of refused `unsupported_task_configuration` keys — all four packages

Eight distinct keys, every one a `harness_defect` at `error` severity on
`task.toml`:

| refused key | tasks that declare it |
|---|---|
| `environment.mcp_servers` | `event-summary`, `query-optimize`, `transaction-reconciliation` |
| `environment.os` | `event-summary`, `transaction-reconciliation` |
| `verifier.collect` | `event-summary`, `transaction-reconciliation` |
| `environment.env` | `query-optimize`, `transaction-reconciliation` |
| `verifier.env` | `query-optimize`, `transaction-reconciliation` |
| `solution` | `query-optimize`, `transaction-reconciliation` |
| `environment.gpus` | `query-optimize`, `terminal-bench-html-js-filter` |
| `environment.allow_internet` | `query-optimize` |

The defect is confirmed as stated in the assignment: the allowlist's accepted
surface was drawn from what `tests/fixtures/task_workbench/` uses, not from what
`library/tasks/` uses. Every one of the eight keys above is a documented
`harbor.models.task.config` field, and three of them are keys `event-summary` —
the package `doctor` validates and `AGENTS.md` advertises — declares.

### The decisive observation

**Every real occurrence of these keys is empty or the schema default.**
`mcp_servers = []`, `collect = []`, `os = "linux"` (the `TaskOS` default),
`gpus = 0`, and `[environment.env]` / `[verifier.env]` / `[solution.env]` as
empty tables. Only `environment.allow_internet = true` in `query-optimize`
carries a value that changes Harbor's behaviour.

That shapes the fix and is why widening does not have to weaken anything. The
inert declaration is what the real library needs admitted; the loaded value is
what the allowlist exists to refuse. Full per-key justification in Step 2.
