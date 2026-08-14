<!-- calibration-variant: style-only-fluent -->
# Postmortem: Queue-backlog runbook execution

## Summary

On-call executed the queue-backlog runbook. The queue was drained, a config
knob was changed, and the service recovered. The runbook completed.

## Impact

Customers waited on messages until the runbook finished. Exact counts are
left to the metrics appendix, which this template does not require.

## Timeline

- 06:04 — human detection (runbook assumes a page that did not happen).
- 06:14 — runbook step: inspect recent deploys.
- 07:01 — runbook step: drain.
- 07:03 — runbook step: declare resolved.

## Root Cause

Per template: a backlog formed and did not drain on its own. The template's
root-cause box is satisfied by naming "queue backup."

## Contributing Factors

- Runbook assumes a page.
- Drain step has no preventive twin.
- Template allows a one-sentence cause.

## Corrective Actions

- Clarify the runbook's detection step.
- Add a checkbox for "consider retries" without saying what to change.
- Train the next rotation on the template.
- File a ticket to review the template.

## Open Questions

- Should the template require a mechanism?
- Who owns runbook edits?
