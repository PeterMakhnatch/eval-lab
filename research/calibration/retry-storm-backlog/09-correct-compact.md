<!-- calibration-variant: correct -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

Six minutes of gateway 503 (03:11–03:17) plus unlimited 1s retries on 64
shared slots produced a self-sustaining backlog until 07:03.

## Impact

SMS out ~03:15–07:03. Lag 13,920s. Queue 812,400. 806,114 drained. No page.

## Timeline

- **02:47** — v2.8.1; queue ~2,100.
- **03:11** — gateway 503 starts.
- **03:17** — gateway recovers; retries do not.
- **03:18** — 0 free slots.
- **06:00** — email at 812,400.
- **06:04** — INC-2307.
- **07:01** — drain + capped backoff.
- **07:03** — resolved.

## Root Cause

Unlimited retries, fixed 1000ms, no jitter, shared 64 slots. Evidence:
`worker-config.yaml`, startup `retry.max_attempts=unlimited`, `m-8841207`
attempt=351, `retry_share_pct` 3→95. Trigger ≠ cause. v2.8.1 ruled out
(format-only, healthy 02:47–03:11). DB CPU 86% is a symptom.

## Contributing Factors

Email alert at 500k; no lag/slot alerts; no DLQ; shared pool.

## Corrective Actions

Bound retries with jitter; DLQ; separate retry budget; page on lag; page
queue-depth at a sane threshold; gateway circuit breaker.

## Open Questions

Duplicates; lost vs delayed; gateway RCA; customer count.
