Status: building
Last: Implemented and focused-tested ATIF/Parquet, deterministic facts, causal cohort comparison, and immutable stub-analysis sidecars with catalog joins.
Next: Run the complete acceptance matrix, rebase origin/main, resolve only owned-path changes, then push and open the ANALYST PR.
Blockers: Full-tree Ruff has nine pre-existing failures in CURATOR/RECON-owned files; docs/prompts/overnight-missions.md is absent from origin/main, so its last committed version (53dd823) supplied the work order.

The checked-out tree is mid-migration: reviewed controls remain under
`evidence/runs/`, while the mission names `research/evidence/runs/`. The
implementation will discover both without moving BUILDER-owned evidence.

Focused verification: 23 tests passed before documentation additions. The
tracked Oracle/no-op comparison is 1.0 vs 0.0 with n=1 Wilson intervals and one
paired task. PostgreSQL schema init was idempotent; two raw controls ingested;
one saved Oracle sidecar indexed with its digest, finding, and citation. No live
model or Harbor benchmark was invoked.
