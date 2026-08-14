<!-- calibration-variant: subtly-wrong-cause -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

`notifications-db` CPU hit 86% at 03:20 and stayed elevated, slowing
notify-worker enough that the outbound queue grew to 812,400. We drained at
07:01. The gateway's six-minute 503 was a sideshow.

## Impact

SMS missing ~3h 48m. Peak lag 13,920s. Queue 812,400. 806,114 drained. No
page.

## Timeline

- **02:47** — v2.8.1, unrelated.
- **03:11** — gateway 503s start.
- **03:17** — gateway recovers.
- **03:20** — notifications-db CPU warning 86%.
- **06:00** — queue-depth email.
- **06:04** — INC-2307.
- **07:01** — drain.
- **07:03** — resolved.

## Root Cause

Database CPU saturation. At 86% the worker could not commit delivery state
fast enough, so messages piled up and were retried. The retry counters look
dramatic but they are a consequence of the database not keeping up. 86% is
below the 95% page, which is why nobody came.

v2.8.1 is not the cause (log format only). The gateway window is too short to
explain a four-hour queue.

## Contributing Factors

- DB page threshold 95% is too high.
- No read-replica for worker state writes.
- Queue-depth alert is email-only.

## Corrective Actions

1. Scale notifications-db and lower the CPU page to 70%.
2. Move retry state off the primary.
3. Keep the drain runbook.
4. Lower `notify-queue-depth` to 50,000.

## Open Questions

- Which query burned CPU? Not in the evidence.
- Should we replace notifications-db with Redis?
