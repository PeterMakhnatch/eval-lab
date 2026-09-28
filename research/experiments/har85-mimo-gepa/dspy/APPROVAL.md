# HAR-85 DSPy arm: approval gate (bound authorization)

dspy.GEPA runs OUTSIDE the Lab queue (direct `harbor run` per metric call),
so `evallab approve` cannot gate it — there is no queue spec to approve.
The gate is `run-after-approval.sh` + `verify_approval` in `gepa_mimo.py`,
following the Lab's out-of-queue convention
(`src/evallab/gepa_optimizer/workflow.py:557-577`, `_run_campaign`).

## What the approval binds

The approval is a JSON file with exactly three load-bearing fields:

```json
{"binding_sha256": "<sha>", "approved_by": "peter", "approved_at": "<ISO-8601>"}
```

`binding_sha256` is sha256 over canonical sorted-key JSON of every
spend-relevant launch parameter for that phase:

- phase gepa: `gepa_mimo.py` sha256 + `run-after-approval.sh` sha256, split
  `manifest_digest`, student route, reflection model, base policy, train/val
  id lists, `max_metric_calls`, per-trial `cost_limit_usd`, phase `cap_usd`,
  `harbor_env` (where task containers run; the launcher always passes `daytona`).
- phase heldout: launcher sha256, split `manifest_digest`, student route,
  winner policy digest, the 16 heldout ids, attempts, `cost_limit_usd`,
  `cap_usd`, `harbor_env`.

The paid path recomputes the binding and refuses (exit 2) on: binding
mismatch (ANY param change: tasks, calls, cap, environment, code edits, split
change), `approved_by` other than `peter`, missing/unparsable `approved_at`,
or `expected_cost_usd(binding) > cap_usd`. The expected cost the cap must
cover is model API-equivalent (subscription quota) PLUS metered Daytona
sandbox time; `--print-binding` prints both parts. Signed refs are runtime
state: keep them OUTSIDE the repo (e.g. `/private/tmp`), never commit them.
Only the blank templates in `approvals/` are committed.

## Exact commands

Pilot phase 1 (4 train + 2 val, 10 metric calls, cap $3; expected $2.73 =
$1.98 model API-equiv + $0.75 Daytona):

```bash
# 1. Print the binding for the EXACT pilot flags (from the worktree root):
PYTHONPATH=src runs/.harbor-dspy/bin/python research/experiments/har85-mimo-gepa/dspy/gepa_mimo.py \
  --print-binding --split research/experiments/har85-mimo-gepa/split.provisional.json \
  --tasks-root runs/har85-gepa-mimo/tasks --repo-root . --jobs-dir runs/har85-gepa-mimo/jobs \
  --train-tasks candidate-0260-security-appsec,candidate-0390-security-appsec,candidate-0109-science-robotics,candidate-0308-security-forensics \
  --val-tasks candidate-0688-hardware-rtl,candidate-0036-software-data-engineering \
  --max-metric-calls 10 --cost-limit-usd 1.0 --cap-usd 3 --harbor-env daytona --out runs/har85-gepa-mimo/phase1
# 2. Paste binding_sha256 into a copy of approvals/phase1-pilot.template.json
#    kept OUTSIDE the repo; Peter fills approved_by/approved_at.
# 3. Run (binding re-verified before the first trial; coding-plan credential
#    materialized from the owner's OpenCode auth store and DAYTONA_API_KEY
#    read from ~/.omp/agent/.env, both fail closed when absent):
research/experiments/har85-mimo-gepa/dspy/run-after-approval.sh --phase gepa \
  --approval-file /private/tmp/har85-phase1.signed.json --cap-usd 3 \
  --train-tasks candidate-0260-security-appsec,candidate-0390-security-appsec,candidate-0109-science-robotics,candidate-0308-security-forensics \
  --val-tasks candidate-0688-hardware-rtl,candidate-0036-software-data-engineering \
  --max-metric-calls 10
```

Phase 2 (heldout, once; winner digest known only after phase 1; 96 trials,
expected $8.64 = $5.76 model API-equiv + $2.88 Daytona, cap $9):

```bash
# 1. Derive the binding with the phase-1 winner policy:
PYTHONPATH=src runs/.harbor-dspy/bin/python research/experiments/har85-mimo-gepa/dspy/gepa_mimo.py \
  --phase heldout --print-binding --split research/experiments/har85-mimo-gepa/split.provisional.json \
  --tasks-root runs/har85-gepa-mimo/tasks --repo-root . --jobs-dir runs/har85-gepa-mimo/jobs \
  --job-tag x --winner <phase1-policy.json> --attempts 3 --cost-limit-usd 1.0 --cap-usd 9 \
  --harbor-env daytona --out runs/har85-gepa-mimo/phase2
# 2. Sign a copy of approvals/phase2.template.json outside the repo.
# 3. Run:
research/experiments/har85-mimo-gepa/dspy/run-after-approval.sh --phase heldout \
  --approval-file /private/tmp/har85-phase2.signed.json --cap-usd 9 \
  --winner <phase1-policy.json> --attempts 3
```

Pre-spend check without spending (verifies a signed ref against the
recomputed binding, exits 0, launches nothing): add `--verify-only` to the
phase-1 python invocation (with `--approval-file`), or run the same for
`--phase heldout` with `--winner`.

## No bypass statement

There is no `evallab approve` / `--proposer-approval-ref` path for this arm
because dspy.GEPA never enters the Lab queue: each metric call shells to
`harbor run` directly. Routing approval through the queue would be theater —
the queue would constrain nothing the loop does. The binding gate above is
the control, and it is stricter than a token: it pins the code (both files),
the split, every task id, the call budget, the per-trial ceiling, the
execution environment, and the phase cap in one hash. The refusal paths are
exercised, not asserted
(see README.md).
