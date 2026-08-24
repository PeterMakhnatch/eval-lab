Status: done
Last: merged executable factor provenance in PR #149 (`1c220c6`) and the empirical curve in PR #150 (`8ea9f8b`)
Next: none; lease spent
Blockers: none

# M047 (A) — Factor execution provenance and empirical paired curve

## Contract

- **Outcome:** execute declared factor points with immutable identity/provenance, then derive an empirical paired curve.
- **Lane / owner:** Research / Research lane owner.
- **Exclusive lease:** additive factor-provenance fields in `src/evallab/schemas.py`, `src/evallab/ladder.py`, `src/evallab/queue.py`, `src/evallab/facts.py`, `src/evallab/cohort.py`, their SQL/Parquet projection schemas and focused tests; curve files remain leased but untouched until this dependency slice lands.
- **Status:** merged via PRs #149 and #150; lease spent.
- **Acceptance:** implemented: declared factors retain immutable provenance; curve specs distinguish execution-bound from task-generator factors and preregister one primary task-block-paired contrast over ordered authoritative coordinates; every level composes the cohort engine, reports task-clustered pass@k/pass^k, pairs/censoring/exceptions, and refuses sparse, unpaired, mislabeled, mixed, mismatched, or zero-crossing primary inference without emitting a fitted or aggregate score. A valid refuse-to-rank artifact is a successful scientific CLI result; malformed or missing inputs still exit nonzero.
- **Next executable step:** none.

## Source evidence and dependencies

PR #146's `ladder.py` carries factor coordinates into generated specs. M045/#140 supplies staged cohorts and M030/#142 supplies trajectory facts. PRs #149 and #150 merged factor provenance and the empirical curve before M052/#155 consumed them; M048 and M051 remain independent evidence sources.
