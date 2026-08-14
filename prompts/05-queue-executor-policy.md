# 05 — Queue + executor + policy gate

Build `src/harbor_lab/queue.py` and extend the CLI with `submit`, `tick`,
`approve`, `reject`, `stop`, `resume`. Directory queue as in
`docs/design-additions.md` §2.1; pydantic `ExperimentSpec` (extend the existing
`experiments/*.json` schema with `submitted_by`, `priority`, `est_cost_usd`,
`policy_rule`); policy loader for `policy/standing-approvals.yaml`; cost ledger
check against the catalog; `events.jsonl` appender. The executor wraps the
existing `harbor_lab.runner` and auto-ingests on completion.

Acceptance: two agents submit concurrently without interference; an
out-of-policy spec lands in `waiting/`; a spec past the ceiling is refused with
a reason file; `STOP` halts dispatch; every transition appears in
`events.jsonl`; `uv run pytest` covers the state machine with a stub runner.

## Repository-wide constraints

- Preserve immutable `runs/` and rebuildable PostgreSQL.
- Keep deterministic extraction before model analysis.
- Put every new JSON contract in `src/harbor_lab/schemas.py` as a Pydantic
  model.
- Add dependencies only with `uv add`; `uv.lock` is authoritative.
- The executor is the only application code path that may invoke Harbor or
  Docker.
- Ship the conservative `policy/standing-approvals.yaml` defaults from
  `docs/design-additions.md` §2.2. Never loosen them automatically.
- No billable run in tests; stub the runner. Live checks use only Oracle/no-op.
