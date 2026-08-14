<!-- calibration-variant: correct -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

From 03:18 the scheduler said `0 free worker slots, 64/64 busy, first-attempt
queue not being served`. Those 64 slots were occupied by unlimited 1s retries
started during the 03:11–03:17 gateway 503. The slots never freed themselves.
Drain + backoff at 07:01 ended INC-2307 at 07:03.

## Impact

~3h 48m without SMS/verification codes. Peak lag 13,920s, depth 812,400,
806,114 drained. Support-detected at 06:04.

## Timeline

- **02:47** — v2.8.1 log format; slots not saturated.
- **03:11** — 503s begin.
- **03:13** — 12,904 in retry, holding slots.
- **03:17** — 503s end.
- **03:18** — 64/64 busy.
- **03:20** — db CPU 86% (retry write volume).
- **06:00–06:04** — email then support escalation.
- **06:31** — v2.8.1 diffed, no behaviour change.
- **07:01–07:03** — drain, backoff, recovery.

## Root Cause

Retries and first attempts share 64 slots with no cap, no backoff, no jitter.
Evidence: `workers:` comment in `worker-config.yaml`; startup retry line;
`m-8841207`; `retry_share_pct` 95; repeated `0 free worker slots`. Gateway
fault is the trigger. v2.8.1 and db CPU are not causes.

## Contributing Factors

No slot-saturation alert; email-only depth alert; no DLQ; 17 minutes on the
deploy hypothesis.

## Corrective Actions

1. Cap attempts and add exponential backoff with jitter.
2. Separate retry lane or per-class budget.
3. Page on `worker_slots_busy == 64` and on delivery lag.
4. DLQ.
5. Re-route queue-depth to pager.

## Open Questions

Gateway idempotency on repeated `message_id`; drain fate; gateway RCA.
