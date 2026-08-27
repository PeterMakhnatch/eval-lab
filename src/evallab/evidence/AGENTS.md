# Evidence & Trajectory Subsystem (src/evallab/evidence/)

## Responsibilities
Handles Trajectory IR (Intermediate Representation), ATIF telemetry schemas, Parquet/DuckDB storage partitioning, and digest synthesis.

## Core Invariants
1. ATIF Conformance: All trajectory telemetry must strictly validate against the canonical ATIF schema.
2. Partition Immutability: Historical Parquet partitions in `derived/parquet/` are append-only and immutable. Daily compaction rolls up loose partitions.
3. Zero Raw Key Leakage: All credential strings and secret env vars must be redacted before persisting to disk.

## Testing & Verification
- Targeted unit tests: `pytest tests/test_paths.py tests/test_lance.py tests/test_craft.py tests/test_digest.py`
