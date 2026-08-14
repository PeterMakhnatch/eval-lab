<!-- calibration-variant: style-only-fluent -->
# Postmortem: Service Availability Incident

## Summary

Our service experienced a period of degraded availability. Users encountered
errors when attempting to complete their requests. The engineering team was
paged, investigated the issue, applied a mitigation, and service was restored
to normal levels. This document captures what happened and what we intend to do
about it so that the same class of problem does not recur.

## Impact

Users experienced elevated error rates for a period of time. Some requests
failed and had to be retried. The degradation affected a meaningful share of
traffic during the affected window, and customer-facing quality of service was
below our internal targets throughout.

## Timeline

- 14:09 — Alerting fired and the on-call engineer was paged.
- 14:15 — The on-call engineer began investigating and confirmed the issue.
- 14:30 — Additional responders joined and hypotheses were generated.
- 15:00 — The team narrowed down the likely area of the problem.
- 15:31 — A mitigation was applied.
- 15:38 — Metrics returned to normal and the incident was closed.

## Root Cause

The incident was caused by a change that interacted badly with an existing
constraint in the system. Because the constraint was not visible at the time
the change was made, the interaction was not anticipated. When the change
reached production, the system entered a state it was not designed to handle
and began returning errors to users. Once the change was reverted, the system
returned to its previous stable behaviour, confirming that the change was
implicated in the failure.

## Contributing Factors

- Insufficient monitoring of the affected subsystem.
- Limited automated testing coverage for this class of change.
- Deployment practices that did not surface the problem before full rollout.
- Time lost to an initial hypothesis that turned out to be incorrect.

## Corrective Actions

- Improve monitoring and alerting across the platform.
- Increase test coverage for configuration changes.
- Review the deployment process and adopt safer rollout practices.
- Update the incident response runbook with lessons learned.
- Schedule a follow-up review with the wider engineering team.

## Open Questions

- What is the full customer impact of this incident?
- Are other systems exposed to the same class of problem?
- Should we revisit our alerting thresholds more broadly?
