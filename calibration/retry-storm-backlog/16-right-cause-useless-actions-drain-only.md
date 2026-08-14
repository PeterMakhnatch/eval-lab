<!-- calibration-variant: right-cause-useless-actions -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

Unlimited 1s retries on shared slots turned a 03:11–03:17 gateway 503 into a
backlog that lasted until we drained 806,114 messages at 07:01. Resolved
07:03.

## Impact

~3h 48m without SMS, lag 13,920s, queue 812,400.

## Timeline

- **02:47** — v2.8.1 healthy.
- **03:11** — 503s.
- **03:17** — 503s end; retries continue.
- **03:18** — 0 free slots.
- **06:04** — INC-2307.
- **07:01** — drain.
- **07:03** — resolved.

## Root Cause

Retry amplification: unlimited attempts, 1000ms, no backoff, 64 shared slots.
Evidence: config, startup line, attempt counters, `retry_share_pct`. v2.8.1
ruled out.

## Contributing Factors

Late email alert; no DLQ.

## Corrective Actions

1. Keep the drain script in the on-call gist.
2. Add a queue-depth graph to the existing dashboard.
3. Remember to drain sooner next time.
4. Write a status-page template for SMS delays.

## Open Questions

Duplicates; whether drained messages were dropped.
