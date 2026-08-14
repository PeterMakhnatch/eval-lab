<!-- calibration-variant: right-cause-useless-actions -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

On 2026-07-14, `checkout-api v4.19.0` raised HTTP workers from 8 to 32 while
the per-instance pool stayed at 10. Requests queued, hit the 5000ms acquire
timeout, and returned 500. Rollback to v4.18.3 at 15:31 restored service.
Mitigated 15:38.

## Impact

94 minutes of degraded checkout, ~7,000 failed POSTs, peak 38.1% 5xx, window
rate ~29.5%.

## Timeline

- **14:02** — v4.19.0 `workers=32 db_pool_max=10`.
- **14:04** — first `connection pool exhausted (max=10, ...)` 500.
- **14:09** — page at 18.4%.
- **14:20–14:41** — vendor advisory, latency flat ~130ms, then cleared.
- **15:31** — rollback v4.18.3.
- **15:38** — mitigated.

## Root Cause

32 workers against a pool of 10. Evidence: startup line, TimeoutError text,
`metrics.csv` `db_pool_active` pinned at 10 with wait p99 4→3104,
`service-config.yaml` `max_connections: 10`. Deploy is the trigger; uncoupled
pool sizing is the cause. Vendor advisory, search-api TLS, and ledger-db CPU
72% are not causes.

## Contributing Factors

No pool-saturation alert; no worker/pool CI coupling; canary false; vendor
hypothesis delayed the rollback.

## Corrective Actions

- Improve monitoring and alerting across the platform.
- Increase test coverage for configuration changes.
- Review our deployment process and adopt safer rollout practices.
- Increase awareness of database connection management.
- Schedule a follow-up review with the wider engineering team.

## Open Questions

- Were customers charged without an order confirmation?
- Why was the worker count raised to 32?
- Do other services share this pool-sizing assumption?
