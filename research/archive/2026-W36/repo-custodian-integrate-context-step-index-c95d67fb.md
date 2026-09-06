# Repo Custodian — integrate approved context operation step index

## Exact target

- Canonical integration worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Branch: `analyst/synth-data-promotion-hardening`
- Required clean starting head: `770488cfb1c5318c6b1f39d386c2e251d8864487`
- Approved source worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/context-operation-step-index`
- Approved source branch/head: `feat/context-operation-step-index @ c95d67fb03ac1dab6b1d47d0c06e4c46d47a62aa`
- Approved source base: `46794c166c39cb64c7c41350f8906b5ec3badab1`
- Approved source stack: `051a5f40` → `b17e7891` → `fa73aa46` → `c95d67fb`
- Architecture reports:
  - `/tmp/system-architect-context-operation-step-index-review-051a5f40.md`
  - `/tmp/system-architect-context-payload-digest-final-review-fa73aa46.md`
  - `/tmp/system-architect-context-index-container-rereview-c95d67fb.md`

Never edit the dirty root, run a model, or integrate unrelated work.

## Exact four-path source scope

The approved range `46794c16..c95d67fb` touches exactly:

1. `src/evallab/interpretation/producers/memory_continuity.py`
2. `src/evallab/semantic_facts.py`
3. `tests/test_memory_continuity_producer.py`
4. `tests/test_semantic_facts.py`

Replay the approved semantics onto current canonical head `770488cf`; do not merge/cherry-pick the old base wholesale. Use current canonical files as the base for overlaps and preserve isolation/admissibility, external-lineage, resolver, and action-memory changes.

## Required contract

- Reuse `ContextOperationFact.step_index` as the single shared per-step total-order coordinate. Do not create a second ordinal.
- Derive operation ordering from real trajectory order, not lexical operation names, IDs, sorted maps/sets, or output container iteration.
- Preserve source order for forgotten indexes; ambiguous/unordered containers and missing/duplicate order must produce typed unavailable/refusal rather than guessed order.
- Payload digest is domain-separated and exact-type-only. Reject bool-as-int coercion, non-finite floats, unsupported nested mappings/containers, and unordered sets/frozensets.
- Producer mapping and semantic-fact validation must agree on the exact operation identity/order/payload digest contract.
- Preserve every newer canonical top-level symbol and test. Append/adapt approved tests; do not replace broader canonical suites.

## Verification

At the integrated exact head run:

- `uv run pytest tests/test_memory_continuity_producer.py tests/test_semantic_facts.py`
- focused regressions for any overlapping current canonical consumers if the diff shows them;
- touched Ruff check and format check;
- `git diff --check`;
- exact four-path diff and top-level test/symbol preservation audit.

Commit cleanly. Page `wH:p9` with old/new exact heads, commit, conflict/resolution notes, changed-path/stat, test counts, clean status, and no-model/no-root-edit confirmation.
