<!-- calibration-variant: correct -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

`service-config.yaml` and the v4.19.0 startup line disagree about capacity:
32 HTTP workers, 10 database connections, 5000ms acquire timeout. That
configuration, shipped at 14:02, is why `POST /v1/checkout` returned 500 until
the 15:31 rollback to v4.18.3. INC-2291 was mitigated at 15:38.

## Impact

Checkout was degraded for 94 minutes. Roughly 7,000 requests failed (7,021
`checkout_5xx` in `metrics.csv`). Peak 5xx was 38.1% at 14:45. Successful
requests were still slow because `http_p99_ms` sat at ~5000.

## Timeline

All times UTC, 2026-07-14.

- **14:02** — `deploys.csv`: raise workers 8 → 32. Log: `workers=32 db_pool_max=10`.
- **14:03–14:04** — `db.pool` `active=10 max=10`; first exhausted timeout at 14:04:12.
- **14:09** — page at 18.4% 5xx.
- **14:20–14:41** — vendor advisory opened and closed; vendor p99 stayed ~130ms.
- **14:52–15:12** — instance restarts; no change.
- **15:31** — rollback `v4.18.3` workers=8.
- **15:38** — mitigated.

## Root Cause

The pool was sized for 8 workers and never re-validated when workers became 32.
That is the cause; the deploy is only the trigger. Establishing evidence:
`service-config.yaml` (`max_connections: 10`, `acquire_timeout_ms: 5000`), the
14:02:11 startup line, the `connection pool exhausted (max=10, waiters=...)`
errors, and `metrics.csv` pinning `db_pool_active` at 10 while wait p99 rose
to 3104. Recovery on rollback without a database change confirms it.

Ruled out: the 14:20 payments-vendor advisory (latency flat, 200s, wrong
timing), search-api TLS (not on the checkout path), ledger-db CPU 72% (symptom).

## Contributing Factors

- Detection waited for 5xx > 5% because no pool-saturation alert exists.
- No CI check that `workers` and `max_connections` are consistent.
- Canary disabled.
- Vendor hypothesis delayed looking at the deploy.

## Corrective Actions

1. Startup or CI assertion: refuse boot when workers exceed pool size.
2. Raise `max_connections` for the intended 32-worker concurrency and load-test
   before re-shipping v4.19.0.
3. Alert on pool acquire wait and pool saturation.
4. Enable canary deploys (`deployment.canary: true`).

## Open Questions

- Payment reconciliation for charges without confirmations is unavailable.
- The reason for choosing 32 workers is not in the evidence.
- Whether sibling services have the same uncoupled pool setting.
