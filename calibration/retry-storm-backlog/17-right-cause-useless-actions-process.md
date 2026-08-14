<!-- calibration-variant: right-cause-useless-actions -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

The retry policy (`unlimited` / 1000ms / no jitter / shared 64 slots) turned
a six-minute gateway fault into a four-hour outage. Drain at 07:01. Resolved
07:03.

## Impact

SMS missing ~3h 48m, 812,400 peak, 806,114 drained, support-detected.

## Timeline

- **02:47** — v2.8.1.
- **03:11–03:17** — gateway 503.
- **03:18** — slots full.
- **06:04** — INC-2307.
- **07:03** — resolved.

## Root Cause

Unbounded non-isolated retries. Evidence: `worker-config.yaml`, startup
`retry.max_attempts=unlimited`, `m-8841207`, `retry_share_pct` 95. Not
v2.8.1. Not db CPU.

## Contributing Factors

Detection and comms process, not just config.

## Corrective Actions

1. Hold a blameless conversation about paging culture.
2. Add INC-2307 to the reliability reading list.
3. Ask people to be more careful with retries.
4. Put "think about amplification" on the design-review checklist as prose
   with no owner or done-when.

## Open Questions

Customer count; gateway RCA.
