# Storage Subsystem (src/evallab/storage/)

## Purpose
Physical path resolution, content-addressed storage (CAS) layout, DuckDB
unified attach surfaces, Parquet lake compaction, and historical backfills.

## What lives here / entry points
- `paths.py`: Single source of truth for runtime and evidence filesystem paths.
- `attach.py`: Multi-zone DuckDB unified attach (`evallab db attach`).
- `parquet_compaction.py`: Compacts raw partitioned Parquet files into lake stores.
- `data_backfill.py`: Trial ingestion and backfill engine (`evallab data backfill`).
- `inspect_storage.py`, `incremental_ingest.py`: Storage diagnostic utilities.

## Invariants or rules
1. Single Source of Path Authority: All runtime and evidence paths must be resolved
   via `evallab.storage.paths`. Do not hardcode filesystem paths.
2. Unified Multi-Zone Attach: `evallab.storage.attach` provides access across Zone 2
   (PostgreSQL catalog), Zone 3 (Parquet lake), and Zone 4 (curated marts).
3. Fail-Closed Backfills: `data_backfill` assigns explicit reason-coded dispositions
   (`ANALYSIS_READY` vs `HOLD`) and never admits unverified or corrupted trials.
4. Storage Separation: Raw durable evidence in `runs/` and `derived/evidence-cas/`
   is protected from cache cleanup.

## Tests or checks
- Targeted unit tests: `pytest tests/test_paths.py tests/test_attach.py tests/test_attach_properties.py tests/test_parquet_compaction.py tests/test_data_backfill_command.py`

## What not to add here
Do not introduce direct SQL dialect abstractions or model evaluation code here.
