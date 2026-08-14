<!-- calibration-variant: correct -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

`worker-config.yaml` still has `retry.max_attempts: unlimited`, `delay_ms:
1000`, `backoff: none`, `jitter: none`, and one 64-slot pool for first
attempts and retries. When the gateway returned 503 from 03:11 to 03:17 that
config produced a self-sustaining retry storm. Recovery at 07:03 required a
drain plus a live config change.

## Impact

~3h 48m without SMS. Peak lag 13,920s, peak depth 812,400, 806,114 drained.
Detection via support at 06:04, not a page.

## Timeline

All times UTC 2026-07-22.

- **02:47** — v2.8.1; queue healthy.
- **03:11–03:17** — gateway 503 window (`ticket.md`, logs, `gateway_503`
  only in the 03:20 metrics bucket).
- **03:18** — 64/64 busy, first-attempt queue starved.
- **06:00** — email alert at 812,400.
- **07:01** — drain; `max_attempts=6` backoff=exponential jitter=full.
- **07:03** — resolved.

## Root Cause

The retry policy, not the six-minute outage, explains a 3h52m customer
outage. Evidence: config retry block; startup line; `m-8841207` attempt
cadence; `messages in retry state` 12,904→771,240; `retry_share_pct` to 95.
v2.8.1 is not the cause (log formatting, healthy queue for 24 minutes).
notifications-db CPU 86% is a symptom.

## Contributing Factors

Email-only 500k alert; no lag/slot alerts; `dead_letter_queue: null`; shared
slots.

## Corrective Actions

1. Persist the 07:01 retry bounds in source control.
2. Configure a DLQ with replay.
3. Cap retry concurrency.
4. Page on delivery lag and on worker-slot saturation.
5. Lower and re-route `notify-queue-depth`.

## Open Questions

Duplicates under repeated `message_id`; fate of drained messages; gateway
RCA; customer cardinality.
