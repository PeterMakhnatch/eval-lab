Status: review-wanted
Last: completed executable factor binding, canonical point identity, run/fact/cohort provenance, legacy-null migration, and focused hard-gate regressions
Next: review and merge the factor-provenance dependency slice, then implement the empirical paired curve on its projected task-block coordinates
Blockers: none

# M047 (A) — Factor execution provenance and empirical paired curve

## Contract

- **Outcome:** execute declared factor points with immutable identity/provenance, then derive an empirical paired curve.
- **Lane / owner:** Research / Research lane owner.
- **Exclusive lease:** additive factor-provenance fields in `src/evallab/schemas.py`, `src/evallab/ladder.py`, `src/evallab/queue.py`, `src/evallab/facts.py`, `src/evallab/cohort.py`, their SQL/Parquet projection schemas and focused tests; curve files remain leased but untouched until this dependency slice lands.
- **Status:** review; factor execution/provenance is complete and curve rendering remains next.
- **Acceptance:** complete for this dependency slice: declared factors bind allowlisted execution fields or fail closed; canonical grid/point/arm/factor/binding and task-block provenance survives execution into facts/cohorts; legacy rows remain nullable; preamble controls and treatment content stay distinguishable. The remaining curve slice must render per-level counts, estimates, uncertainty, and paired deltas, excluding and reporting missing pairs without imputation.
- **Next executable step:** review and merge factor provenance, then build the empirical paired curve from the projected coordinates.

## Source evidence and dependencies

PR #146's `ladder.py` carries factor coordinates into generated specs, while no committed paired empirical curve exists. M045/#140 supplies staged cohorts. M030/#142 supplies trajectory facts. No dependency on M048 or M051; M052 waits for this mission and M049.
