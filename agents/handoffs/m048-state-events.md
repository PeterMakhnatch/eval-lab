Status: review-wanted
Last: repaired the state hard gate, including baseline/revert evidence, typed operations, invalid sentinels, and golden table contracts
Next: independent diff review of the repaired implementation
Blockers: none

# M048 (B) — State event facts

## Contract

- **Outcome:** make `StateEventFact` ingestible, compactable, temporally queryable, and explicitly non-causal.
- **Lane / owner:** Platform / Platform lane owner.
- **Exclusive lease:** `containers/state-journal/{Dockerfile,producer.py,watch.py}`, additive `src/evallab/harbor_state_journal.py`, `src/evallab/state_events.py`, `src/evallab/schemas.py`, `src/evallab/facts.py`, `src/evallab/atif.py`, `src/evallab/attach.py`, `src/evallab/parquet_compaction.py`, focused compaction fixtures, `tests/test_state_events.py`, and `tests/fixtures/state_events/**`.
- **Status:** review-wanted; focused and full local validation are recorded below.
- **Acceptance:** the committed golden stream and final net diff are regenerated through the runner image's producer module. Existing baseline→write-one→baseline(revert)→write-two projects as three valid facts with sequence/predecessor, typed multi-operations, bounded before/after values, and producer/source identity; the final diff honestly retains only baseline→write-two. Direct malformed, duplicate, conflicting, gapped, or non-append evidence fails closed. Job-fact extraction retains sibling facts and emits one deterministic invalid sentinel. Repeat projection overwrites rather than duplicates; semantics are temporal and explicitly non-causal.
- **Next executable step:** independent diff review.

## Source evidence and dependencies

PR #146 added the state journal, event mart, and compaction surfaces, but `origin/main` has no `StateEventFact` contract. This mission consumes PLATFORM-146 and is independent of M047, M049, M050, and M051.

## Implementation note

`state-events.jsonl` remains the evidence source. The runner plugin image and
the fixture generator share the same portable producer module. Projection does
not invoke Git, collapse by path, infer causality, or repair ambiguous records.
The source-byte digest and per-record digest bind each fact to the observed
stream, while the primary key remains `(job_id, trial_id, sequence)`.

## Validation

- Focused state/compaction/attach/golden suite: 51 passed.
- Sibling state-journal/event-mart/golden facts: 11 passed.
- Full suite: 1,759 collected; 1,756 passed, 2 skipped, 1 xfailed.
- Full Ruff, governance, repomap freshness, docindex freshness, lessons freshness,
  and registry audit passed. Pinned `ty 0.0.71` remained at its 28-diagnostic
  baseline.
