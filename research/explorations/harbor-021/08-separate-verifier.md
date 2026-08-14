# Separate verifier environments

## What it is

`[verifier] environment_mode = "separate"` runs verification in a second
sandbox built from `tests/` (its own Dockerfile). Only declared `artifacts`
cross the boundary. The agent cannot see tests; the verifier cannot see
unstated container state. Multi-step tasks can set this per step
(`[steps.verifier.environment]`).

**Already adopted** on `tasks/event-summary` (`environment_mode = "separate"`,
`tests/Dockerfile` = `python:3.13-slim-bookworm`).

## Demo

```bash
bash explorations/harbor-021/demos/run-separate-verifier.sh
```

Re-runs the lab task with oracle:

```bash
harbor run --path tasks/event-summary --agent oracle \
  --jobs-dir runs --job-name separate-verifier-oracle-demo -n 1
```

Observed (2026-08-13): Correctness **1.000**, input_preservation 1.000,
output_hygiene 1.000, reward 1.000, 8s. Trial
`event-summary__YVrwsaQ`:

```
verifier_environment_mode = "separate"
rewards = {correctness, input_preservation, output_hygiene, reward} all 1.0
exception = None
```

The plugin demo (`03-job-plugin-api.md`) independently produced the same
mode on `event-summary__LoHW4aD`. Transcript:
`captures/separate-verifier/demo.log`. Evidence bundle
`evidence/runs/event-summary-oracle-evidence` matches.

## Verdict

**Already adopted; extend into brief 07 and 11.** Every registered canary
and every migrated task should keep a separate verifier image. Combined with
the allowlist note: do **not** set the verifier's `network_mode` to
`no-network` on Docker Desktop — it will be rejected. The isolation that
works today is the second image + artifact handoff, not network policy.
