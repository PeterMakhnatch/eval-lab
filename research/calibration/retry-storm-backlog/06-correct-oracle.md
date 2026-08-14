<!-- calibration-variant: correct -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

On 2026-07-22 our SMS gateway returned HTTP 503 for six minutes, from 03:11 to
03:17 UTC. `notify-worker` retries failed deliveries with no attempt cap, a
fixed one-second delay and no backoff, and retries occupy the same worker slots
as first attempts. The six-minute fault therefore produced a retry population
large enough to fill all 64 slots, and it stayed full after the gateway
recovered: the backlog sustained itself for nearly four hours. Delivery resumed
at 07:03 only after an operator drained the retry set and applied capped
exponential backoff with jitter.

## Impact

Customers did not receive SMS notifications, including verification codes, from
roughly 03:15 until 07:03 — about 3 hours 48 minutes.

- Peak delivery lag reached 13,920 seconds, or 3 hours 52 minutes
  (`metrics.csv`, 07:00 bucket).
- Queue depth peaked at 812,400 against a normal depth of about 2,100, roughly
  390 times baseline.
- 806,114 messages were still in the retry set when the operator drained it at
  07:01 (`notify-worker.log`).
- Throughput to customers was effectively zero for the duration: all 64 worker
  slots were busy and `retry_share_pct` reached 95, so the slots were serving
  retries rather than new deliveries.
- Nobody was paged. The incident was detected by a customer-support escalation
  at 06:04, 2 hours 53 minutes after onset.

## Timeline

All times UTC, 2026-07-22.

- **02:47** — `notify-worker v2.8.1` deployed (`deploys.csv`), a log-formatting
  change. The queue stays healthy at about 2,100 for the next 24 minutes
  (`metrics.csv`), so this deploy did not start the incident.
- **03:11** — SMS gateway begins returning 503. The first
  `503 upstream_unavailable attempt=1` appears at 03:11:04
  (`notify-worker.log`); the vendor later confirmed a 03:11–03:17 fault
  (`ticket.md`).
- **03:11–03:17** — Retries accumulate at one attempt per second per message.
  Message `m-8841207` reaches `attempt=347` by 03:16:58.
- **03:13** — 12,904 messages are in retry state, all holding worker slots.
- **03:17** — Gateway recovers; `m-8841207` is accepted on `attempt=351`. No
  further 503s appear in `metrics.csv` after the 03:20 bucket.
- **03:18** — `0 free worker slots, 64/64 busy, first-attempt queue not being
  served`. The backlog is now self-sustaining despite the upstream being
  healthy.
- **03:20** — `notifications-db-cpu` warns at 86%, below its 95% page
  threshold (`alerts.log`).
- **06:00** — `notify-queue-depth` fires at 812,400 — but it is routed to
  email, not to the pager.
- **06:04** — Customer-support escalation opens INC-2307.
- **06:14–06:31** — The v2.8.1 deploy is suspected, then ruled out by diffing
  it (`oncall-chat.txt`).
- **06:35** — The gateway confirms its six-minute 503 window by email.
- **07:01** — Operator drains 806,114 retrying messages and applies
  `retry.max_attempts=6`, `backoff=exponential`, `jitter=full`.
- **07:03** — Delivery resumes; incident resolved.
- **07:09** — First-attempt queue back under 20,000.

## Root Cause

An unbounded, non-backoff, non-isolated retry policy turned a six-minute
upstream fault into a three-hour-fifty-two-minute outage.

`worker-config.yaml` sets `retry.max_attempts: unlimited`, `delay_ms: 1000`,
`backoff: none` and `jitter: none`, unchanged since the service was introduced
in 2024, and its `workers:` block notes that first attempts and retries are
drawn from the same 64-slot pool with no separate retry lane and no per-class
concurrency budget. The v2.8.1 startup line records the same values in
production: `retry.max_attempts=unlimited retry.delay_ms=1000
retry.jitter=none`.

When the gateway began failing at 03:11, every in-flight message started
retrying once per second and held its slot for each attempt. `m-8841207`
climbing from `attempt=1` to `attempt=351` shows the cadence directly. The
retry population reached 12,904 within two minutes and 61,388 by 03:22, which
is more than enough to occupy 64 slots permanently. From 03:18 onward the
scheduler reports `0 free worker slots ... first-attempt queue not being
served`, and `retry_share_pct` in `metrics.csv` climbs from 3 to 95.

The important point is that this did not end when the gateway recovered at
03:17. Retries plus newly arriving first attempts exceeded delivery capacity,
so the system had no path back to its healthy state on its own — a metastable
failure. The gateway's six minutes are the **trigger**; the retry policy is the
**cause**, and it is what explains why the outage lasted roughly 38 times
longer than the fault that started it. Recovery required deliberately removing
the retry population, which is exactly what the 07:01 drain and backoff change
did.

Two other signals present in the evidence are **not** causes:

- **`notify-worker v2.8.1`, deployed at 02:47.** `deploys.csv` describes it as
  a change of log formatting from text to JSON with no behaviour change, the
  responders diffed it and confirmed that at 06:31, and queue depth and
  delivery lag were at baseline for the 24 minutes between the deploy and the
  gateway fault.
- **The `notifications-db` CPU warning at 03:20.** It peaks at 86% against a
  95% paging threshold, appears only after retry volume rises, and resolves
  before the incident does. It is a downstream effect of retry traffic, not a
  cause.

## Contributing Factors

- **The only queue alert cannot wake anyone.** `notify-queue-depth` is routed
  to email rather than the pager, so the 06:00 firing changed nothing.
- **Its threshold is far above any healthy value.** 500,000 against a normal
  depth of about 2,100 means the alert can only fire once the incident is
  already hours old.
- **No alert on the signals that moved first.** Delivery lag, retry share and
  worker slot saturation all departed from baseline within minutes of 03:11,
  and none of them is alerted on (`alerts.log`).
- **No dead-letter queue.** `dead_letter_queue: null` means nothing ever left
  the retry set on its own; the population could only grow.
- **Retries and first attempts share one slot pool.** With no per-class budget,
  retry traffic starved new deliveries instead of degrading alongside them.
- **The deploy hypothesis cost 17 minutes**, from 06:14 to 06:31, in an
  incident that had already run for three hours.

## Corrective Actions

1. **Put the 07:01 retry configuration into source control.** The capped
   exponential backoff with jitter and `max_attempts=6` was applied live during
   the incident. Done when the values are in the service's committed
   configuration and a deploy cannot silently restore the old policy.
2. **Add a dead-letter queue for `notifications.outbound`.** Messages that
   exhaust their attempts must leave the retry path. Done when
   `dead_letter_queue` is configured, alarmed on non-zero depth, and has a
   documented replay procedure.
3. **Give retries their own concurrency budget.** Cap retry traffic at a
   fraction of the 64 slots (or run a separate retry lane) so first attempts
   are always served. Done when a synthetic upstream failure leaves
   first-attempt throughput measurably intact.
4. **Page on delivery lag.** Add a paging alert on delivery lag above 300
   seconds for 5 minutes, which would have fired at approximately 03:25 in this
   incident rather than never. Done when the alert fires against a replay of
   this incident's metrics.
5. **Route `notify-queue-depth` to the pager and lower its threshold.** 500,000
   is roughly 240x normal depth. Done when the alert pages and its threshold is
   set from observed healthy depth rather than from the 2024 default.
6. **Add a circuit breaker to the gateway client.** When the gateway fails
   persistently, shed and defer load rather than accumulating retries against
   it. Done when a simulated sustained 503 keeps worker slot utilisation below
   saturation.

## Open Questions

- **How many recipients received duplicate messages?** `gateway.sms` sets
  `idempotency_key: message_id`, but nothing in the supplied evidence documents
  what the gateway does with a repeated key, and `m-8841207` alone was
  submitted 351 times. This is the highest priority follow-up.
- **Were messages lost as well as delayed?** The 07:01 drain removed 806,114
  messages from the retry set, and the evidence does not say whether they were
  re-queued, delivered, or dropped.
- **What caused the gateway's 03:11–03:17 fault?** The vendor confirmed the
  window but has not supplied a root-cause report.
- **How many distinct customers were affected?** The metrics are message
  counts, and the evidence contains no per-customer breakdown.
