<!-- calibration-variant: subtly-wrong-cause -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

On 2026-07-22, release `notify-worker v2.8.1` was deployed at 02:47 UTC and
introduced a regression in the delivery path. Message throughput collapsed
shortly afterwards and the `notifications.outbound` queue grew to 812,400
messages. Customers stopped receiving SMS notifications. The incident was
detected via a support escalation at 06:04 and resolved at 07:03 once the
backlog was drained and configuration was corrected.

## Impact

SMS delivery was broken for roughly 3 hours 48 minutes.

- Peak delivery lag reached 13,920 seconds (3 hours 52 minutes).
- Queue depth peaked at 812,400 against a normal depth of about 2,100.
- 806,114 messages were drained from the retry set at 07:01.
- No engineer was paged; detection came from customer support at 06:04.

## Timeline

- **02:47** — `notify-worker v2.8.1` deployed. This is the change that
  introduced the fault.
- **03:11** — Gateway errors begin to surface as the regression takes effect
  under load.
- **03:17** — Gateway errors stop, but the service does not recover.
- **03:20** — `notifications-db` CPU climbs to 86% as the database struggles
  with the increased write pressure from the new release.
- **06:00** — Queue depth alert fires at 812,400.
- **06:04** — Support escalation opens INC-2307.
- **07:01** — Backlog drained and configuration corrected.
- **07:03** — Delivery resumes; incident resolved.

## Root Cause

The root cause was the `notify-worker v2.8.1` release deployed at 02:47. The
release changed the service's logging subsystem from plain text to structured
JSON. Serialising a JSON log record for every delivery attempt is markedly more
expensive than writing a text line, and that additional per-attempt cost
reduced the effective throughput of each of the 64 worker slots. Once
throughput fell below the arrival rate of new messages, the queue began to grow
and never recovered until the backlog was drained manually.

The database CPU warning at 03:20 corroborates this: the heavier logging path
increased pressure on `notifications-db`, pushing CPU to 86% and further
reducing delivery throughput. The two effects compounded each other.

The gateway's brief 503 window was a coincidental external event during an
already degraded period and did not itself cause the backlog.

## Contributing Factors

- The v2.8.1 release was not load-tested before deployment.
- `notifications-db` was already close to its capacity limits.
- The queue depth alert threshold of 500,000 was too high to catch the problem
  early.
- No canary stage for notify-worker deployments.

## Corrective Actions

1. Roll back `notify-worker` to v2.8.0 and keep plain-text logging until the
   JSON logging path has been benchmarked.
2. Benchmark structured logging under production message rates before
   re-attempting the v2.8.1 release.
3. Scale up `notifications-db` so that it has headroom above 86% CPU, and
   review the schema-114 index added on 2026-07-21.
4. Lower the `notify-queue-depth` alert threshold from 500,000 to 50,000.
5. Introduce a canary stage for notify-worker deploys so that a throughput
   regression is caught on one instance rather than all eight.

## Open Questions

- Exactly how much per-message overhead does JSON logging add? We have not
  measured it.
- Did any messages get lost during the drain at 07:01?
- How many distinct customers were affected?
