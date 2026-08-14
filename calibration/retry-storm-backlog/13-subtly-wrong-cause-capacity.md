<!-- calibration-variant: subtly-wrong-cause -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

64 worker slots are not enough. After a brief gateway 503 the queue grew
because we lacked instances. Adding retries on those same 64 slots made the
shortage obvious. Drain at 07:01 plus the later backoff change recovered us.

## Impact

SMS out ~3h 48m, lag 13,920s, queue 812,400.

## Timeline

- **02:47** — v2.8.1.
- **03:11** — 503s.
- **03:17** — 503s end; 64 slots remain busy.
- **06:04** — INC-2307.
- **07:01** — drain.
- **07:03** — resolved.

## Root Cause

Insufficient worker capacity. 64 slots cannot absorb a burst. The config's
unlimited retries are a secondary annoyance; the primary miss is that we did
not autoscale the worker fleet when `worker_slots_busy` hit 64.

v2.8.1 is not the cause. The gateway recovered in six minutes.

## Contributing Factors

No autoscaling. Queue alert too late. No overflow region.

## Corrective Actions

1. Autoscale notify-worker to 256 slots.
2. Add a second region.
3. Keep a standing overflow queue cluster.
4. Lower the email threshold.

## Open Questions

- What slot count would have drained the 03:17 leftover without a human?
- Should we move SMS off this worker entirely?
