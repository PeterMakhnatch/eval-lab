Status: review-wanted
Last: implemented the typed, byte-bound P/R/U/C/Y contract and screen admission guard
Next: review the contract and free integrated workflow together; validation is intentionally unclaimed
Blockers: none

# M052 (F) — Typed P/R/U/C/Y capability contract

## Contract

- **Outcome:** enforce independent typed P/R/U/C/Y capability contracts at harness, policy, freeze, and heldout boundaries without a scalar score.
- **Lane / owner:** Research / Research lane owner, with Platform review at execution boundaries.
- **Exclusive lease:** `src/evallab/capability_contract.py`, `src/evallab/capability_workflow.py`, their focused tests and workflow fixtures, `research/experiments/capability-contracts/**`, and additive integration in `src/evallab/screen.py` plus policy-admission tests.
- **Status:** review-wanted; implementation is present and validation is intentionally unclaimed.
- **Acceptance represented:** every report preserves the complete P/R/U/C/Y vector with explicit unavailable/insufficient/invalid/satisfied states; current evidence bytes and path/kind identities are re-read without symlink or repository escape; typed admission rejects invalid reports while accepting honest `valid_insufficient` evidence; the free workflow executes M047/M049/M048/M051 APIs while refusing to turn their component evidence into generality; no model exposes a scalar generality or integration score.
- **Next executable step:** review the contract and free integrated fixture together, then run the focused and repository validation once after lane integration.

## Independent claim boundaries

- **P — protocol portability:** protocol or harness is an explicit factor; retry, schema, tools, termination, budgets, compaction, model, preamble, adapter, and truncation coordinates are matched outside that factor; exactly one strict equivalence preregistration binds the metric, direction, primary k, and margin, while exactly one current curve supplies the primary-k paired interval used for containment.
- **R — frozen reliability:** heldout domains and environments, paired curve/cohort evidence, power, and both pass@k and pass^k are required. CI overlap, zero-crossing, non-significance, or a curve refusal remains inconclusive.
- **U — unfamiliar adaptation:** a pre-trace novelty certificate must match current registered heldout bytes and `TaskContamination`; reference-prompt borrowing, unresolved/known contamination, or knowledge unavailable in-world is invalid.
- **C — continual learning:** longitudinal phase JSON binds a unique byte digest, identity, phase index, and timestamp; the phase list must be strictly ordered and accompanied by longitudinal evidence, independently of U's unfamiliar-environment novelty boundary. C neither consumes nor implies a U result, and post-trace revision is invalid.
- **Y — production reliability:** production reliability evidence requires a raw `IntegrationCostLedger`; `added_loc` is the current nonblank/noncomment physical-line count across M052-owned new production modules, `modified_loc` is the same count only inside explicit `M052-INTEGRATION` blocks in pre-existing production modules, and `revisions` is one current sha256-bound source snapshot per measured production path (not commits or estimated edit rounds). Dependencies, environment-specific symbols, prompt tokens, and post-trace fixes remain separate raw fields; the ledger never produces a score.

## Source evidence and dependencies

M047 supplies executable factor provenance and empirical paired curves; M049 supplies exact-byte task certification; M048 supplies typed state facts; M051 supplies strict file-only upstream identities. M052 composes those boundaries but does not turn component checks into substantive generality evidence. No validation claim is made by this handoff.
