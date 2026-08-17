---
status: living
audience:
  - operator
  - analyst
---
# Parquet Compaction Engine

Status: living. Owner: Platform lane. Date: 2026-08-16. Implements WS-E Item 4 from `docs/build-plan.md`.

`src/evallab/parquet_compaction.py` consolidates granular uncompacted Parquet trial partitions from `derived/parquet/job_id=*/` into a daily partitioned layout under `derived/parquet/compact/dt=YYYY-MM-DD/`.

Compaction is a pure projection over raw evidence and derived fact tables: it enforces zero row loss, preserves exact PyArrow schemas, supports idempotent re-runs, and retains granular partitions for a configurable trailing window (default 7 days) while pruning older partitions after validation.

## Layout & Architecture

### Granular Uncompacted Layout (Source)
```text
derived/parquet/
  job_id=03c50e09-d16f-4058-93b9-893bb9cae9da/
    jobs.parquet
    trial_id=1e40baab-3f5b-4030-89a0-439c25638328/
      trajectories.parquet
      steps.parquet
      tool_calls.parquet
      observations.parquet
      trial_facts.parquet
      reward_facts.parquet
      artifact_facts.parquet
      tool_usage.parquet
```

### Compacted Daily Layout (Target)
```text
derived/parquet/
  compact/
    dt=2026-08-15/
      jobs.parquet
      trajectories.parquet
      steps.parquet
      tool_calls.parquet
      observations.parquet
      trial_facts.parquet
      reward_facts.parquet
      artifact_facts.parquet
      tool_usage.parquet
    dt=2026-08-16/
      ...
```

One consolidated Parquet file is produced per table per day.

## Table Coverage & Deduplication

All 9 projected and fact tables are compacted and deduplicated across jobs and trials:

| Table | Primary Key | Source Schema |
|---|---|---|
| `jobs` | `job_id` | `PARQUET_SCHEMAS["jobs"]` |
| `trajectories` | `job_id, trial_id, document_id` | `PARQUET_SCHEMAS["trajectories"]` |
| `steps` | `job_id, trial_id, document_id, step_id` | `PARQUET_SCHEMAS["steps"]` |
| `tool_calls` | `job_id, trial_id, document_id, step_id, tool_call_id` | `PARQUET_SCHEMAS["tool_calls"]` |
| `observations` | `job_id, trial_id, document_id, step_id, observation_index` | `PARQUET_SCHEMAS["observations"]` |
| `trial_facts` | `job_id, trial_id` | `FACT_SCHEMAS["trial_facts"]` |
| `reward_facts` | `job_id, trial_id, reward_name` | `FACT_SCHEMAS["reward_facts"]` |
| `artifact_facts` | `job_id, trial_id, source` | `FACT_SCHEMAS["artifact_facts"]` |
| `tool_usage` | `job_id, trial_id, function_name` | `FACT_SCHEMAS["tool_usage"]` |

Deduplication uses DuckDB in-memory window queries (`QUALIFY row_number() OVER (PARTITION BY ... ORDER BY ...) = 1`) with deterministic sort ordering.

## Date Resolution Hierarchy

A job's partition date (`dt=YYYY-MM-DD` in UTC) is determined through a four-stage hierarchy:

1. **`steps.parquet` timestamp**: Reads the ISO8601 `timestamp` column from trial steps.
2. **`result.json` in evidence/runs roots**: Matches `job_id` against `result.json` `finished_at` / `started_at`.
3. **PostgreSQL catalog**: Queries `jobs.finished_at` / `jobs.started_at` when a database connection is configured.
4. **Filesystem mtime**: Fallback to modification time of `jobs.parquet` (or `job_id` directory) in UTC.

## Retention & Pruning

- **Closed Days**: Days strictly before the current UTC date (or explicitly targeted via `--target-date`).
- **Trailing Granular Retention**: Granular partitions `job_id=*` for recent days (`dt >= today - retention_days`, default 7 days) are retained for deep trial exploration and debugging.
- **Older Partition Pruning**: For days older than the retention threshold (`dt < today - retention_days`), granular partitions `job_id=*` are pruned (`shutil.rmtree`) only after compaction and schema/row-count validation succeed 100%.
- Pruning can be disabled with `--no-prune`.

## Validation & Integrity

Every table write adheres to a strict atomic validation contract:

1. Data is written to `<table_name>.parquet.tmp` using zstd compression and dictionary encoding disabled (`use_dictionary=False`, `write_statistics=True`).
2. The written temporary file is read back immediately:
   - `written.num_rows == expected_num_rows` (zero row loss verification).
   - `written.schema.equals(expected_schema)` (exact Arrow schema integrity).
3. If validation fails, `CompactionValidationError` is raised, temporary files are removed, and no granular partitions are pruned.
4. On success, `temp_path.replace(target_path)` atomically publishes the compacted file.

## Running Compaction

### CLI

```bash
# Compact all closed days (default 7 days granular retention)
python -m evallab.parquet_compaction compact

# Compact a specific target date
python -m evallab.parquet_compaction compact --target-date 2026-08-14

# Dry run (plan without modifying disk)
python -m evallab.parquet_compaction compact --dry-run

# Output structured JSON
python -m evallab.parquet_compaction compact --json

# Override derived Parquet root
python -m evallab.parquet_compaction compact --derived-dir /path/to/derived/parquet
```

### Programmatic API

```python
from evallab.parquet_compaction import compact, plan_compaction

# Execute compaction
result = compact(
    derived_root=Path("derived/parquet"),
    target_date="2026-08-14",
    retention_days=7,
)

assert result.ok
print(f"Compacted {len(result.compacted_days)} days, pruned {len(result.pruned_jobs)} jobs.")
```

## DuckDB Querying

Compacted partitions support DuckDB Hive partitioning queries across all days with automatic partition pruning:

```sql
-- Query all trial facts across compacted dates
SELECT dt, count(*), avg(primary_reward)
FROM read_parquet('derived/parquet/compact/dt=*/trial_facts.parquet', hive_partitioning = true)
GROUP BY dt
ORDER BY dt DESC;

-- Query a single closed day directly
SELECT *
FROM read_parquet('derived/parquet/compact/dt=2026-08-14/steps.parquet');
```
