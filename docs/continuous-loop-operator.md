---
status: living
audience:
  - operator
  - runner
---

# Continuous-loop operator (disabled by default)

**No services or runs authorized.** This runbook covers operator validation
and unit *templates* for the PR #271 control loop. It does not start launchd,
systemd, Docker, Harbor, or billable model calls. Platform still owns
`campaigns.py` / `continuous_control_plane.py` (not in this change). Data still
owns `trajectory_compliance_ops.ingest_after_settlement` (documented hook
name only).

## Files

| Path | Role |
|---|---|
| `scripts/ops/continuous-operator` | Operator CLI |
| `src/evallab/ops_continuous.py` | Gate stack and state-dir oracles |
| `scripts/ops/keychain-inject.sh` | Secret *reference* probe (no values) |
| `scripts/ops/cloud-worker-bootstrap.sh` | Vendor-neutral worker template |
| `scripts/ops/launchd/com.petermakhnatch.evallab.continuous-operator.plist` | Disabled launchd unit |
| `scripts/ops/systemd/evallab-continuous-operator.service` | Disabled systemd unit |
| `scripts/ops/systemd/evallab-continuous-operator.timer` | Inactive timer |
| `containers/continuous-operator/` | `restart: "no"` image + compose |
| `policy/continuous-loop-policy.example.yaml` | Unsigned example; empty refs keep DISABLED |

Runtime state lives in `--state-dir` / `EVAL_LAB_OPERATOR_STATE` (gitignored
when you use `operator-state/` locally). Never `~/Library/LaunchAgents`.

## Commands

All commands are local JSON oracles. Exit `0` only when the action is valid
**and** still non-running. Gate failures exit `2` with a closed reason code.

```bash
uv run python -m evallab.ops_continuous validate --state-dir /tmp/op-state
scripts/ops/continuous-operator validate --state-dir /tmp/op-state
scripts/ops/continuous-operator dry-run --agent oracle --state-dir /tmp/op-state
scripts/ops/continuous-operator dry-run --agent nop --state-dir /tmp/op-state
scripts/ops/continuous-operator status --state-dir /tmp/op-state --policy policy/continuous-loop-policy.example.yaml
scripts/ops/continuous-operator quota --state-dir /tmp/op-state
scripts/ops/continuous-operator pause --state-dir /tmp/op-state
scripts/ops/continuous-operator drain --state-dir /tmp/op-state
scripts/ops/continuous-operator restart --state-dir /tmp/op-state
scripts/ops/continuous-operator upgrade --state-dir /tmp/op-state
scripts/ops/continuous-operator rollback --state-dir /tmp/op-state
scripts/ops/continuous-operator maintenance --state-dir /tmp/op-state
scripts/ops/continuous-operator kill --state-dir /tmp/op-state
scripts/ops/continuous-operator rotate-logs --state-dir /tmp/op-state
scripts/ops/continuous-operator rotate-cas --state-dir /tmp/op-state
scripts/ops/keychain-inject.sh
scripts/ops/cloud-worker-bootstrap.sh
```

`--agent codex` (or any non-oracle/nop) on `dry-run` returns `billable_refused`.
Oracle/nop dry-run writes `dry-run-plan.json` with `harbor: false` and does
not invoke `evallab run`.

## Enable token, standing approval, budget, secret

Inject by environment or state files. Never put token material in git or argv
logs (the CLI prints reason codes and mode, not secret values).

| Input | How |
|---|---|
| Enable token | `EVAL_LAB_ENABLE_TOKEN` |
| Standing approval | `EVAL_LAB_STANDING_APPROVAL` or `$STATE/standing_approval` |
| Distinct identities | `EVAL_LAB_ENABLE_IDENTITY` ≠ `EVAL_LAB_APPROVAL_IDENTITY` |
| Budget | `EVAL_LAB_BUDGET_PRESENT=1` or `$STATE/budget` |
| Secret reference | `EVAL_LAB_SECRET_REF=keychain:service/account` |
| Secret presence probe | `EVAL_LAB_SECRET_PRESENT=1` or `$STATE/secret_present` (existence only) |
| Policy | `--policy path` (`continuous_loop_policy` fields from PR #271 §5) |
| Clock | `--now` / `EVAL_LAB_OPERATOR_NOW` (tests) |

Closed reason codes: `missing_enable_token`, `missing_standing_approval`,
`missing_budget`, `missing_secret`, `stale_heartbeat`, `drain_incomplete`,
`default_disabled`, `same_enable_and_approval_identity`, `billable_refused`.

Default `validate` with no inputs → `default_disabled`. Incomplete signed
policy fields also keep the operator DISABLED.

## Units (remain disabled)

- launchd: `Disabled=true`, `RunAtLoad=false`, no `KeepAlive`. Do not
  `launchctl bootstrap`.
- systemd: `[Install]` has no `WantedBy=`. Do not `systemctl enable`.
- compose: `restart: "no"` and `profiles: ["manual"]`. Do not `docker compose up`.
- Cloud bootstrap prints the worker protocol and exits `0` without starting a VM.

## Drain vs kill

- `drain` waits on `$STATE/inflight.json`. If
  `maintenance_drain_timeout_seconds` elapses with leases remaining →
  `drain_incomplete`. Empty inflight → mode `DISABLED`.
- `kill` writes `$STATE/kill.json` with `FAILED_OPERATOR_KILL` and
  `executed: false` (no process signal).

## Health / CAS rotation

`status` reports `UNKNOWN` when heartbeat/health files are absent. A heartbeat
older than `operational_limits.scheduler_stale_after_seconds` →
`stale_heartbeat`. `rotate-logs` / `rotate-cas` record intent under the state
dir and do not delete `research/evidence` or live CAS.

## Postrun compliance hook

After Platform PR-1E settlement exists, Data
`ingest_after_settlement` is the postrun hook. This operator layer does not
call it.

## Focused proof

```bash
uv run pytest tests/test_continuous_operator.py -q
```
