<!-- calibration-variant: subtly-wrong-cause -->
# Postmortem: INC-2307 — notification delivery backlog

## Summary

The SMS gateway was down. It said 03:11–03:17 but our queue did not recover
until 07:03, so the vendor's window is incomplete. We treated the vendor as
the root cause and drained our side as mitigation.

## Impact

~3h 48m without SMS, 812,400 peak depth, 806,114 drained, no page.

## Timeline

- **02:47** — v2.8.1, healthy afterwards.
- **03:11** — gateway 503s.
- **03:17** — vendor claims recovery.
- **06:04** — customers still have no SMS.
- **06:35** — vendor email restates the six-minute window.
- **07:01** — we drain.
- **07:03** — delivery resumes.

## Root Cause

A sustained gateway outage, not a six-minute blip. Our retry counters and
slot saturation are what a client does when the upstream is still failing,
even if the vendor's status page disagrees. The 07:01 drain worked because it
dropped work the gateway would have continued to reject.

Retry configuration is an implementation detail. v2.8.1 is unrelated.

## Contributing Factors

Single SMS vendor. No secondary gateway. Status-page trust.

## Corrective Actions

1. Replace the SMS gateway vendor.
2. Demand a written RCA for a four-hour outage, not a six-minute one.
3. Add a second provider behind a feature flag.
4. Page when the vendor status page and our delivery lag disagree.

## Open Questions

- Why did the vendor report 03:17 recovery?
- How many customers saw duplicates after we retried?
