Status: done
Last: merged as PR #75 (`ae170e6`)
Next: none
Blockers: none

# Spine Purpose and Quota Policy Handoff (WS-E Item 1)

Implements:
1. `ExperimentSpec.purpose: baseline|comparison|elicitation|drift|calibration|craft|practice` (required)
2. Dispatch-time refusal for purposeless specs
3. `refuse_billable_at_used_percent` policy field in `policy/standing-approvals.yaml`
4. Migration script `scripts/backfill_spec_purpose.py`
5. Test coverage in `tests/test_spec_purpose.py` (30 tests)

## Verification
- `uv run pytest` (784 passed, 1 xfailed)
- `uv run ruff check .` (clean)
