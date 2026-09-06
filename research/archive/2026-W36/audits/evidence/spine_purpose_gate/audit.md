# Audit Evidence: Spine Purpose Gate (WS-E Item 1)

## Subject
- **Handoff**: `agents/handoffs/spine-purpose.md`
- **Module**: `src/evallab/schemas.py`, `src/evallab/queue.py`, `scripts/backfill_spec_purpose.py`, `policy/standing-approvals.yaml`
- **Tests**: `tests/test_spec_purpose.py`

## Verified Claims
1. **Required Purpose Field**: `ExperimentSpec.purpose` is strictly validated against the 7-value taxonomy: `baseline | comparison | elicitation | drift | calibration | craft | practice`.
2. **Dispatch-Time Rejection**: `PolicyGate.decide` checks `getattr(spec, "purpose", None)` first, returning `admitted=False`, `reason_code="purposeless_spec"`, and actionable remediation instructions naming all 7 values.
3. **CLI Enforcement**: `evallab submit` rejects purposeless or invalid-purpose specs during Pydantic schema validation before queuing.
4. **Policy Ceiling Field**: `policy/standing-approvals.yaml` includes `refuse_billable_at_used_percent: null`.
5. **Backfill Migration**: `scripts/backfill_spec_purpose.py` runs dry-run by default and derives intent from evidence.
6. **Test Suite**: 30/30 tests in `tests/test_spec_purpose.py` pass.

## Verdict
**CONFIRMED**. The purpose gate is fully implemented, wired in `schemas.py`, enforced in `queue.py` / `PolicyGate`, checked at `evallab submit` CLI, and all 30 tests pass cleanly.
