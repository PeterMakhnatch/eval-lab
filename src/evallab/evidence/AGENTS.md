# Evidence extraction

## Purpose
Deterministic ATIF parsing, facts, event-mart projections, Parquet IO, and request evidence. Runtime CAS and state events remain in their existing top-level modules.

## What lives here / entry points
- `atif.py`: ATIF canonical normalization and event conversion.
- `facts.py`: Deterministic trial fact extraction.
- `event_mart.py`: Event mart aggregation and query surfaces.
- `parquet_io.py`: High-performance columnar Parquet writing.
- `llm_request.py`: Structural projection for Goose `llm_request.*.jsonl` records.

## Invariants or rules
- Validate incoming ATIF against the appropriate schema.
- Identical input bytes and schema/version produce reproducible facts, digests, and query results. Intentional contract changes need explicit migration/version handling.
- Do not execute models or add subjective judgments here; preserve immutable input bytes and provenance.

