Status: building
Last: Cycle 1 complete: extended queue property tests covering credential deferral, vanished files, and mid-tick quota
Next: Cycle 2: quota accounting property tests
Blockers: none

# M018 - LOOP-FUZZ Handoff

## Cycle 1: Queue State Machine Property Tests

### Scope & Invariants Tested
- **Conservation**: all non-vanished specs exist in exactly one state across `QUEUE_STATES`.
- **Legal transitions only**: event log contains strictly allowed transitions.
- **No double dispatch**: specs transition to `running` at most once.
- **Credential deferral preserves approved state**: when credentials for an agent are missing during `tick()`, the spec stays in `approved/` and logs `dispatch_deferred` without being moved to `waiting/` or dropped.
- **Vanished file tolerance**: `list_specs()` and `tick()` handle vanished/unlinked files gracefully without crash.
- **Mid-tick quota enforcement**: when cumulative spend reaches the daily cost ceiling mid-tick, subsequent specs are moved to `waiting/` with reason `daily_spend_limit` and never exceed ceiling.
- **Admission respected**: billable specs only execute when explicitly approved.

### Hypothesis Findings & Counterexamples
- `test_property_quota_never_exceeded_mid_tick`: Counterexample `costs=[3.0, 3.0, 3.0]` initially failed on `StandingApprovalsPolicy(auto_run=[])` because Pydantic requires `auto_run` to have `min_length=1`. Test fixture updated with valid `AutoRunRule`. No underlying source defect in `queue.py`.

### Evidence & Pytest Output
Suite runtime:
```
tests/test_queue_properties.py: 4 passed in 2.85s
```
Full test suite summary:
```
1275 passed, 1 skipped, 1 xfailed in 59.42s
```
Premerge check:
```
premerge green: Python 3.12; ty 28 <= 28
```
