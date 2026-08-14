# 07 — Canary suite + drift detection

Register 3–5 pinned canaries: the migrated transaction-reconciliation task,
one adapted terminal-bench task (pin `terminal-bench/terminal-bench@<version>`
via `harbor download`; never `@latest` in a comparison), one more local task.
Nightly enqueue under the `canary` policy rule, 3 attempts. A SQL view computes
trailing-7-day mean ± σ per (task, agent); the digest flags excursions as
*harness-drift suspects* (the `harness_failure` taxonomy row), explicitly not
capability news.

Acceptance: canaries run two consecutive nights unattended; an artificial
perturbation (e.g. bumping a task version) is flagged in the digest.

## Repository-wide constraints

- Preserve immutable `runs/` and rebuildable PostgreSQL.
- Keep deterministic extraction before model analysis.
- Put every new JSON contract in `src/harbor_lab/schemas.py` as a Pydantic
  model.
- Add dependencies only with `uv add`; `uv.lock` is authoritative.
- The executor is the only application code path that may invoke Harbor or
  Docker.
- Canary members and external package revisions are immutable pins; reject
  `latest`, `head`, floating branches, or missing task digests.
- No billable run in tests; stub the runner. Live checks use only Oracle/no-op.
