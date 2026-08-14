# Canary suite and drift interpretation

The nightly suite is defined in `policy/canary-suite.yaml`. It has three
members, two agents, and three attempts per agent/job:

| Canary | Local path | Immutable source |
|---|---|---|
| transaction reconciliation | `tasks/transaction-reconciliation` | local task version `0.1.0`, migrated from the frozen source repository |
| Terminal-Bench query optimization | `tasks/query-optimize` | Harbor Hub `terminal-bench/query-optimize@4`, content hash recorded in policy |
| event summary | `tasks/event-summary` | local task version `1.0.0` |

The Terminal-Bench task was obtained with:

```bash
harbor task download terminal-bench/query-optimize@4 --output-dir tasks
```

Revision 4 is an immutable Harbor Hub revision, not the mutable `latest` tag.
The suite also records a deterministic digest over every local task file. If a
task changes without a human updating its version and digest, nightly execution
fails closed before dispatch.

On a healthy night the scheduler submits one job per `(canary, agent)` under
the standing `canary` policy rule. Every job has exactly three attempts. The
six configured job estimates total $15, below the committed $20 daily ceiling;
dispatch still rechecks the live catalog spend before every job.

## Drift semantics

The `canary_drift_observations` SQL view computes the preceding seven calendar
days' mean and sample standard deviation for each `(task, agent)`, excluding
the current observation day. It also carries the task checksum used by Harbor.

The digest marks either of these as a **harness-drift suspect**:

- the task checksum differs from the trailing baseline; or
- reward lies more than one trailing standard deviation from the mean.

The label maps to `harness_failure` in the initial taxonomy and is explicitly
not capability news. A version change, Docker drift, expired credential, or
verifier change must be investigated before comparing model behavior. Small
samples remain small samples; the flag is a triage signal, not a statistical
claim about capability.

The automated test suite runs the full six-job suite on two consecutive dates
with a stub runner and separately injects a task-version perturbation into the
digest. Live validation remains restricted to free Oracle/no-op controls.
