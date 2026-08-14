<!-- calibration-variant: right-cause-useless-actions -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

v4.19.0 raised workers 8→32 against a 10-connection pool. Acquire timeouts at
5000ms produced checkout 500s from 14:04 until the 15:31 rollback. Mitigated
15:38.

## Impact

94 minutes, ~7,021 failed POSTs, peak 38.1% 5xx.

## Timeline

- **14:02** — v4.19.0 workers=32 db_pool_max=10.
- **14:04** — pool exhausted.
- **14:09** — page 18.4%.
- **14:20** — vendor advisory (ruled out: p99 127–134ms).
- **15:31** — rollback v4.18.3.
- **15:38** — mitigated.

## Root Cause

Uncoupled pool sizing: 32 workers, 10 connections, 5000ms acquire timeout.
Startup line, TimeoutError, `db_pool_active=10`, `service-config.yaml`. Vendor
advisory is not the cause.

## Contributing Factors

No pool alert; canary off; vendor hypothesis cost time.

## Corrective Actions

1. Keep checkout-api pinned to v4.18.3 indefinitely.
2. Add a dashboard panel for 5xx so the next incident is easier to watch.
3. Remind on-call to consider rollback sooner.
4. Write a Slack postmortem announcement.

## Open Questions

- Charge-without-confirm status unknown.
- Why workers were raised to 32 is unknown.
