Status: ready
Last: registered the factor execution/provenance and paired-curve acceptance contract
Next: implement the provenance-bearing execution record and smallest paired fixture before the curve renderer
Blockers: none

# M047 (A) — Factor execution provenance and empirical paired curve

## Contract

- **Outcome:** execute declared factor points with immutable identity/provenance, then derive an empirical paired curve.
- **Lane / owner:** Research / Research lane owner.
- **Exclusive lease:** `src/evallab/factor_curve.py` (new), `tests/test_factor_curve.py` (new), `research/experiments/factor-curves/**`, and additive factor-provenance fields in `src/evallab/ladder.py`, `src/evallab/schemas.py`, and their focused tests.
- **Status:** ready; factor execution/provenance precedes curve rendering.
- **Acceptance:** every executed trial joins to grid, point, arm, factor values, and immutable input digests. A paired fixture renders per-level counts, estimates, uncertainty, and paired deltas from those facts. Missing pairs are excluded and reported, never imputed.
- **Next executable step:** implement the provenance-bearing execution record and fixture before any curve renderer.

## Source evidence and dependencies

PR #146's `ladder.py` carries factor coordinates into generated specs, while no committed paired empirical curve exists. M045/#140 supplies staged cohorts. M030/#142 supplies trajectory facts. No dependency on M048 or M051; M052 waits for this mission and M049.
