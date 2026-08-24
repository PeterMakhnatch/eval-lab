Status: done
Last: merged as PR #147 (`0ad6446`)
Next: none
Blockers: none

# M050 (D) — Lessons truth-boundary repair

## Contract

- **Outcome:** repair lessons eligibility, optional annotation boundaries, lineage, and deterministic freshness.
- **Lane / owner:** Research / PR #147 repair owner.
- **Exclusive lease:** `src/evallab/lessons.py`, `sql/lessons.sql`, `research/lessons.md`, `tests/test_lessons.py`, and the additive CI freshness gate.
- **Status:** merged as PR #147 at `0ad6446`.
- **Acceptance:** exception trials are excluded and counted; unannotated eligible trials remain in cohorts; excluded rows cannot receive powered intervals; regeneration from resolved lineage is byte-identical.
- **Next executable step:** none; the lease is spent.

## Source evidence and dependencies

PR #147 identified and repaired exception trials in capability denominators, an inner join that dropped unannotated trials, unresolved lineage, and the missing deterministic freshness gate. It merged as `0ad6446`.
