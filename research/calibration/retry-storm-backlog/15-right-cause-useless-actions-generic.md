<!-- calibration-variant: right-cause-useless-actions -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

Gateway 503s for six minutes (03:11–03:17) plus unlimited 1s retries on 64
shared slots produced a self-sustaining backlog until the 07:01 drain.
Resolved 07:03.

## Impact

SMS out ~03:15–07:03. Lag 13,920s. Queue 812,400. 806,114 drained. No page.

## Timeline

- **02:47** — v2.8.1; queue healthy.
- **03:11–03:17** — gateway 503.
- **03:18** — 64/64 busy.
- **06:00** — email 812,400.
- **06:04** — INC-2307.
- **07:01** — drain + backoff.
- **07:03** — resolved.

## Root Cause

Unlimited retries, fixed 1000ms, no jitter, shared 64 slots. Evidence:
`worker-config.yaml`, startup line, `m-8841207` attempt=351, `retry_share_pct`
3→95. Trigger is the gateway; cause is the policy. v2.8.1 and db CPU 86%
are not causes.

## Contributing Factors

Email-only 500k alert; no lag/slot alerts; no DLQ.

## Corrective Actions

- Improve monitoring.
- Review retry best practices as a team.
- Increase awareness of metastable failures.
- Update the incident newsletter.
- Schedule a workshop.

## Open Questions

Duplicates; drain fate; gateway RCA; customer count.
