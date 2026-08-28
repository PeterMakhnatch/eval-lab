# Evidence Projection Subsystem (src/evallab/evidence/)

## Responsibilities
Owns the canonical ATIF projection, deterministic event mart, and trial-fact
extraction modules named by the frozen migration map.

Still **outside** this package (do not assume they moved):
- CAS storage: `src/evallab/evidence_store.py`
- State events helper: `src/evallab/state_events.py`
- Semantic fact models: `src/evallab/semantic_facts.py`
- Trajectory IR / interpretation, path discovery, and compaction

## Core Invariants
1. ATIF Conformance: telemetry must validate against the canonical ATIF schema.
2. Projection Parity: exported rows, Parquet schemas, SQL-facing table names,
   digests, and query results must not change during physical moves.
3. Frozen layout: do not relocate remaining top-level evidence helpers without
   Peter approval. This package is not a promise that every evidence module has
   already moved.

## Testing & Verification
- Targeted tests follow the moved modules: ATIF/fixture conformance, event mart,
  facts/truth/state events, Parquet schema, and direct CLI consumers.
