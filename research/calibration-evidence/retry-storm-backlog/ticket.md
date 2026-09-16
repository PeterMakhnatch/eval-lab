# INC-2307 — notification delivery backlog

- Severity: SEV-2
- Opened: 2026-07-22 06:04 UTC (manually, after a customer-support escalation)
- Resolved: 2026-07-22 07:03 UTC
- Status: resolved, postmortem not yet written
- Incident commander: d.abara
- Responders: d.abara, m.tsai
- Affected service: notify-worker (queue `notifications.outbound`, SMS channel)

## Raw notes captured during the incident

```
06:04  support escalation: customers not receiving SMS codes since ~03:15
06:09  queue notifications.outbound is at 812k, normally about 2k
06:14  notify-worker v2.8.1 shipped at 02:47, suspect the deploy
06:31  diffed v2.8.1, it only changes log formatting
06:48  workers are all busy but almost nothing is being delivered
07:01  drained the retry backlog and applied a capped-backoff config
07:03  delivery resumed, declaring resolved
```

## Vendor communication

The SMS gateway provider confirmed by email that they served HTTP 503 for a
subset of traffic between 03:11 and 03:17 UTC on 2026-07-22 and that the fault
was resolved on their side at 03:17. They have not supplied a root-cause report
for their own outage.

## Not yet gathered

- How many recipients received the same message more than once. The gateway's
  behaviour when the same message id is submitted repeatedly is not documented
  in anything we hold.
