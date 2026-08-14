<!-- calibration-variant: style-only-fluent -->
# Postmortem: Follow-the-runbook incident

## Summary

The on-call engineer followed the standard availability runbook. Alerts
fired, dashboards were checked, a mitigation from the playbook was applied,
and the service recovered. The runbook worked as written.

## Impact

Users saw errors until the playbook mitigation landed. Duration and volume
were within the range the runbook treats as a Sev-2.

## Timeline

- 14:09 — page received; acknowledge within SLA.
- 14:15 — runbook step 1: confirm symptom on the golden dashboard.
- 14:30 — runbook step 2: gather responders.
- 15:00 — runbook step 3: choose mitigation.
- 15:31 — runbook step 4: apply mitigation.
- 15:38 — runbook step 5: declare mitigated.

## Root Cause

Per the runbook, a recent change interacted with an existing limit. The
runbook does not require naming the limit when the mitigation is a rollback.

## Contributing Factors

- Runbook could be shorter.
- New responders needed coaching on the template.
- The template's "root cause" box is easy to fill with generalities.

## Corrective Actions

- Tighten the runbook wording.
- Add a screenshot of the golden dashboard to the template.
- Train the next on-call cohort on the template.
- File a ticket to review the template next quarter.

## Open Questions

- Should Sev-2s require a named mechanism?
- Is the template too flexible?
