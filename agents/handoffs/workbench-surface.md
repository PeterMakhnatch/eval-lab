Status: review-wanted
Last: widened the accepted `task.toml` surface to the real task library — seven keys admitted for their inert value only, each arriving with a `_MODELLED_CONSTRUCT_VALUES` entry citing the Harbor 0.21.0 line behind its refusal — then re-ran all four packages and confirmed every earlier refusal still fires
Next: review. `unsupported_task_configuration` on `library/tasks/` is down from 16 occurrences to 1, and that one (`environment.allow_internet`) is a refusal on the merits argued in Step 2
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

## Step 2 — the widening

Commit `ad1d228`. The allowlist design is untouched: `SUPPORTED_TASK_CONFIG` is
still a closed mirror and everything outside it is still refused outright. What
changed is that seven keys are now inside it, each admitted for exactly one
value — the inert one Harbor folds away — and each arriving with the check that
models it:

| admitted key | accepted value | value model |
|---|---|---|
| `environment.os`, `verifier.environment.os` | `"linux"` | `_is_default_task_os` |
| `environment.gpus`, `verifier.environment.gpus` | `0` | `_is_zero_gpus` |
| `environment.mcp_servers`, `verifier.environment.mcp_servers` | `[]` | `_is_empty_array` |
| `environment.env`, `verifier.environment.env`, `verifier.env`, `solution.env` | empty table | `_is_empty_table` |
| `verifier.collect` | `[]` | `_is_empty_array` |

`_MODELLED_CONSTRUCT_VALUES` is that model — one `_ModelledValue` per dotted key
per side, each carrying the Harbor 0.21.0 source line that makes the refusal of
any other value a fact rather than a preference: `docker.py:218-222`/`:265-275`
for `os`, `base.py:367-369` and `:745-750` for `gpus`, `trial.py:829-837` with
`config.py:616-636` for `mcp_servers`, `utils/env.py:94-130` with
`verifier.py:166-171` and `trial.py:778-813` for `env`, `trial.py:999-1029` for
`collect`. `[solution]` is admitted as a table because `SolutionConfig` carries
exactly one field, `env` (`config.py:335-336`), so naming it closes the table.
Full prose per key is in `docs/task-workbench.md`.

Two decisions are worth carrying into review.

**`_scan_supported_table` decides an admitted key by its value and never
descends into it.** The accepted shape is empty or scalar by construction, so
there is nothing below it; descending would let an unmodelled child pass under
an allowlisted parent, which is the shape of both holes the earlier review
rounds found.

**`environment.allow_internet` is still refused, and that is the point.**
`library/tasks/query-optimize` declares it, so refusing it keeps one
`harness_defect` on the board. Harbor folds the alias into `network_mode` only
when `network_mode` is absent from `model_fields_set` *and* `allowed_hosts` is
`None` (`config.py:885-892`); mirroring that three-way interaction would stand a
second, weaker network resolver beside `_effective_verifier_network`. A task
states its policy with an explicit `network_mode` instead. The refusal note now
says all of that instead of naming the path.

## Step 3 — re-measured, same command, same four packages

| task | diagnostics | `harness_defect` | new diagnostic codes |
|---|---|---|---|
| `event-summary` | 8 → 5 | 3 → 0 | none |
| `query-optimize` | 23 → 18 | 6 → 1 | none |
| `terminal-bench-html-js-filter` | 16 → 15 | 1 → 0 | none |
| `transaction-reconciliation` | 19 → 13 | 6 → 0 | none |

Every removed diagnostic is an `unsupported_task_configuration`; the code
multiset is otherwise unchanged per task, so nothing was traded for anything.
The single remaining `harness_defect` is `query-optimize`'s `allow_internet`
refusal argued above: 16 `unsupported_task_configuration` occurrences across the
four packages before, 1 after.

All four still exit 1 with `static_passed: false`, on genuine task findings —
`adversarial_cases_insufficient`, `base_image_unpinned`, `source_ref_unpinned`,
`verifier_network_not_isolated`, `script_not_executable`, `build_network_use`,
`golden_data_leak` and the rest. Widening the accepted surface was never
supposed to certify a task; it was supposed to stop the workbench reporting its
own limits as the task's defects. That is what it did.

## Step 4 — verification and lease

- `uv run pytest tests/test_task_workbench.py -q`: 122 passed (102 before this
  mission). New: the `inert-surface` fixture, whose whole point is that the real
  library's shape certifies with zero diagnostics *and* resolves to the same
  network policy as the reference package; one negative case per admitted key
  per side; `test_admitted_values_are_inert_in_harbor_itself`, which resolves the
  documents through Harbor's own `TaskConfig` so the *acceptances* are pinned to
  Harbor 0.21.0 the same way the refusals are; and
  `test_every_key_admitted_for_one_value_arrives_with_that_value_model`, the
  module's binding rule made executable — it fails if a later key is admitted
  without a value model, or if a rename leaves a model unreachable and therefore
  silently not running.
- `uv run pytest -q`: 547 passed.
- `uv run ruff check .`: clean. `ruff format` diverges on this file, but it
  diverges identically at `ad67126` in regions this mission never touched, and
  CI does not run it; left alone rather than restyled under a lease.
- Diff against `ad67126` touches five paths, all inside the lease:
  `src/evallab/task_workbench.py`, `tests/test_task_workbench.py`,
  `tests/fixtures/task_workbench/cases/inert-surface/task.toml`,
  `docs/task-workbench.md`, this file. `library/` and `policy/` are byte-identical
  to `ad67126`.
- No paid model, cloud sandbox, Harbor run, Docker build, deploy, or publication.
  No API-key environment variable read or introduced. `task_workbench plan` is
  static analysis; the control executions it would need are not part of it.
