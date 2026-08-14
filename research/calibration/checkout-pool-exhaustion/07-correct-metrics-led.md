<!-- calibration-variant: correct -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

INC-2291 is a configuration mismatch made visible by `checkout-api v4.19.0`.
The 14:02 deploy raised HTTP workers from 8 to 32. The per-instance pool stayed
at `max_connections: 10`. Metrics then show `db_pool_active` pinned at 10 and
`db_pool_wait_p99_ms` climbing from 4 to 3104 until rollback to `v4.18.3` at
15:31. Mitigation was declared at 15:38.

## Impact

- Window 14:04–15:38 (94 minutes).
- About 7,021 failed `POST /v1/checkout` requests (`metrics.csv` `checkout_5xx`
  sum over the degraded buckets).
- Peak 5xx rate 38.1% in the 14:45 bucket; window failure rate about 29.5%.
- `http_p99_ms` sat on the 5000ms acquire timeout for the degraded period.

## Timeline

- **14:02** — v4.19.0 deploy; startup line `workers=32 db_pool_max=10`.
- **14:04** — first `TimeoutError: connection pool exhausted (max=10, waiters=41)`.
- **14:05** — 5xx 6.1%, `db_pool_wait_p99_ms` 1450, `db_pool_active` 10.
- **14:09** — `checkout-api-5xx-rate` pages at 18.4%.
- **14:20** — vendor advisory adopted as a hypothesis.
- **14:41** — advisory clears; our 5xx curve does not.
- **14:45** — peak: 38.1% 5xx, wait p99 3104.
- **15:31** — rollback to v4.18.3 (`workers=8`).
- **15:38** — incident mitigated.

## Root Cause

Workers=32 against a pool of 10 is the mechanism. `service-config.yaml` still
has `database.pool.max_connections: 10` and `acquire_timeout_ms: 5000` from
2025-11-04, when the service ran 8 workers. The v4.19.0 startup line and every
`db.pool` warning (`active=10 max=10`) plus the `TimeoutError` text establish
that requests queued for a connection and died at 5000ms. Rollback restoring
8 workers cleared the waiters without touching the database.

The payments-vendor advisory is not the cause: `vendor_payments_p99_ms` stayed
127–134ms, vendor calls returned 200, errors started at 14:04 (before 14:20)
and continued after 14:41. search-api TLS at 14:05 is a different service.
ledger-db CPU at 72% is below the 90% page and rises after the errors.

## Contributing Factors

- No alert on pool saturation or acquire wait (`alerts.log`).
- Pool size is not derived from worker count, so a one-line worker change
  passed review.
- `deployment.canary: false` rolled the change to all six instances.
- The vendor hypothesis was held from 14:20 to 14:41 despite flat vendor
  latency noted at 14:23.

## Corrective Actions

1. Couple `database.pool.max_connections` to `server.workers`, or fail startup
   when `workers > max_connections`.
2. Re-size the pool for 32 workers and re-release v4.19.0 under load.
3. Page on `db_pool_active >= db_pool_max` for 5 minutes and warn on
   `db_pool_wait_p99_ms` above 250ms.
4. Set `deployment.canary: true` for checkout-api.

## Open Questions

- Were any customers charged without an order confirmation? Reconciliation is
  not in the supplied evidence.
- Why was the worker count raised to 32? No ticket is supplied.
- Do other services share the fixed pool-sizing assumption?
