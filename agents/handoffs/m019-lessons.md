Status: building
Last: cycle 2 - statistical gates (Wilson 95% CI on every row) and refuse-to-rank propagation from cohort.py
Next: cycle 3 - add v_outcome_by_verifier_type and v_failure_by_facet join to craft parquet
Blockers: none

# M019 - LOOP-LESSONS: aggregates with honest gates

Handoff for M019 / `role/m019-lessons` worktree.

## Cycle 1: RECHECK views against live data & insufficient-n gating

### 1. Recheck & Acceptance
Rechecked previous claimed acceptance of merged statistical lessons work (`tests/test_lessons.py` and full pytest test suite). All 12 unit and integration tests pass cleanly.

### 2. Live Data Query Output
Executed all 3 DuckDB lesson views against live repository data (5 craft tasks, 25 synthesized trial facts from observatory-1 records, 3 analysis sidecars).

#### View 1: `v_outcome_by_verifier_type`
```
{'source_repo': 'local-lab/library', 'verifier_type': 'golden_file', 'n': 12, 'passed_n': 7, 'pass_rate_pct': 58.33, 'exceptions_n': 3, 'exception_rate_pct': 25.0, 'failed_unexcepted_n': 2, 'avg_duration_seconds': None, 'avg_cost_usd': None}
{'source_repo': 'local-lab/library', 'verifier_type': 'pytest', 'n': 7, 'passed_n': 1, 'pass_rate_pct': 14.29, 'exceptions_n': 3, 'exception_rate_pct': 42.86, 'failed_unexcepted_n': 3, 'avg_duration_seconds': None, 'avg_cost_usd': None}
{'source_repo': 'local-lab/library', 'verifier_type': 'hybrid', 'n': 6, 'passed_n': 0, 'pass_rate_pct': 0.0, 'exceptions_n': 3, 'exception_rate_pct': 50.0, 'failed_unexcepted_n': 3, 'avg_duration_seconds': None, 'avg_cost_usd': None}
```

#### View 2: `v_loop_rate_by_env`
```
{'source_repo': 'local-lab/library', 'env_services_n': 1, 'env_multi_container': False, 'env_files_bucket': '1_to_5_files', 'n': 25, 'loops_n': 0, 'loop_rate_pct': 0.0, 'avg_steps': 1.2, 'avg_tool_errors': 0.0}
```

#### View 3: `v_failure_by_facet` (sample of powered + insufficient-n rows)
```
{'source_repo': 'local-lab/library', 'facet_name': 'base_image_pin', 'facet_value': 'tag', 'failure_category': 'exception', 'validity': 'harness_failure', 'n': 9, 'failures_n': 9, 'failure_rate_pct': 100.0}
{'source_repo': 'local-lab/library', 'facet_name': 'base_image_pin', 'facet_value': 'tag', 'failure_category': 'unscored_failure', 'validity': 'valid_agent_attempt', 'n': 8, 'failures_n': 8, 'failure_rate_pct': 100.0}
{'source_repo': 'local-lab/library', 'facet_name': 'base_image_pin', 'facet_value': 'tag', 'failure_category': 'none', 'validity': 'passed', 'n': 8, 'failures_n': 0, 'failure_rate_pct': 0.0}
{'source_repo': 'local-lab/library', 'facet_name': 'dependency_pinning', 'facet_value': 'unstated', 'failure_category': 'none', 'validity': 'passed', 'n': 7, 'failures_n': 0, 'failure_rate_pct': 0.0}
{'source_repo': 'local-lab/library', 'facet_name': 'dependency_pinning', 'facet_value': 'pinned', 'failure_category': 'unscored_failure', 'validity': 'valid_agent_attempt', 'n': 3, 'failures_n': 3, 'failure_rate_pct': 100.0}
{'source_repo': 'local-lab/library', 'facet_name': 'dependency_pinning', 'facet_value': 'pinned', 'failure_category': 'exception', 'validity': 'harness_failure', 'n': 3, 'failures_n': 3, 'failure_rate_pct': 100.0}
{'source_repo': 'local-lab/library', 'facet_name': 'dependency_pinning', 'facet_value': 'unpinned', 'failure_category': 'exception', 'validity': 'harness_failure', 'n': 3, 'failures_n': 3, 'failure_rate_pct': 100.0}
{'source_repo': 'local-lab/library', 'facet_name': 'dependency_pinning', 'facet_value': 'unpinned', 'failure_category': 'unscored_failure', 'validity': 'valid_agent_attempt', 'n': 3, 'failures_n': 3, 'failure_rate_pct': 100.0}
{'source_repo': 'local-lab/library', 'facet_name': 'dependency_pinning', 'facet_value': 'unstated', 'failure_category': 'exception', 'validity': 'harness_failure', 'n': 3, 'failures_n': 3, 'failure_rate_pct': 100.0}
{'source_repo': 'local-lab/library', 'facet_name': 'dependency_pinning', 'facet_value': 'unstated', 'failure_category': 'unscored_failure', 'validity': 'valid_agent_attempt', 'n': 2, 'failures_n': 2, 'failure_rate_pct': 100.0}
{'source_repo': 'local-lab/library', 'facet_name': 'dependency_pinning', 'facet_value': 'unpinned', 'failure_category': 'none', 'validity': 'passed', 'n': 1, 'failures_n': 0, 'failure_rate_pct': 0.0}
```

### 3. Statistical Gating Output Example (including insufficient n renders)
```
| Source Repo | Facet Name | Facet Value | Category | Validity | n | Failures | Failure Rate | Wilson 95% CI | Status | Finding |
|---|---|---|---|---|---:|---:|---:|---|---|---|
| local-lab/library | base_image_pin | tag | exception | harness_failure | 9 | 9 | 100.0% | [70.1%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 70.1%-100.0%, n=9] |
| local-lab/library | dependency_pinning | pinned | unscored_failure | valid_agent_attempt | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | dependency_pinning | unpinned | none | passed | 1 | 0 | 0.0% | [0.0%, 79.3%] | `insufficient n` | insufficient n |
```

### 4. Hardening
Added `test_empty_views_render_insufficient_n_never_silent` to `tests/test_lessons.py` verifying that even when zero rows or empty tables are supplied, all 3 views produce `insufficient n` rows and never crash or emit empty-as-finding. Also fixed `sql/lessons.sql` so unmeasured step/tool-error rows are not coerced to 0 before `AVG()`.

### 5. Verification Output
Full pytest summary:
```
1272 passed, 2 skipped, 1 xfailed in 60.10s
```
Premerge check:
```
premerge green: Python 3.12; ty 28 <= 28
```

## Cycle 2: Statistical Gates & Refuse-to-Rank Propagation from cohort.py

### 1. Recheck & Acceptance
Rechecked Cycle 1 deliverables: all 3 views run against live repository data with gating and non-empty fallbacks. Ran `uv run pytest tests/test_lessons.py` with 100% pass.

### 2. Implementation: Statistical Gates & Refuse-to-Rank Propagation
Implemented statistical gating guarantees and pairwise refuse-to-rank propagation:
1. Every emitted row carries `n` and a `cohort.py` Wilson 95% confidence interval `(low, high)`, or the `insufficient n` marker.
2. Refuse-to-rank logic propagates directly from `cohort.NOT_COMPARABLE` (and explicit statistical criteria from `cohort.py`), preventing ranking when:
   - Either compared row has `powered is False` (underpowered / insufficient n).
   - Metrics are uninformative all-zero or all-constant across the comparison.
   - Wilson 95% confidence intervals overlap between the two rows.
   - Empirical rates are identical.
3. Added `LessonRanking`, `compare_lesson_rows()`, and `rank_lesson_rows()` in `src/evallab/lessons.py`.
4. Fixed ty typecheck issue in `lessons.py` (`first_failure_str` int conversion) maintaining diagnostics at exactly baseline 28.

### 3. Hardening
Added two regression tests in `tests/test_lessons.py`:
- `test_statistical_gating_every_row_carries_n_and_cohort_interval_or_marker`: Verifies every row in `build_lessons` across all views contains valid `n`, confidence intervals when powered, and `insufficient n` markers when unpowered.
- `test_refuse_to_rank_propagates_from_cohort`: Verifies refuse-to-rank behaves correctly on underpowered rows, overlapping confidence intervals, uninformative metrics (all-zero columns), and ranks only on disjoint intervals with `NOT_COMPARABLE` statement propagation.

### 4. Verification Output
Full pytest summary:
```
1275 passed, 1 skipped, 1 xfailed in 51.78s
```
Premerge check:
```
premerge green: Python 3.12; ty 28 <= 28
```
