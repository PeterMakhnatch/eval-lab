Status: review-wanted
Last: statistical lesson aggregation views and engine (WS-D)
Next: Integrator review and merge into main
Blockers: none

# LESSONS: Aggregation Views & Findings Engine (WS-D)

Implements the statistical lesson aggregation views and findings engine from `docs/build-plan.md` WS-D.

## Leased Files
- `sql/lessons.sql`
- `src/evallab/lessons.py`
- `tests/test_lessons.py`
- `research/lessons.md`
- `agents/handoffs/lessons.md`

## Summary of Changes

1. **SQL Views (`sql/lessons.sql`):**
   Created DuckDB views joining:
   - `v_failure_by_facet`: craft facets ⋈ trials ⋈ analysis sidecars, analyzing failure categories, validity breakdown, and failure rates across structural task facets (verifier type, instruction style, difficulty mechanism, container mode, base image pin, dependency pinning).
   - `v_loop_rate_by_env`: observation records / trajectories ⋈ craft complexity, evaluating tool call loops vs multi-container and environment file complexity.
   - `v_outcome_by_verifier_type`: verifier types (pytest, golden_file, diff, hybrid, etc.) ⋈ pass rates and exception rates.
   - Fallback schemas included to ensure standalone DuckDB execution works without prior table creation.

2. **Lessons Engine (`src/evallab/lessons.py`):**
   - Discovers and loads craft records, trial facts, stage-5 analysis sidecars, and observatory markdown observation records.
   - Populates in-memory DuckDB tables and executes the lesson views.
   - Applies statistical gating using Wilson 95% confidence intervals (via `evallab.cohort.wilson_interval`). Rows below the power threshold ($n < 5$) carry status `insufficient n` and are never reported as generalized findings.
   - Generates `research/lessons.md` with required `<!-- generated-by: lessons v1 -->` header.

3. **Verification & Tests (`tests/test_lessons.py`):**
   - Comprehensive unit and integration test suite testing all view queries against mock fixtures.
   - Verifies statistical gating logic: $n < 5$ correctly yields status `insufficient n` and finding `insufficient n`, while $n \ge 5$ computes point estimates and valid Wilson 95% confidence intervals.
   - Verifies observation markdown parser, standalone SQL script execution, and full end-to-end lesson generation on repo evidence.

## Verification
- `uv run pytest tests/test_lessons.py` (7 passed in 0.28s)
- `uv run pytest` (885 passed, 1 xfailed in 24.5s)
- `uv run ruff check .` (all clean)
- `uv run python -c "from pathlib import Path; from evallab.lessons import generate_lessons_file; generate_lessons_file(Path('.'))"` generates `research/lessons.md` deterministically.
