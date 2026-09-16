# Audit Evidence: parquet-compaction

- **Subject**: `parquet-compaction`
- **Handoff Audited**: `agents/handoffs/parquet-compaction.md`
- **Audit Date**: 2026-08-18
- **Auditor**: M015 (LOOP-AUDIT)

## Claims Extracted from Handoff
1. `src/evallab/parquet_compaction.py` implements compaction of granular partitions (`derived/parquet/job_id=*/`) into Hive daily partitions (`derived/parquet/compact/dt=YYYY-MM-DD/`).
2. Compacts all 9 tables: `jobs`, `trajectories`, `steps`, `tool_calls`, `observations`, `trial_facts`, `reward_facts`, `artifact_facts`, and `tool_usage`.
3. Date resolution hierarchy (steps timestamp -> result.json -> PostgreSQL -> mtime).
4. Retention window (default 7 days) and non-pruning of recent partitions.
5. Atomic temporary writes with DuckDB deduplication and validation.
6. CLI entrypoint: `python -m evallab.parquet_compaction compact [...]`.
7. `tests/test_parquet_compaction.py` has 15 tests.
8. Documentation in `docs/parquet-compaction.md`.

## Re-run & Reproduction Commands
```bash
# 1. Run unit/integration tests for parquet compaction
uv run pytest tests/test_parquet_compaction.py

# 2. Run compaction planner against real shared parquet root in dry-run mode
uv run python -m evallab.parquet_compaction compact --dry-run

# 3. Check CLI help surface
uv run python -m evallab.parquet_compaction --help
```

## Captured Outputs & Verdict
1. `uv run pytest tests/test_parquet_compaction.py`:
   Output: `15 passed in 0.91s` (CONFIRMED)
2. `uv run python -m evallab.parquet_compaction compact --dry-run`:
   Output: Successfully scanned 3 dates (2026-08-14, 2026-08-15, 2026-08-16) across 72 jobs and planned compaction for all 9 tables with 0 row loss (CONFIRMED)
3. Documentation in `docs/parquet-compaction.md`:
   Fully structured document with schema specifications and DuckDB examples present (CONFIRMED)

## Overall Verdict
**CONFIRMED** (Engine, CLI dry-run across real shared data, and all 15 tests pass cleanly).

## Risk Note
Compaction is run via `python -m evallab.parquet_compaction` rather than integrated into `evallab` top-level CLI (`evallab compact`), requiring explicit module invocation unless scheduled via automation.
