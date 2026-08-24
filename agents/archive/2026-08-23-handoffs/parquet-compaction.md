Status: done
Last: merged as PR #79 (`f2b3985`)
Next: none
Blockers: none

# PARQUET-COMPACTION

Status: review-wanted
Last: deterministic Parquet compaction engine implemented and verified; 15/15 tests passing; full suite (893 passed, 1 xfailed) and ruff clean; PR opened
Next: integrator review and merge
Blockers: none

Branch `role/parquet-compaction`, worktree `.worktrees/parquet-compaction`, off `origin/main` at `d27e4a7`.
Lease: `src/evallab/parquet_compaction.py`, `tests/test_parquet_compaction.py`, `docs/parquet-compaction.md`, this file.
Non-goals respected: `schemas.py`, `queue.py`, `cli.py`, and `policy/` untouched.

## Summary of Deliverables (WS-E Item 4)

1. **Parquet Compaction Engine (`src/evallab/parquet_compaction.py`):**
   - Consolidates uncompacted granular partitions (`derived/parquet/job_id=*/`) into Hive-partitioned daily directories (`derived/parquet/compact/dt=YYYY-MM-DD/`).
   - Compacts all 9 tables: `jobs`, `trajectories`, `steps`, `tool_calls`, `observations`, `trial_facts`, `reward_facts`, `artifact_facts`, and `tool_usage`.
   - Resolves job dates hierarchically (`steps.parquet` timestamp -> `result.json` -> Postgres -> `mtime`).
   - Retains granular partitions for trailing recent days (default 7 days) and prunes older granular partitions only after verified compaction.
   - Enforces zero row loss and exact PyArrow schema preservation using atomic temporary writes with post-write validation and rollback on error.
   - Deterministic deduplication by primary key via DuckDB window queries.
   - CLI entry point: `python -m evallab.parquet_compaction compact [--target-date YYYY-MM-DD] [--derived-dir DIR] [--retention-days 7] [--dry-run] [--no-prune] [--json]`.

2. **Test Suite (`tests/test_parquet_compaction.py`):**
   - 15 unit and integration tests covering date resolution, end-to-end compaction, row count validation, schema preservation, idempotent re-runs, pruning vs retention boundaries, `--dry-run`, DuckDB Hive partitioning queries, and CLI output.

3. **Documentation (`docs/parquet-compaction.md`):**
   - Comprehensive documentation detailing source/target partition layouts, table primary keys, date resolution hierarchy, retention semantics, validation contract, CLI examples, and DuckDB SQL query examples.

## Verification Evidence

- `uv run pytest tests/test_parquet_compaction.py`: 15 passed in 0.85s.
- `uv run pytest`: 893 passed, 1 xfailed (full suite green).
- `uv run ruff check .`: All checks passed!
