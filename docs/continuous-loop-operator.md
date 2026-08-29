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
scripts/ops/continuous-operator recover --state-dir /tmp/op-state
scripts/ops/continuous-operator rotate-logs --state-dir /tmp/op-state
scripts/ops/continuous-operator rotate-cas --state-dir /tmp/op-state
scripts/ops/keychain-inject.sh
scripts/ops/cloud-worker-bootstrap.sh
```

`--agent codex` (or any non-oracle/nop) on `dry-run` returns `billable_refused`.
Oracle/nop dry-run writes `dry-run-plan.json` with `harbor: false` and does
not invoke `evallab run`.

## Enable token, standing approval, budget, secret

Approval and budget reuse Platform typed controls: `PaidRunAuthorization`
JSON (`spec_id`, `actor`, `authorized_at`; `quota_override` must be false)
plus `load_policy` of `standing-approvals.yaml`. Env presence flags
(`EVAL_LAB_STANDING_APPROVAL`, `EVAL_LAB_BUDGET_PRESENT`) are self-assertion
and are refused. Enable identity, approval `actor`, and budget `actor` must
be pairwise distinct. Parallel signed-manifest extra fields are rejected.
`approval_digest` is an HMAC-SHA256 over canonical policy + budget + scope +
`spec_id`. Key reference and key_id fingerprints are compiled into the
operator (active + previous). Policy `approval_signature_ref` must equal the
pinned ref: Linux `file:/run/secrets/evallab-approval-hmac` (that exact path;
systemd may expose the same basename under `$CREDENTIALS_DIRECTORY` after
fstat regular, no symlink, fingerprint, and fstatvfs read-only mount) or
macOS `keychain:EvalLab/evallab-approval-hmac`. Caller-selected paths, env
key bytes, and unpinned fingerprints are not trust roots. Records carry
`issued_at` / `expires_at` / `nonce` / `key_id` / MAC. Unkeyed SHA-256 of
public fields is not a signature.
Never put token material in git or argv logs. There is no production `--now`
or `EVAL_LAB_OPERATOR_NOW` clock override. A heartbeat timestamp in the
future is `stale_heartbeat`.

| Input | How |
|---|---|
| Enable token | `EVAL_LAB_ENABLE_TOKEN` + `EVAL_LAB_ENABLE_IDENTITY` |
| Standing approval | MAC'd CAS record `kind=approval` with `issued_at`/`expires_at`/`nonce`/`key_id` (caller JSON is not the trust root) |
| Budget | MAC'd CAS record `kind=budget`; `ceiling_usd` > 0 and not above `StandingApprovalsPolicy` |
| MAC key | Compiled ref + key_id; Linux `/run/secrets/evallab-approval-hmac`; never caller path or writable state |
| Recovery | One-time MAC bound to kill digest plus sorted fenced IDs and lease settlement digests; every fenced ID must exist in `leases.json` with terminal evidence |
| Secret reference | `EVAL_LAB_SECRET_REF` closed grammar `keychain:<service>/<account>` (never logged) |
| Secret presence probe | `EVAL_LAB_SECRET_PRESENT=1` or `$STATE/secret_present` (existence only) |
| Policy | `--policy` full typed nested fields; nulls keep DISABLED |

Closed reason codes: `missing_enable_token`, `missing_standing_approval`,
`missing_budget`, `missing_secret`, `stale_heartbeat`, `drain_incomplete`,
`default_disabled`, `same_enable_and_approval_identity`, `billable_refused`,
`recovery_spent`.

Default `validate` with no inputs → `default_disabled`. Incomplete signed
policy fields also keep the operator DISABLED.

## Units (remain disabled)

- launchd: `Disabled=true`, `RunAtLoad=false`, no `KeepAlive`, user-session
  `Aqua`, interpreter `/usr/local/libexec/evallab/.venv/bin/python`,
  `Umask=63` (077). Production paths are installer-rendered absolute
  `$HOME/Library/Application Support/EvalLab` (state) and
  `$HOME/Library/Logs/EvalLab` (logs). The installer precreates those
  directories mode 0700 with no symlink and log files 0600, and does not
  `launchctl bootstrap`. Template tokens stay in the committed plist.
- systemd: `[Install]` has no `WantedBy=`. `DynamicUser=yes`,
  `StateDirectory=evallab-operator`. Optional enablement drop-in:
  `LoadCredential=evallab-approval-hmac:/root-managed/evallab-approval-hmac`
  so disabled `validate` starts without the credential file. The operator
  reads only `$CREDENTIALS_DIRECTORY/evallab-approval-hmac`. Do not
  `systemctl enable`.
- compose: `restart: "no"` and `profiles: ["manual"]`; `read_only: true`;
  no-network root `operator-state-init` uses pinned
  `python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f`
  (not a tag-only image) to chown the named volume to 65532 mode 0700;
  operator `depends_on` `service_completed_successfully`. Docker secret
  `evallab-approval-hmac` uid/gid 65532 mode 0440. tmpfs only `/tmp:mode=0700`.
  Do not `docker compose up`.
- Cloud bootstrap prints the worker protocol and exits `0` without starting a VM.

## Drain vs kill

- `drain` waits on `$STATE/inflight.json`. Any remaining or malformed
  inflight, and any unexecuted kill fence, returns nonzero `drain_incomplete`.
  Empty inflight with no kill fence → mode `DISABLED`.
- `kill` writes `$STATE/kill.json` with `FAILED_OPERATOR_KILL`,
  `executed: false`, and **does not wipe** inflight leases.
- Mode transitions are centralized. `KILLED` is entered only after the kill
  action writes its audit (`kill.json` with `executed: false`). `DRAINING` and
  `KILLED` cannot be overwritten by `pause`, `restart`, `maintenance`,
  `start`, or `validate`. `drain` reconciles every inflight lease, marks each
  terminal, then atomically writes empty `inflight.json` and
  `kill.executed=true` when a kill record exists. Only then may a signed
  one-time recovery clear `KILLED`. Replay of a spent nonce is `recovery_spent`.

## Health / CAS rotation

`status`, `validate`, `quota`, and `start` fail closed on a missing, future,
or stale heartbeat once a typed policy is present (`stale_heartbeat`). `rotate-logs` /
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
