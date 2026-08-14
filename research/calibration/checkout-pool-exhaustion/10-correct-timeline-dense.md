<!-- calibration-variant: correct -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

The timeline of INC-2291 is the mechanism: the 14:02 worker-count change
immediately saturates a 10-connection pool, 500s begin at 14:04, a vendor
advisory at 14:20 does not move vendor latency, and rolling back workers at
15:31 restores the pool. Mitigated 15:38.

## Impact

- 14:04 to 15:38: 94 minutes.
- 7,021 `checkout_5xx` vs a 2–3 per-bucket baseline.
- Peak 38.1% at 14:45; ~29.5% across the window.
- `#support` complaints from 14:18.

## Timeline

- **14:02** — `deploys.csv` v4.19.0; log `workers=32 db_pool_max=10`.
- **14:03:58** — `db.pool` wait_ms=812 active=10 max=10 waiters=6.
- **14:04:12** — first `connection pool exhausted (max=10, waiters=41) after 5000ms`.
- **14:05** — 6.1% 5xx, wait p99 1450.
- **14:09** — page 18.4%.
- **14:20** — vendor advisory; on-call adopts it.
- **14:23** — vendor p99 still ~130ms.
- **14:41** — advisory clears; our errors do not.
- **14:45** — 38.1% 5xx, wait p99 3104, waiters=97.
- **14:52** — restarts; no change.
- **15:31** — v4.18.3 workers=8.
- **15:33** — wait_ms=3 active=4.
- **15:38** — mitigated.

## Root Cause

32 workers / 10 connections / 5000ms acquire timeout. The deploy triggered a
latent pool-sizing defect. Evidence: startup line, pool warnings, TimeoutError,
`metrics.csv`, `service-config.yaml`. Not the cause: payments vendor (flat
~130ms, 200s, wrong timing), search-api TLS, ledger-db CPU 72%.

## Contributing Factors

No pool alert; uncoupled config; canary off; vendor hypothesis delayed rollback;
restarts tried before rollback.

## Corrective Actions

1. Startup assertion `workers <= max_connections`.
2. Raise `max_connections` and load-test 32 workers.
3. Alert on pool saturation and acquire wait.
4. Enable canary deploys.
5. Add "what did we deploy?" to the first five minutes of the runbook.

## Open Questions

Reconciliation report missing; reason for 32 workers missing; other services
unexamined; vendor's own 14:20 advisory unexplained (not needed here).
