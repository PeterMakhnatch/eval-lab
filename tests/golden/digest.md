# Eval lab digest — 2026-08-16

Reporting period: 2026-08-15 (local catalog day).

## Automation status

- Quarantined: yes
- Dispatches in this nightly cycle: 0
- Failed readiness checks: docker_reachable
- Zero dispatch enforced: yes
- Catalog readable: yes

## Preflight

What `uv run evallab preflight` printed for this digest: remaining quota per paid provider, the unfinished queue grouped by declared purpose, and power warnings for queued comparisons. Read from Harbor job directories and `queue/` only — no network call, no subprocess, no paid call.

- Providers refusing billable work: none
- Providers with no readable allowance: claude-code ([unavailable] is not 'plenty left')
- Providers whose exhaustion is a lockout, not a charge: codex
- Unfinished specs: 6
- Power warnings: 1

```text
evallab preflight — is it safe and sensible to run right now
generated_at: 2026-08-16T12:00:00+00:00
repository:   <TMP>
quota roots:  <TMP>/runs, <TMP>/research/evidence/runs

PER-PROVIDER REMAINING QUOTA (scope: account, NOT the lab; provider-reported)

claude-code
  remaining allowance      UNKNOWN [unavailable]
    reason: no paid trial in the scanned job directories recorded a provider quota snapshot, so the remaining allowance is unknown
    UNKNOWN is not 'plenty left'. This says the allowance could not be measured, not that a run fits inside it. Check the provider yourself before authorising anything billable.
  paid trials seen         0 [observed]
  quota snapshots          0 [observed]

codex
  used_percent             92.0 [observed]
  remaining_percent        8.0 [observed] (account-wide, whole percentage points)
  window                   10080 minutes (168h00m)
  resets_at                2026-08-20T18:32:49+00:00
  observed_at              2026-08-16T10:00:00+00:00
  staleness                2h00m old
  credits_balance          0
  hard stop                True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  plan_type / limit_id     prolite / codex
  lab's share of that      [unavailable]
  quota snapshots          1 [observed]
  paid trials seen         1 [observed]

  lab refusal ceiling      90.0 percent used (reason code subscription_quota_ceiling)
    A lab ceiling is a spend decision and is recorded under its own reason code, never as the provider's statement.

QUEUE BY PURPOSE (states: proposed, pending, approved, waiting, running)
  purpose baseline: 1 spec(s), 1 billable
    [approved] base-a — task canary/event-summary, agent codex, 1 attempt(s)
  purpose comparison: 3 spec(s), 3 billable
    [approved] cmp-alpha — task t/alpha, agent codex, 4 attempt(s)
    [approved] cmp-bravo — task t/bravo, agent codex, 4 attempt(s)
    [approved] cmp-charlie — task t/charlie, agent codex, 4 attempt(s)
  purpose drift: 1 spec(s), 1 billable
    [waiting] canary-run — task canary/event-summary, agent codex, 1 attempt(s)
  purpose practice: 1 spec(s), 0 billable
    [proposed] oracle-control — task library/tasks/event-summary, agent oracle, 1 attempt(s)

POWER WARNINGS (queued comparisons only)
  3 queued comparison spec(s) across 3 distinct task(s)
  n_tasks 3, k 4, baseline 0.500
  WARNING: no per-attempt difference is detectable at n_tasks=3, k=4, baseline=0.500: this comparison cannot reach an interval at its declared attempt count
  The queue carries no field linking two specs into one comparison, so every queued comparison spec is pooled into a single cohort here. Pooling can only overstate n_tasks, so a warning raised at the pooled n holds for every finer partition of these specs; a clean bill of health at the pooled n does not.

VERDICT: nothing in these readings refuses, but codex has no overflow credits, so exhausting the window is a lockout until it resets, not an extra charge
```

## Completed trials

One row per job: a job that ran several trials shows the trial count and every recorded reward, because 1/1/0 across three trials is not the same fact as 1/1/1.

| job | task | agent | trials | rewards | exceptions | policy |
|---|---|---|---:|---|---|---|
| event-summary-oracle | library/tasks/event-summary | oracle | 1 | 1 |  | unattributed |
| canary-event-summary-codex | canary/event-summary | codex | 1 | 0 |  | canary |
| broken-container | canary/event-summary | codex | 1 | unscored | EnvironmentError | unattributed |

Lab self-tests (job name starting `smoke-`, produced by `evallab smoke`) are summarised here instead of listed. Any self-test that raised is a row in the table above, and every other run — including every `oracle` and `nop` control — is always listed. Spend and the exception taxonomy below count these trials.

- 1 self-test trial — library/tasks/event-summary / oracle, reward 1, 0 exceptions (latest: smoke-oracle-fixture)

## Early-morning automation

Completed after the reporting-period cutoff:

One row per job: a job that ran several trials shows the trial count and every recorded reward, because 1/1/0 across three trials is not the same fact as 1/1/1.

| job | task | agent | trials | rewards | exceptions | policy |
|---|---|---|---:|---|---|---|
| early-oracle | library/tasks/event-summary | oracle | 1 | 1 |  | unattributed |

## Canary drift

One row per canary per catalog day. Two rows for the same canary are two days of that canary, not two verdicts about one day.

| day | task | version | agent | reward | 7-day mean ± σ | n | assessment |
|---|---|---|---|---:|---:|---:|---|
| 2026-08-15 | canary/event-summary | 1.0.0 | codex | 1 | 1.000 ± 0.000 | 6 | within baseline |
| 2026-08-16 | canary/transaction-reconciliation | 1.1.0 | codex | 0 | 1.000 ± 0.000 | 6 | harness-drift suspect (task_version_changed); not capability news |

## Cost and failures

- Recorded spend: $0.4200 / $20.00 daily ceiling
- Exceptions by taxonomy: harness_failure=2

## Queue

- Depth: proposed=1, pending=0, approved=4, waiting=1, running=0, done=0, failed=0

| proposal | experiment | reason |
|---|---|---|
| 01GOLDENWAIT00000000000000 | canary-run | daily_cost_ceiling |

## Evidence and calibration

- Run corpus: 1013 bytes (baseline unavailable)
- Judge calibration: no judge is calibrated — no measured record under `research/calibration/records/`.
- Canary observations in report: 2

## Discoveries awaiting verdict

- [**D-20260815-KTXJSHGZ**](DISCOVERIES.md#d-20260815-ktxjshgz) (`draft`) — Across this small control-only cohort, event-summary and transaction-reconciliation showed the expected pattern.

## Queue events

A run of consecutive events identical in event, job, and policy/reason collapses to one row carrying its repeat count and time range. Every event that differs from the one before it is listed on its own line, verbatim.

| time | event | job | policy/reason |
|---|---|---|---|
| 2026-08-15T18:00:00+00:00 – 2026-08-15T18:00:02+00:00 | tick_idle ×3 |  |  |
| 2026-08-15T19:00:00+00:00 | dispatch_started | canary-event-summary-codex | canary |
| 2026-08-16T11:30:00+00:00 | nightly_quarantined |  | headless_doctor_failed:docker_reachable |

## Storm alarms

- Status: quiet (no reason_code storm detected in 1h window)

<!-- run-bytes: 1013 -->
