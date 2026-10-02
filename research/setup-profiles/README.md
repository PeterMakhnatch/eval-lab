---
status: living
audience:
  - builder
  - runner
---

# Setup reference profiles

One measured setup per file. A MiMo batch names its reference in the spec
(`reference_profile: <name>` plus `deviations: [{field, value, reason}]`);
dispatch compares the batch's setup fingerprint against the named profile and
refuses on any uncovered difference. See `docs/contracts.md` and
`docs/trial-treatment.md`.

## Profiles

| File | Setup |
|---|---|
| `xiaomi-mimo-rl.yaml` | Xiaomi MiMo RL training setup (HAR-148): the `mimoagent` default agent on `example_configs/swe.yaml`, verl rollout sampling, locked rollout network. |

## Rules

- Every compared field cites its source: a paper section or a `mimoagent`
  file:line. A value that cannot be sourced is marked `unsourced` with the
  reason. Do not guess.
- Unsourced reference fields are recorded, never compared: nothing can differ
  from an unknown value.
- `compared_fields` lists exactly what dispatch compares. Deployment identity
  (`server.model_revision`, `server.sglang_image`) is recorded in every
  fingerprint but never compared: the reference pins behaviour, not which
  container serves it.
- `lock.mode` and the task ledger binding are hard gates, never deviations:
  a MiMo agent run pins `egress_lock: true` explicitly (implicit defaults caused the
  2026-10-01 open-network run) and its task must sit in
  `research/experiments/python-task-ledger/ledger.csv` with a matching digest.
- Model-free agents (nop/oracle) skip the reference and the harness/server/
  sampling comparisons; they keep the effective lock resolution and the ledger
  binding.
- Request ceilings are lower-bound comparisons, not exact matches: a cap below
  the reference `budgets.step_limit` requires a `budgets.max_requests`
  deviation. Cumulative token ceilings are compared only when the reference
  cites a cumulative token budget; an unsourced budget is shown as unknown in
  preflight, not inferred from the context window. Runaway backstops remain
  explicit spec fields.
- `harness.additions` records enabled loop breaking, output caps, completion
  fixes, and extra instructions/rules/skills. These are absent from the native
  reference and require a declared deviation when enabled.
- Reports keep verifier outcomes separate from stop categories. Proxy ceilings,
  trial budgets and loop breaking are `our_limit`; native step caps are
  `harness_step_limit`, distinct from task timeouts and model completion. A
  job with more than 5% `our_limit` stops is marked **setup-limited** in the job
  rollup and each run page; this never changes rewards or attempt counts.
