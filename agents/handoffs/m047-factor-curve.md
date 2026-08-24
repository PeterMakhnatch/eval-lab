Status: review-wanted
Last: closed curve hard-gate review with authoritative factor coordinates/kinds, exact timeout treatment semantics, preserved cohort refusals, and successful scientific-refusal CLI exits
Next: review the repaired empirical curve slice for merge
Blockers: none

# M047 (A) — Factor execution provenance and empirical paired curve

## Contract

- **Outcome:** execute declared factor points with immutable identity/provenance, then derive an empirical paired curve.
- **Lane / owner:** Research / Research lane owner.
- **Exclusive lease:** additive factor-provenance fields in `src/evallab/schemas.py`, `src/evallab/ladder.py`, `src/evallab/queue.py`, `src/evallab/facts.py`, `src/evallab/cohort.py`, their SQL/Parquet projection schemas and focused tests; curve files remain leased but untouched until this dependency slice lands.
- **Status:** review-wanted; executable factor provenance and the empirical paired curve are implemented.
- **Acceptance:** implemented: declared factors retain immutable provenance; curve specs distinguish execution-bound from task-generator factors and preregister one primary task-block-paired contrast over ordered authoritative coordinates; every level composes the cohort engine, reports task-clustered pass@k/pass^k, pairs/censoring/exceptions, and refuses sparse, unpaired, mislabeled, mixed, mismatched, or zero-crossing primary inference without emitting a fitted or aggregate score. A valid refuse-to-rank artifact is a successful scientific CLI result; malformed or missing inputs still exit nonzero.
- **Next executable step:** review this repaired curve slice for merge.

## Source evidence and dependencies

PR #146's `ladder.py` carries factor coordinates into generated specs. M045/#140 supplies staged cohorts and M030/#142 supplies trajectory facts. The curve implementation composes those cohort comparisons over `task_block_id`; its depth fixture records explicit factor/block provenance but is contract-enforcement evidence only because depth generation is not yet an executable Grid binding. No dependency on M048 or M051; M052 waits for this mission and M049.
