<!-- calibration-variant: correct -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

v4.19.0 at 14:02 set workers=32 against `db_pool_max=10`. Requests waited for
connections, hit the 5000ms acquire timeout, and returned 500 until rollback to
v4.18.3 at 15:31. Mitigated 15:38.

## Impact

94 minutes of broken checkout, ~7,000 failed POSTs, peak 38.1% 5xx, p99 latency
pinned at the acquire timeout.

## Timeline

- **14:02** — v4.19.0 workers 8→32; startup `workers=32 db_pool_max=10`.
- **14:04** — first pool-exhausted 500.
- **14:09** — 5xx page 18.4%.
- **14:20** — vendor advisory (later ruled out).
- **14:45** — peak 38.1% / wait p99 3104.
- **15:31** — rollback v4.18.3.
- **15:38** — mitigated.

## Root Cause

Uncoupled pool sizing: 32 workers, 10 connections. Cited: startup line,
`TimeoutError: connection pool exhausted (max=10, ...) after 5000ms`,
`metrics.csv` `db_pool_active=10` and `db_pool_wait_p99_ms` 4→3104,
`service-config.yaml` `max_connections: 10`. Vendor advisory is not causal
(`vendor_payments_p99_ms` 127–134ms; errors 14:04–after 14:41). search-api TLS
and ledger-db CPU 72% are not causes.

## Contributing Factors

No pool-saturation alert; no worker/pool CI check; canary false; vendor
hypothesis held ~20 minutes after flat vendor latency was observed.

## Corrective Actions

1. Derive pool size from worker count or fail startup on mismatch.
2. Re-size pool and load-test before re-releasing 32 workers.
3. Alert on `db_pool_wait_p99_ms` and pool saturation.
4. Turn on canary for checkout-api.

## Open Questions

Customer charge-without-confirm status unknown; why workers=32 unknown; other
services not audited in this evidence.
