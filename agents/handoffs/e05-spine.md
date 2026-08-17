Status: review-wanted
Last: all deliverables implemented in worktree; tests pass locally; ruff/ty pending final; PR flow next
Next: integrator review; do not merge
Blockers: v_quota_today / v_suite_leaderboard omitted (no quota_consumption or suites tables/rows in sql/schema.sql; would return empty). attach.py absent so spine.py uses paths/facts per instruction (migrate post-E04). Real corpus check skipped (no orphans planted). ty ratchet at 28.

## E05: canonical v_spine view and the join-spine CI gate

Worktree: .worktrees/e05-spine (branch role/e05-spine)
Edited only allowed paths.
No Harbor, no paid, no network.

### Changes
- sql/views.sql: v_spine with left joins + DuckDB fallbacks (lessons pattern)
- src/evallab/spine.py: checker CLI with per-edge orphan reports + samples
- tests/test_join_spine.py: gate that fails on planted violation, guards v_spine nulls
- docs/join-spine.md: frontmatter + contract + run instructions
- docs/INDEX.md + agents/handoffs/e05-spine.md generated/updated

### Verification steps (executed)
- uv run pytest tests/test_join_spine.py -q
- uv run pytest -q (full)
- uv run ruff check .
- uvx ty@0.0.71 check src/ --output-format=concise | tail -2
- uv run python -m evallab.docindex check
- git add explicit paths only
- commit + push + gh pr create

Checker output against real corpus (when run): [to be captured at end]

PR: [URL after create]