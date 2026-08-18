Status: building
Last: cycle 1 - live data recheck on views with non-empty insufficient n gating
Next: cycle 2 - statistical gates and propagation from cohort.py
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
