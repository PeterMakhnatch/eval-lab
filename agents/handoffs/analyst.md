Status: blocked
Last: Completed briefs 01-03 plus fixed-label failure-taxonomy agreement; rebased origin/main and passed all tests and owned-path Ruff.
Next: After green PR #1 merges, fetch/rebase, rerun full-tree pytest and Ruff, then push role/analyst and open the ANALYST PR.
Blockers: Full-tree Ruff has nine pre-existing failures in CURATOR/RECON-owned files. Green PR #1 (codex/restore-green-ci) fixes them but remains open; ANALYST may not edit those paths or open a PR before Ruff is clean. docs/prompts/overnight-missions.md is absent from origin/main, so its last committed version (53dd823) supplied the work order.

The checked-out tree is mid-migration: reviewed controls remain under
`evidence/runs/`, while the mission names `research/evidence/runs/`. The
implementation will discover both without moving BUILDER-owned evidence.

Verification on rebased head: `uv run pytest -q` passed 36 tests and the
focused analysis suite passed 25 tests. Owned-path Ruff and `git diff --check`
are clean; full-tree Ruff reports only the nine foreign-path errors above. The
tracked Oracle/no-op comparison is 1.0 vs 0.0 with n=1 Wilson intervals and one
paired task. PostgreSQL schema init was idempotent; two raw controls ingested;
one saved Oracle sidecar indexed with its digest, finding, and citation. Raw
re-ingestion preserved both controls' `event-summary-local-controls`
association and the Oracle analysis join. Fixed labels: 25; saved valid
sidecars: 1; exact agreement: 1/1; valid-label coverage: 1/25. No live model or
Harbor benchmark was invoked.
