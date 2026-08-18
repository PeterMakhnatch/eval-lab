Status: building
Last: Cycle 2 complete: quota accounting property tests & UTC rollover normalization fix
Next: Cycle 3: authoring proposal state machine property tests
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

---

## Cycle 2: Quota Accounting State Machine Property Tests

### Scope & Invariants Tested
- **UTC-Day Rollover & Bucketing**: timestamps across arbitrary global timezones are normalized to UTC; `TrialConsumption.day` and `ConsumptionLedger.by_day()` strictly bucket by UTC calendar dates.
- **Non-negativity & Conservation**: token counts, costs, and trial counts are non-negative; subset partitionings (`by_day`, `by_agent`, `by_task`, `by_job`, `by_policy_rule`) exactly conserve total sums.
- **Headroom & Rate Limits**: complementary window percentages sum to 100%; multi-observation selection consistently picks the latest observation.
- **Stateful Reserve/Release Tracking**: `QuotaReserveReleaseStateMachine` fuzzes attempt reservations, completion releases, failed attempt retentions, and UTC-day calendar transitions.
- **Ceiling Enforcement**: `PolicyGate.decide` strictly enforces daily spend ceilings and returns `daily_cost_ceiling` refusal.

### Hypothesis Findings & Counterexamples
- **Fuzz-found bug**: `TrialConsumption.day` returned `started_at.date()` directly, which yielded local calendar dates when `started_at` was constructed with an aware timezone. Shrunk counterexample `started_at=datetime(2026, 8, 15, 1, 30, tzinfo=ZoneInfo('Etc/GMT-3'))` yielded `date(2026, 8, 15)` instead of UTC date `date(2026, 8, 14)`.
- **Fix applied**: `src/evallab/quota.py` updated in commit `26875d7` (`fuzz-fix: normalize TrialConsumption.day to UTC when started_at is timezone-aware`, +7 lines) to convert `started_at.astimezone(UTC).date()`.
- **Regression test**: `test_regression_trial_consumption_day_timezone_shrunk_counterexample` added to `tests/test_quota_properties.py`.

### Evidence & Pytest Output
Suite runtime:
```
tests/test_quota_properties.py: 9 passed in 1.69s
```
Full test suite summary:
```
1284 passed, 1 skipped, 1 xfailed in 51.21s
```
Premerge check:
```
premerge green: Python 3.12; ty 28 <= 28
```
