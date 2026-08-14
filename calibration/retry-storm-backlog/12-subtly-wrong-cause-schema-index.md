<!-- calibration-variant: subtly-wrong-cause -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

The notifications-db schema-114 index change from 2026-07-21 22:30 regressed
enqueue writes. Overnight traffic then filled `notifications.outbound` to
812,400. We noticed at 06:04 and drained at 07:01.

## Impact

~3h 48m without SMS, lag 13,920s, queue 812,400, 806,114 drained.

## Timeline

- **2026-07-21 22:30** — schema-114 index.
- **02:47** — v2.8.1 (unrelated log format).
- **03:11–03:17** — gateway 503, coincidental.
- **03:20** — db CPU 86% as the bad index is hammered.
- **06:00** — queue alert email.
- **07:01** — drain.
- **07:03** — resolved.

## Root Cause

schema-114. The new index made enqueue and retry-state updates expensive, so
workers spent their 64 slots waiting on the database instead of delivering.
Retry amplification is what a slow enqueue path looks like, not an
independent cause.

billing-api v6.1.2 at 04:10 is unrelated. v2.8.1 is log format only.

## Contributing Factors

Index change had no EXPLAIN gate. No canary on schema changes.

## Corrective Actions

1. Revert schema-114.
2. Require EXPLAIN ANALYZE on notification-db DDL.
3. Add a canary replica for index builds.
4. Scale the primary as a precaution.

## Open Questions

- What was the query plan before vs after schema-114? Not in the pack.
- Did billing-api also suffer?
