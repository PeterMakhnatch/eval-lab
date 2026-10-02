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
- The native Harbor `mimoagent` wrapper is fingerprinted as
  `mimoagent-default`, with its validated SDK revision, the committed
  `swe.yaml` step limit and the Xiaomi sampling enforced by the proxy.
  Its server parser comes from `serve.py` rather than the Terminus
  response normalizer. The existing `mimo` parser and 64K context still
  need explicit deviations from the training reference.
