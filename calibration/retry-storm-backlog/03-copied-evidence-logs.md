<!-- calibration-variant: copied-evidence -->
# Postmortem: INC-2307

## Summary

INC-2307 affected notify-worker on 2026-07-22. Here is what the logs show.


2026-07-22T02:47:19Z INFO  startup: notify-worker v2.8.1 slots=64 retry.max_attempts=unlimited retry.delay_ms=1000 retry.jitter=none
2026-07-22T03:11:04Z WARN  gateway: POST /v1/messages 503 upstream_unavailable attempt=1 msg=m-8841207
2026-07-22T03:11:05Z WARN  gateway: POST /v1/messages 503 upstream_unavailable attempt=2 msg=m-8841207
2026-07-22T03:11:41Z WARN  gateway: POST /v1/messages 503 upstream_unavailable attempt=38 msg=m-8841207
2026-07-22T03:13:26Z WARN  retry: 12,904 messages in retry state, all consuming worker slots
2026-07-22T03:16:58Z WARN  gateway: POST /v1/messages 503 upstream_unavailable attempt=347 msg=m-8841207
2026-07-22T03:17:02Z INFO  gateway: POST /v1/messages 201 accepted attempt=351 msg=m-8841207
2026-07-22T03:18:40Z WARN  scheduler: 0 free worker slots, 64/64 busy, first-attempt queue not being served

## Impact

The metrics for the incident window are below.

bucket_end_utc,queue_depth,delivery_lag_sec,gateway_requests,gateway_503,gateway_2xx,worker_slots_busy,worker_slots_total,retry_share_pct,notifications_db_cpu_pct
2026-07-22T02:50:00Z,2050,11,4180,0,4180,21,64,3,33
2026-07-22T03:00:00Z,2110,12,4240,0,4240,22,64,3,34
2026-07-22T03:10:00Z,2080,12,4205,0,4205,22,64,3,34
2026-07-22T03:20:00Z,61400,214,46120,31240,14880,64,64,68,71
2026-07-22T03:30:00Z,148600,638,92480,0,92480,64,64,88,86
2026-07-22T03:40:00Z,231900,1142,96310,0,96310,64,64,91,88
2026-07-22T03:50:00Z,309400,1688,97020,0,97020,64,64,92,88
2026-07-22T04:00:00Z,382100,2255,97460,0,97460,64,64,93,88
2026-07-22T04:10:00Z,450300,2841,97610,0,97610,64,64,93,87
2026-07-22T04:20:00Z,513800,3444,97530,0,97530,64,64,93,87
2026-07-22T04:30:00Z,572600,4061,97480,0,97480,64,64,94,86

## Timeline


06:04 <d.abara> support says customers stopped getting SMS codes around 03:15
06:06 <d.abara> nobody was paged? checking alerts
06:08 <d.abara> queue-depth alert fired at 06:00 but it only goes to email
06:09 <d.abara> notifications.outbound is at 812k, it normally sits around 2k
06:12 <m.tsai> delivery lag is nearly 4 hours
06:14 <d.abara> notify-worker v2.8.1 went out at 02:47, that has to be it
06:16 <m.tsai> rolling back?
06:20 <d.abara> hold on, the queue was completely healthy from 02:47 to 03:11
06:24 <m.tsai> notifications-db cpu warning fired at 03:20 as well, 86%
06:26 <d.abara> pages at 95 so it never woke anyone, and it came after the queue grew
06:31 <d.abara> diffed v2.8.1 — it is a logging change only, text to JSON. not it
06:35 <m.tsai> gateway emailed back: they served 503s from 03:11 to 03:17
06:37 <m.tsai> six minutes. we have been broken for three hours
06:44 <d.abara> all 64 slots are busy but delivery throughput is basically zero

## Root Cause

The root cause is visible in the logs and the deploy history:

2026-07-19T14:05:00Z,notify-worker,v2.8.0,add per-channel delivery counters,ci-bot
2026-07-21T22:30:00Z,notifications-db,schema-114,add index on outbound_message.created_at,ci-bot
2026-07-22T02:47:00Z,notify-worker,v2.8.1,change log formatting from text to JSON; no behaviour change,ci-bot
2026-07-22T04:10:00Z,billing-api,v6.1.2,update invoice PDF template,ci-bot


2026-07-22T02:47:19Z INFO  startup: notify-worker v2.8.1 slots=64 retry.max_attempts=unlimited retry.delay_ms=1000 retry.jitter=none
2026-07-22T03:11:04Z WARN  gateway: POST /v1/messages 503 upstream_unavailable attempt=1 msg=m-8841207
2026-07-22T03:11:05Z WARN  gateway: POST /v1/messages 503 upstream_unavailable attempt=2 msg=m-8841207
2026-07-22T03:11:41Z WARN  gateway: POST /v1/messages 503 upstream_unavailable attempt=38 msg=m-8841207
2026-07-22T03:13:26Z WARN  retry: 12,904 messages in retry state, all consuming worker slots

## Contributing Factors

06:48 <m.tsai> the workers are all retrying, look at retry_share_pct
06:52 <d.abara> pulling up the worker config now
07:01 <d.abara> draining the retry set and pushing a capped backoff config
07:03 <d.abara> delivery is moving again, calling it resolved
07:09 <m.tsai> first-attempt queue is back under 20k

## Corrective Actions

The following configuration was in effect and should be reviewed:

release: v2.8.1

queue:
  name: notifications.outbound
  dead_letter_queue: null      # not configured
  visibility_timeout_ms: 30000

workers:
  # One slot serves one delivery attempt at a time. First attempts and retries
  # are drawn from the same slot pool; there is no separate retry lane and no
  # per-class concurrency budget.
  slots: 64
  instances: 8

retry:
  # Unchanged since the service was introduced in 2024.
  max_attempts: unlimited
  delay_ms: 1000
  backoff: none
  jitter: none

gateway:
  sms:
    base_url: https://api.sms-gateway.example
    timeout_ms: 4000
    idempotency_key: message_id

## Open Questions


2026-07-22T01:15:00Z INFO  backup-lag: nightly notifications-db backup finished on time
2026-07-22T03:20:00Z WARN  notifications-db-cpu: notifications-db CPU 86% > 80% for 10m (page threshold is 95%)
2026-07-22T04:10:00Z INFO  billing-api-deploy: billing-api v6.1.2 rollout completed
2026-07-22T06:00:00Z WARN  notify-queue-depth: notifications.outbound depth 812400 > 500000 for 10m
2026-07-22T07:12:00Z INFO  notify-queue-depth: resolved
2026-07-22T07:14:00Z INFO  notifications-db-cpu: resolved
