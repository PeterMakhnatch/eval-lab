Status: done
Last: merged as PR #118 (`28d54a2`)
Next: none
Blockers: none

# M018 - LOOP-FUZZ Handoff

## Mission Summary
Completed all 5 state machine property-testing backlog items across 5 cycles.
Fuzzed invariants across Queue, Quota Accounting, Authoring Proposal State Machine, Parquet Compaction, and Attach / Catalog Rebuild.
All property suites run <=60s individually (and 34.25s collectively).
Full test suite is green (1294 passed), ruff clean, ty diagnostics at baseline (28 <= 28), premerge passes cleanly.

---

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

---

## Cycle 3: Authoring Proposal State Machine Property Tests

### Scope & Invariants Tested
- **State Transition DAG**: `AuthoringProposalStateMachine` validates strict transitions: `proposed -> (battery_passed -> (craft_reviewed | rejected) | rejected)`.
- **No Skip Invariants**: Attempting to review un-batteried proposals strictly raises `AuthoringError`. Attempting to review rejected proposals strictly raises `AuthoringError`.
- **Hard Registration Gate**: `pipeline.register()` strictly raises `RegisterRefusal` across all proposal stages and seed classes. Automation cannot write `registered` records to the qualification ledger.
- **Ledger Invariants**: `write_ledger` and `load_ledger` preserve record schemas, maintain strict ordering by `proposal_id`, and correctly normalize null evidence paths.

### Hypothesis Findings & Counterexamples
- No source defects in `authoring.py`. Transition constraints, ledger consistency, and registration refusal gates strictly hold across all generated proposal sequences.

### Evidence & Pytest Output
Suite runtime:
```
tests/test_authoring_properties.py: 3 passed in 2.59s
```
Combined property tests:
```
tests/test_queue_properties.py tests/test_quota_properties.py tests/test_authoring_properties.py: 16 passed in 6.52s
```
Full test suite summary:
```
1287 passed, 1 skipped, 1 xfailed in 62.16s
```
Premerge check:
```
premerge green: Python 3.12; ty 28 <= 28
```

---

## Cycle 4: Parquet Compaction Property Tests

### Scope & Invariants Tested
- **Idempotence & Byte Stability**: recompacting an already-compacted day produces identical row counts and bit-for-bit identical SHA-256 parquet file hashes.
- **Zero Row Loss**: compacted tables have exactly the count of distinct primary keys across source jobs, verified with DuckDB.
- **Primary Key Deduplication & Sorting**: `deduplicate_and_sort` eliminates duplicate primary keys across all 9 projected tables and enforces deterministic ordering.
- **Retention & Pruning State Machine**: `CompactionRetentionStateMachine` fuzzes dynamic job additions, compaction across dates, retention pruning cutoffs, and row conservation.

### Hypothesis Findings & Counterexamples
- `_make_row("steps", ...)` initially missed non-nullable `source_sha256` in test generator. Generator updated with full valid schema fields. No source defects in `parquet_compaction.py`.

### Evidence & Pytest Output
Suite runtime:
```
tests/test_compaction_properties.py: 4 passed in 42.40s
```
Combined property tests:
```
tests/test_queue_properties.py tests/test_quota_properties.py tests/test_authoring_properties.py tests/test_compaction_properties.py: 20 passed in 42.73s
```
Full test suite summary:
```
1291 passed, 1 skipped, 1 xfailed in 123.34s
```
Premerge check:
```
premerge green: Python 3.12; ty 28 <= 28
```

---

## Cycle 5: Attach Surface and Catalog Rebuild Property Tests

### Scope & Invariants Tested
- **Rebuild Query Invariance**: `test_property_attach_queries_identical_after_drop_and_rebuild` generates randomized raw jobs with trials, rewards, tokens, and artifacts; projects initial derived parquet; executes a 10-query suite across all 9 tables; drops derived parquet completely; rebuilds from raw; and proves 100% identical query results.
- **Rebuild Byte Stability**: `test_property_catalog_rebuild_is_byte_stable` rebuilds from identical raw jobs into separate directories and proves bit-for-bit identical SHA-256 hashes across all generated Parquet files.
- **Graceful Degradation**: `test_property_attach_graceful_degradation_on_empty_or_missing_derived` verifies honest ZoneStatus reporting on missing roots and empty view querying (0 rows) on empty derived roots.

### Hypothesis Findings & Counterexamples
- `sum(primary_reward)` floating-point aggregation across parallel parquet partitions exhibited slight summing associativity variance (`4e-16`). Queries stabilized using `round(..., 4)` on float sums. No source defects in `attach.py` or `facts.py`.

### Evidence & Pytest Output
Suite runtime:
```
tests/test_attach_properties.py: 3 passed in 3.65s
```
All 5 property test suites combined:
```
tests/test_queue_properties.py tests/test_quota_properties.py tests/test_authoring_properties.py tests/test_compaction_properties.py tests/test_attach_properties.py: 23 passed in 34.25s
```
Full test suite summary:
```
1294 passed, 1 skipped, 1 xfailed in 91.76s
```
Premerge check:
```
premerge green: Python 3.12; ty 28 <= 28
```
