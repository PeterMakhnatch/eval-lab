<!-- calibration-variant: correct -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

The SMS gateway returned 503 for six minutes (03:11–03:17). `notify-worker`
retries with `max_attempts: unlimited`, `delay_ms: 1000`, no backoff, no
jitter, on the same 64 slots as first attempts. That policy turned a
six-minute trigger into a backlog that lasted until 07:03. INC-2307.

## Impact

SMS, including verification codes, missing from ~03:15 to 07:03 (~3h 48m).
Peak lag 13,920s. Peak queue 812,400 vs ~2,100. 806,114 messages drained at
07:01. Nobody paged; support escalated at 06:04.

## Timeline

- **02:47** — v2.8.1 log-format deploy; queue stays ~2,100 until 03:11.
- **03:11** — first `503 upstream_unavailable attempt=1`.
- **03:13** — 12,904 messages in retry state.
- **03:17** — gateway recovers; `m-8841207` accepted on attempt=351.
- **03:18** — `0 free worker slots, 64/64 busy`.
- **03:20** — notifications-db CPU 86% (symptom).
- **06:00** — queue-depth email at 812,400.
- **06:04** — INC-2307 opened.
- **06:14–06:31** — v2.8.1 suspected, then diffed out.
- **07:01** — drain + capped exponential backoff with jitter.
- **07:03** — resolved.

## Root Cause

Unbounded, non-backoff, non-isolated retries. Evidence: `worker-config.yaml`
retry block and shared 64-slot pool; startup
`retry.max_attempts=unlimited retry.delay_ms=1000 retry.jitter=none`;
`m-8841207` climbing to attempt=351; `retry_share_pct` 3→95; scheduler `0
free worker slots`. Gateway 503 is the trigger; the policy is the cause
(~38× longer). v2.8.1 is a log-format change and the queue was healthy
02:47–03:11. DB CPU 86% is downstream of retry volume.

## Contributing Factors

Queue alert is email-only at 500,000; no lag/retry/slot alerts; no DLQ;
shared slot pool; 17 minutes on the deploy hypothesis.

## Corrective Actions

1. Commit `max_attempts=6`, exponential backoff, full jitter.
2. Add a dead-letter queue for exhausted messages.
3. Give retries their own concurrency budget.
4. Page on delivery lag >300s.
5. Route `notify-queue-depth` to the pager and lower the threshold.
6. Circuit-break the gateway client.

## Open Questions

- Duplicate deliveries: `idempotency_key: message_id` behaviour is
  undocumented; `m-8841207` was submitted 351 times.
- Were the 806,114 drained messages re-queued or dropped?
- Gateway's own 03:11–03:17 RCA is not in the evidence.
- Distinct customer count unknown.
