Status: blocked
Last: registered the typed P/R/U/C/Y harness, policy, and heldout enforcement contract
Next: after M047 and M049 merge, freeze one heldout fixture and implement pre-execution rejection
Blockers: M047 factor provenance and M049 byte-bound certification

# M052 (F) — Typed P/R/U/C/Y capability contract

## Contract

- **Outcome:** enforce typed P/R/U/C/Y capability contracts at harness, policy, and heldout boundaries without a scalar score.
- **Lane / owner:** Research / Research lane owner, with Platform review at execution boundaries.
- **Exclusive lease:** `src/evallab/capability_contract.py` (new), `tests/test_capability_contract.py` (new), `research/experiments/capability-contracts/**`, and additive integration in `src/evallab/screen.py` plus policy-admission tests.
- **Status:** blocked on M047 and M049.
- **Acceptance:** every dimension is a typed value with provenance and explicit unavailable state; harness and policy reject undeclared or mismatched contracts before execution; heldout identities cannot enter authoring/tuning inputs; reports preserve the vector and never emit or rank by a scalar aggregate.
- **Next executable step:** after both dependencies merge, freeze one heldout fixture and implement pre-execution rejection.

## Source evidence and dependencies

M045/#140 supplies staged cohorts and M049 supplies exact-byte certification. Neither establishes typed P/R/U/C/Y enforcement or heldout separation. Dependency order is M047 + M049, then M052; M048, M050, and M051 are independent.
