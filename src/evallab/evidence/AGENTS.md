# Evidence Projection Subsystem (src/evallab/evidence/)

## Purpose
Canonical ATIF normalization, deterministic event marts, and trial-fact
extraction for evaluation telemetry.

## What lives here / entry points
- `atif.py`: ATIF canonical normalization and event conversion.
- `facts.py`: Deterministic trial fact extraction.
- `event_mart.py`: Event mart aggregation and query surfaces.
- `parquet_io.py`: High-performance columnar Parquet writing.
- `capture_authority.py`, `llm_request.py`: Telemetry capture authority.

## Invariants or rules
1. ATIF Conformance: Telemetry must validate against the canonical ATIF schema.
2. Projection Parity: Exported rows, Parquet schemas, SQL-facing table names,
   digests, and query results must remain deterministic across versions.
3. Frozen Layout: Do not relocate remaining top-level evidence helpers without
   explicit approval (`evidence_store.py` and `state_events.py` remain at root).

## Tests or checks
- Targeted unit tests: `pytest tests/test_atif.py tests/test_evidence_facts.py tests/test_event_mart.py`

## What not to add here
Do not place raw model execution or subjective evaluator logic here.
