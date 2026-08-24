Status: ready
Last: registered the StateEventFact ingest, compaction, temporal-link, and query-fixture contract
Next: define StateEventFact and ingest the smallest out-of-order fixture
Blockers: none

# M048 (B) — State event facts

## Contract

- **Outcome:** make `StateEventFact` ingestible, compactable, temporally queryable, and explicitly non-causal.
- **Lane / owner:** Platform / Platform lane owner.
- **Exclusive lease:** `src/evallab/state_events.py` (new), additive `src/evallab/schemas.py`, `src/evallab/event_mart.py`, `src/evallab/parquet_compaction.py`, `tests/test_state_events.py` (new), and `tests/fixtures/state_events/**`.
- **Status:** ready; independent follow-on PR.
- **Acceptance:** fixture events survive ingest and compaction byte-for-field, query by entity plus event/observed time, and expose a typed temporal link labelled non-causal. Late and duplicate events have deterministic retained identities.
- **Next executable step:** define the fact schema and ingest one minimal out-of-order fixture.

## Source evidence and dependencies

PR #146 added the state journal, event mart, and compaction surfaces, but `origin/main` has no `StateEventFact` contract. This mission consumes PLATFORM-146 and is independent of M047, M049, M050, and M051.
