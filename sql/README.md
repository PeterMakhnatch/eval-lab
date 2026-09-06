# Schema and analytical SQL views

## Purpose
`sql/` defines the relational schema for PostgreSQL and portable SQL views for DuckDB.
It powers the derived search and indexing layer over evaluated runs, trials,
and analytical features.

## What lives here / entry points
- `sql/schema.sql`: Primary PostgreSQL schema definition (tables, indexes, constraints).
- `sql/views.sql`: Core join spine (`v_spine`) and quota observation views.
- `sql/evidence_queries.sql`: Analytical queries over evidence facts and failure classifications.
- `sql/traj_benchmark_views.sql`: Analytical views for benchmark trajectory programs.
- `sql/craft_views.sql`: Analytical views for CRAFT benchmark facets.

## Invariants or rules
- Idempotent schema additions: Add schema changes idempotently to `sql/schema.sql` (cited in `AGENTS.md`).
- Derived search/index layer: PostgreSQL is a derived search/index layer; Harbor job directories are the immutable source of truth and must remain interpretable without the database (cited in `AGENTS.md`).
- Clean DuckDB execution: SQL view files must resolve cleanly in standalone in-memory DuckDB sessions with zero pre-created tables using fallback definitions (cited in `tests/test_z2_tables.py` and `tests/test_evidence_queries.py`).

## Tests or checks
- `uv run pytest tests/test_z2_tables.py`
- `uv run pytest tests/test_evidence_queries.py`
- `uv run pytest tests/test_canary.py -k test_schema`

## What not to add here
- Do not store raw database dumps, snapshots, or Parquet binary files here; use `derived/`.
- Do not add destructive DDL without explicit migration policies.
- Do not store application queries or business logic that belongs in Python modules under `src/evallab/`.
