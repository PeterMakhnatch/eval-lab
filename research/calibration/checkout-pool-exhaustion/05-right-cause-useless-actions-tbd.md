<!-- calibration-variant: right-cause-useless-actions -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

On 2026-07-14, release `checkout-api v4.19.0` raised the service's HTTP worker
count from 8 to 32 without changing the per-instance database connection pool,
which stayed at its long-standing limit of 10 connections. Request concurrency
then exceeded the connections available to serve it, requests queued for a
connection until the 5000ms acquire timeout expired, and `POST /v1/checkout`
returned 500. Errors began at 14:02 UTC, paged at 14:09, and stopped when the
release was rolled back to v4.18.3 at 15:31. The incident was declared
mitigated at 15:38.

## Impact

Customer checkout was broken or degraded for 94 minutes, from the first errors
at 14:04 to mitigation at 15:38.

- Approximately 7,000 `POST /v1/checkout` requests returned 500 across the
  window (summing `checkout_5xx` in `metrics.csv` over the affected buckets
  gives 7,021, against a baseline of 2–3 per five-minute bucket).
- The failure rate over the window was about 29.5%, peaking at 38.1% in the
  14:45 bucket.
- Request latency was pinned at the 5000ms acquire timeout (`http_p99_ms`
  ≈ 5001–5004) for the whole degraded period, so even successful requests were
  slow.
- Customer complaints reached `#support` by 14:18.

Financial impact is not yet quantified; see Open Questions.

## Timeline

All times UTC, 2026-07-14.

- **14:02** — `checkout-api v4.19.0` deployed (`deploys.csv`), described as
  "raise HTTP server worker count from 8 to 32 for throughput". The startup log
  line records the resulting state: `workers=32 db_pool_max=10`.
- **14:03–14:04** — Connection pool saturates. `db.pool` warns with
  `active=10 max=10`, and the first `TimeoutError: connection pool exhausted`
  appears at 14:04:12 (`checkout-api.log`).
- **14:05** — First degraded metric bucket: 5xx rate 6.1%, `db_pool_wait_p99_ms`
  jumps from 4 to 1450, `db_pool_active` pinned at 10 (`metrics.csv`).
- **14:09** — `checkout-api-5xx-rate` pages at 18.4% (`alerts.log`).
- **14:20** — Payments vendor posts an "elevated latency" advisory. Responders
  adopt it as the leading hypothesis (`oncall-chat.txt`).
- **14:23** — Vendor latency observed flat at ~130ms, contradicting that
  hypothesis; the theory is nevertheless retained.
- **14:41** — Vendor advisory clears while our error rate is unchanged, ruling
  the vendor out.
- **14:45** — Peak impact: 38.1% 5xx, `db_pool_wait_p99_ms` 3104.
- **14:52–15:12** — Two instances restarted; no change in behaviour.
- **15:31** — Rollback to `v4.18.3` (`workers=8`) deployed. The startup line at
  15:31:44 confirms the restored configuration.
- **15:33** — Pool acquire back to `wait_ms=3 active=4`, errors collapse.
- **15:38** — Incident declared mitigated; the 15:40 bucket is at baseline
  (0.3% 5xx).

## Root Cause

The database connection pool was sized for 8 workers and the service was
reconfigured to run 32.

`service-config.yaml` sets `database.pool.max_connections: 10` with
`acquire_timeout_ms: 5000`, last changed 2025-11-04 when the service ran 8
workers. `v4.19.0` changed only the worker count, from 8 to 32. With four times
the concurrent request handlers contending for the same 10 connections, the
pool was permanently saturated: every `db.pool` warning during the incident
reports `active=10 max=10`, with the waiter queue growing from 6 at 14:03:58 to
97 at 14:45:29. Requests that could not acquire a connection within 5000ms
returned 500, which is exactly the error the logs carry:
`TimeoutError: connection pool exhausted (max=10, waiters=...) after 5000ms`.

The defect was latent rather than introduced: the pool had always been too
small for a 32-worker configuration, but nothing ran that configuration until
this release. The deploy is the trigger; the uncoupled pool sizing is the cause.
Rolling back to 8 workers restored service within two minutes without touching
the database, which confirms the mechanism.

Three signals present in the evidence are **not** causes:

- **The payments vendor advisory at 14:20.** `vendor_payments_p99_ms` stays
  between 127 and 134ms for the entire window and every logged vendor call
  returns 200. Errors began at 14:04, sixteen minutes before the advisory, and
  continued unchanged after it cleared at 14:41.
- **The search-api TLS deploy at 14:05 and its certificate-expiry notice at
  14:07:30.** A different service, not in the checkout path.
- **The ledger-db CPU warning at 14:24.** CPU peaks at 72% against a 90% paging
  threshold, and rises only after the errors start. It is a downstream effect
  of connection queueing, not a cause.

## Contributing Factors

- **No alert on pool saturation.** `alerts.log` states explicitly that no alert
  is configured on database connection pool saturation or acquire wait.
  Detection depended on the 5xx rate crossing 5%, so the first signal was
  already customer-visible failure.
- **Pool size is not derived from or validated against worker count.** A
  one-line worker change passed review because nothing connects the two
  numbers, in code, in configuration, or in CI.
- **No canary.** `deployment.canary: false` with a rolling strategy put the
  change on all six instances rather than exposing it on one.
- **The vendor hypothesis was held past its own disconnfirmation.** It was
  adopted at 14:20 and retained at 14:26 despite flat vendor latency being
  noted at 14:23, and only dropped at 14:41. That is roughly 20 minutes not
  spent on the deploy.
- **Restarts were tried before rollback.** Restarting instances at 14:52 cannot
  help a configuration-driven saturation and cost a further 20 minutes.

## Corrective Actions

TBD.

## Open Questions

- **Were customers charged without receiving an order?** The payment
  reconciliation report for the window was not available during the incident,
  and the evidence contains no basis for answering this. It is the highest
  priority follow-up.
- **Why was the worker count raised to 32?** No ticket, design note, or
  capacity analysis is supplied, so we cannot tell whether 32 was a considered
  target or an arbitrary increase — which matters for action 2.
- **How many distinct customers were affected?** The metrics are request
  counts; the evidence has no per-customer breakdown, so the ~7,000 failed
  requests cannot be turned into a customer count.
- **What caused the vendor's own 14:20 advisory?** Not needed to explain this
  incident, but unresolved.
- **Do other services share the fixed pool-sizing assumption?** The evidence
  covers only checkout-api; action 6 exists to answer this.
