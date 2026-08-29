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
scripts/ops/continuous-operator start --state-dir /tmp/op-state
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

Approval and budget are **digest-bound signed manifests** (`$STATE/approval.json`,
`$STATE/budget.json`, or `EVAL_LAB_APPROVAL_MANIFEST` / `EVAL_LAB_BUDGET_MANIFEST`
paths). Env presence flags (`EVAL_LAB_STANDING_APPROVAL`, `EVAL_LAB_BUDGET_PRESENT`)
are self-assertion and are refused. Signers for enable, campaign-approval, and
budget must be pairwise distinct. Manifests carry `scope` (must include
`continuous-loop`), `expires_at`, `payload_digest`, and a bound `signature`.
Never put token material in git or argv logs.

| Input | How |
|---|---|
| Enable token | `EVAL_LAB_ENABLE_TOKEN` + `EVAL_LAB_ENABLE_IDENTITY` |
| Standing approval | signed `campaign_approval` manifest; policy `approval_digest` must match |
| Budget | signed `budget` manifest with scope/expiry |
| Secret reference | `EVAL_LAB_SECRET_REF=keychain:service/account` |
| Secret presence probe | `EVAL_LAB_SECRET_PRESENT=1` or `$STATE/secret_present` (existence only) |
| Policy | `--policy` full typed nested fields; nulls keep DISABLED |
| Clock | `--now` / `EVAL_LAB_OPERATOR_NOW` (tests) |

Closed reason codes: `missing_enable_token`, `missing_standing_approval`,
`missing_budget`, `missing_secret`, `stale_heartbeat`, `drain_incomplete`,
`default_disabled`, `same_enable_and_approval_identity`, `billable_refused`.

Default `validate` with no inputs → `default_disabled`. Incomplete signed
policy fields also keep the operator DISABLED.

## Units (remain disabled)

- launchd: `Disabled=true`, `RunAtLoad=false`, no `KeepAlive`, user-session
  `Aqua`, logs under `~/Library/Logs/evallab/`, absolute interpreter path.
  Do not `launchctl bootstrap`.
- systemd: `[Install]` has no `WantedBy=`. `User=evallab`, `NoNewPrivileges`,
  `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, empty
  `CapabilityBoundingSet`, `RestrictAddressFamilies=AF_UNIX`, mode `0700`
  state. Do not `systemctl enable`.
- compose: `restart: "no"` and `profiles: ["manual"]`; image `uv sync --locked`
  then non-root `USER`. Do not `docker compose up`.
- Cloud bootstrap prints the worker protocol and exits `0` without starting a VM.

## Drain vs kill

- `drain` waits on `$STATE/inflight.json`. If
  `maintenance_drain_timeout_seconds` elapses with leases remaining →
  `drain_incomplete`. Empty inflight → mode `DISABLED`.
- `kill` writes `$STATE/kill.json` with `FAILED_OPERATOR_KILL` and
  `executed: false` (no process signal).

## Health / CAS rotation

`status`, `validate`, `quota`, and `start` fail closed on a missing or stale
heartbeat once a typed policy is present (`stale_heartbeat`). `rotate-logs` /
`rotate-cas` record intent under the state dir and do not delete
`research/evidence` or live CAS. `start` never launches a process.

## Postrun compliance hook

After Platform PR-1E settlement exists, Data
`ingest_after_settlement` is the postrun hook. This operator layer does not
call it.

## Focused proof

```bash
uv run pytest tests/test_continuous_operator.py -q
```
