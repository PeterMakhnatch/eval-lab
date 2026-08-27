# Storage Subsystem (src/evallab/storage/)

## Responsibilities
Owns physical path management, content-addressed storage (CAS) layout and blobs,
DuckDB unified attach surface (`evallab db attach`), Parquet lake compaction,
and full-historical trial backfills (`evallab data backfill`).

## Core Invariants
1. Single Source of Path Authority: All runtime and evidence paths must be resolved
   via `evallab.storage.paths`. Do not hardcode filesystem paths.
2. Unified Multi-Zone Attach: `evallab.storage.attach` provides access across Zone 2
   (PostgreSQL catalog), Zone 3 (Parquet lake), and Zone 4 (curated marts) without
   polluting analytical queries with storage dialect details.
3. Fail-Closed Backfills: `data_backfill` assigns explicit reason-coded dispositions
   (`ANALYSIS_READY` vs `HOLD`) and never admits unverified or corrupted trials.
4. Storage Separation: Raw durable evidence (`runs/trial_jobs/`, `derived/evidence-cas/`)
   is immutable and protected from eviction; derived Parquet tables (`derived/parquet/`)
   are strictly rebuildable projections.

## Testing & Verification
- Targeted unit tests: `pytest tests/test_paths.py tests/test_attach_surface.py tests/test_parquet_compaction.py tests/test_data_backfill.py`
