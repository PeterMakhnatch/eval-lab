# Evidence extraction

## Purpose
Deterministic ATIF parsing, facts, event-mart projections, Parquet IO, capture authority, request evidence, and Reef-recorded evidence readers. Runtime CAS and state events remain in their existing top-level modules.

## What lives here / entry points
- `atif.py`: ATIF canonical normalization and event conversion.
- `facts.py`: Deterministic trial fact extraction.
- `event_mart.py`: Event mart aggregation and query surfaces.
- `parquet_io.py`: High-performance columnar Parquet writing.
- `capture_authority.py`, `llm_request.py`: Telemetry capture authority.
- `reef_intake.py`, `reef_shift.py`: Reef-recorded evidence intake and harness-version shift comparison (readers only, no execution or calibration).

## Invariants or rules
- Validate incoming ATIF against the appropriate schema.
- Identical input bytes and schema/version produce reproducible facts, digests, and query results. Intentional contract changes need explicit migration/version handling.
- Do not execute models or add subjective judgments here; preserve immutable input bytes and provenance.

## Tests or checks
The assigned verifier uses affected existing extraction/query tests, including `tests/test_event_mart.py`, `tests/test_evidence_queries.py`, and `tests/test_evidence_store.py` as relevant.

## What not to add here
No runtime-driver orchestration or module-layout migration outside the assigned change.
