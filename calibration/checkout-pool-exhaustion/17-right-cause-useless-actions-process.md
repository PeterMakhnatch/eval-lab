<!-- calibration-variant: right-cause-useless-actions -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

Release v4.19.0 set workers=32 while `max_connections` stayed 10, so checkout
requests timed out waiting for a connection. Rollback at 15:31 fixed it.
Mitigated 15:38.

## Impact

94 minutes, ~7,000 failed checkouts, 38.1% peak 5xx, complaints from 14:18.

## Timeline

- **14:02** — deploy v4.19.0.
- **14:04** — first pool-exhausted 500.
- **14:09** — page.
- **14:20–14:41** — vendor hypothesis, then discarded (flat ~130ms).
- **15:31** — rollback.
- **15:38** — mitigated.

## Root Cause

Worker count and pool size were not coupled. Evidence: `workers=32
db_pool_max=10` startup line, `TimeoutError: connection pool exhausted
(max=10, waiters=...) after 5000ms`, `metrics.csv` wait p99 4→3104. The
vendor is not the cause.

## Contributing Factors

Process gaps around review, canary, and incident comms.

## Corrective Actions

1. Hold a blameless workshop on communication during incidents.
2. Update the values document to emphasize careful reviews.
3. Ask teams to be more mindful when changing configuration.
4. Add INC-2291 to the quarterly reliability newsletter.

## Open Questions

- Reconciliation numbers are unavailable.
- Whether other services have the same assumption is unknown.
