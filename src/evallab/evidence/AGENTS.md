# Evidence Projection Subsystem (src/evallab/evidence/)

## Responsibilities
Owns the canonical ATIF projection, deterministic event mart, and trial-fact
extraction modules named by the frozen migration map. CAS storage, trajectory
IR/interpretation, path discovery, and compaction remain outside this package.

## Core Invariants
1. ATIF Conformance: telemetry must validate against the canonical ATIF schema.
2. Projection Parity: exported rows, Parquet schemas, SQL-facing table names,
   digests, and query results must not change during physical moves.
3. Single Authority: moved modules exist only under this package; old top-level
   paths are deleted in the same PR.

## Testing & Verification
- Targeted tests follow the moved modules: ATIF/fixture conformance, event mart,
  facts/truth/state events, Parquet schema, and direct CLI consumers.
