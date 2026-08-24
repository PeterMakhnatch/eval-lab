Status: done
Last: merged as PR #138 (`55cb9ee`)
Next: none
Blockers: none

# M029: INGEST Handoff

## Summary
Resolved the data projection gap where cataloged trial jobs were missing from the Parquet analytics surface. Implemented the completeness invariant: every trial directory on disk and every catalog entry in PostgreSQL is either projected into Parquet partitions or explicitly accounted for with a named reason.

Created the ingest verification engine (`src/evallab/ingest_verify.py`), canonical DuckDB reconciliation views (`sql/ingest_views.sql`), enhanced `ProjectionInvariant` with per-reason breakdown in `src/evallab/atif.py`, and added a comprehensive test suite (`tests/test_ingest_verify.py`).

---

## Target & Lease
- **Target**: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/m029-ingest` on branch `role/m029-ingest` (Mission **M029 INGEST**).
- **Leased Files**:
  - `src/evallab/ingest_verify.py` (new)
  - `sql/ingest_views.sql` (new)
  - `src/evallab/atif.py` (enhanced `ProjectionInvariant` with per-reason breakdown)
  - `tests/test_ingest_verify.py` (new)
  - `tests/test_pipeline.py` (updated for per-reason breakdown)
  - `agents/handoffs/m029-ingest.md` (new)

---

## Diagnosis & Measured Failure Causes

### Measured Per-Cause Counts
Total catalog entries inspected: **80 jobs, 100 trials**.

1. **Unprojected Catalog Jobs (Lab Defect)**: **6 jobs** (6 trials)
   - `gymv0-nop-event-summary` (job_id: `36b97136-cdc6-4518-92bd-0b2315a4a642`)
   - `gymv0-nop-query-optimize` (job_id: `e4444502-b8c2-4f76-9095-b606122880d2`)
   - `gymv0-nop-transaction-reconciliation` (job_id: `a38c4af4-6964-416e-8f32-a05f61328fab`)
   - `gymv0-oracle-event-summary` (job_id: `162af89e-8499-470e-b5f1-567de820a3a7`)
   - `gymv0-oracle-query-optimize` (job_id: `4551d14c-29f8-4476-af8a-4fcee9f48f21`)
   - `gymv0-oracle-transaction-reconciliation` (job_id: `f70d6018-e138-4dd3-8b6f-f3b2a7c93fac`)
   - **Cause**: Jobs were ingested into the PostgreSQL catalog during prior runs, but `project_jobs` / `rebuild_from_raw` was never executed for them, leaving catalog entries without `job_id=*` Parquet partitions.
   - **Resolution**: Projected cleanly via `project_jobs`, producing 54 Parquet tables.

2. **Unprojectable Disk Runs (Legitimately Unprojectable)**: **2 directories**
   - `runs/brief07-query-controls/brief07-query-optimize-oracle/query-optimize__FMKBKV2`
     - **Reason**: `crashed_execution` (Docker inspect failed during setup, trial aborted before `result.json` was generated). Parent job recorded `n_total_trials=1, n_completed_trials=0, n_errored_trials=1`.
   - `runs/failed-network-policy-oracle/event-summary__AjHV5Cq`
     - **Reason**: `crashed_execution` (Network policy failure, trial aborted before result generation). Parent job recorded 0 completed trials.
   - **Resolution**: Accounted and categorized by `scan_disk_trials` with explicit reasons rather than silently dropped.

3. **Promoted Evidence Duplicates**: **3 directories**
   - `research/evidence/runs/canary-event-summary-codex-20260815`
   - `research/evidence/runs/canary-terminal-bench-html-js-filter-codex-20260815`
   - `research/evidence/runs/canary-transaction-reconciliation-codex-20260815`
   - **Status**: Promoted immutable evidence copies sharing the exact same job UUIDs with `runs/canary-*`, already projected under their job UUIDs in `derived/parquet`.

---

## Real Before & After Output

### Before: `uv run python -m evallab.cli doctor`
```
FAIL  catalog-parquet catalog=80 projected=74 exceptions=0 missing=6 extra=0 db=localhost:54329/evallab
```

### After: `uv run python -m evallab.cli doctor`
```
ok    catalog-parquet catalog=80 projected=80 exceptions=0 missing=0 extra=0 db=localhost:54329/evallab
```

### Ingest Verification: `uv run python -m evallab.ingest_verify`
```
=== Ingest Completeness Verification ===
Disk trial directories:       11 projectable, 0 unprojectable
Catalog (PostgreSQL):         80 jobs, 100 trials
Parquet analytics partitions: 80 jobs, 100 trials
ATIF trajectory documents:    23 indexed
Accounted exceptions:         0
Gaps detected:                0
Completeness status:          COMPLETE (0 gaps)
```

---

## Mutation Testing Evidence

### Mutation 1: Suppress trial gap detection in `ingest_verify.py`
- **Mutation**: Commented out `missing_parquet_trials` gap reporting loop in `src/evallab/ingest_verify.py`.
- **Result**: `test_ingest_verify_detects_unaccounted_missing_partition` failed:
```
FAILED tests/test_ingest_verify.py::test_ingest_verify_detects_unaccounted_missing_partition - assert True is False
 +  where True = IngestVerificationResult(..., gaps=()).is_complete
1 failed, 5 deselected in 0.25s
```
- **Restored**: Passed (1 passed).

### Mutation 2: Change unprojectable categorization reason
- **Mutation**: Changed `reason = "crashed_execution"` to `reason = "unknown_failure"` in `src/evallab/ingest_verify.py`.
- **Result**: `test_scan_disk_trials_categorizes_unprojectable_runs` failed:
```
FAILED tests/test_ingest_verify.py::test_scan_disk_trials_categorizes_unprojectable_runs - AssertionError: assert 'unknown_failure' == 'crashed_execution'
1 failed, 5 deselected in 0.22s
```
- **Restored**: Passed (1 passed).

### Mutation 3: Remove per-reason breakdown in `ProjectionInvariant.detail`
- **Mutation**: Removed `exceptions_by_reason` formatting in `src/evallab/atif.py`.
- **Result**: `test_projection_invariant_per_reason_breakdown` failed:
```
FAILED tests/test_ingest_verify.py::test_projection_invariant_per_reason_breakdown - AssertionError: assert 'catalog=3 projected=1 exceptions=2 missing=0 extra=0' == 'catalog=3 projected=1 exceptions=2 (CorruptedTrajectory=1, MissingResultJson=1) missing=0 extra=0'
1 failed, 5 deselected in 0.22s
```
- **Restored**: Passed (1 passed).

### Mutation 4: Ignore recorded exceptions and report as active gaps
- **Mutation**: Removed `and job_id not in recorded_exceptions` in `src/evallab/ingest_verify.py`.
- **Result**: `test_ingest_verify_accounts_for_exception_in_gaps` failed:
```
FAILED tests/test_ingest_verify.py::test_ingest_verify_accounts_for_exception_in_gaps - AssertionError: assert 1 == 0
1 failed, 5 deselected in 0.23s
```
- **Restored**: Passed (1 passed).

---

## Premerge Gate Verification

```bash
$ env -u EVALLAB_DERIVED_ROOT bash scripts/premerge.sh; echo "EXIT_CODE=$?"
Resolved 75 packages in 2ms
Audited 51 packages in 1ms
All checks passed!
1533 passed, 2 skipped, 1 xfailed in 126.84s (0:02:06)
...
PASS doctor mode=docker-free
PASS submit->tick job=smoke-oracle-p0stsryjc8ev trials=1
PASS catalog job_id=886e92a2-0de4-4384-b7ad-aa8c623e96b1
PASS parquet job_id=886e92a2-0de4-4384-b7ad-aa8c623e96b1
PASS digest path=runs/_smoke/smoke-oracle-p0stsryjc8ev/digests/2026-08-19.md
PASS analysis sidecar=runs/_smoke/smoke-oracle-p0stsryjc8ev/analyses/67f312dd-6159-4df1-8782-515b20c89f62/analysis.json validation=valid
PASS status snapshot sections=Recent,Now,Next,Tasks,Health,Analysis analysis=draft
SMOKE PASS both-stores-agree
...
premerge green: Python 3.12; ty 27 <= 28
EXIT_CODE=0
```
