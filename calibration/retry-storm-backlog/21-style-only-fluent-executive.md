<!-- calibration-variant: style-only-fluent -->
# Postmortem: Messaging reliability event

## Summary

Customers experienced a prolonged gap in outbound messaging. The response
organization convened, a mitigation was applied, and delivery resumed. This
note is for the staff meeting, not an engineering design review.

## Impact

Messages were late or missing for several hours. Support volume rose.
Financial impact will be estimated separately.

## Timeline

- 03:11 — upstream partner reported trouble.
- 06:04 — support raised the customer impact.
- 07:01 — mitigation applied.
- 07:03 — service restored.

## Root Cause

A partner disruption interacted with our architecture in a way that prevented
self-recovery. The details are less important than the customer promise we
missed.

## Contributing Factors

- Single-vendor concentration.
- Detection that waited on customers.
- Escalation paths that assume a page will fire.

## Corrective Actions

- Brief the exec team on vendor concentration.
- Fund a multi-quarter messaging resilience theme.
- Improve our customer-apology template.
- Revisit SLAs at the next QBR.

## Open Questions

- How should we describe this externally?
- What spend is justified to avoid a repeat?
