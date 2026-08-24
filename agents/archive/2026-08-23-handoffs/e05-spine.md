Status: done
Last: merged as PR #93 (`f19182c`)
Next: none
Blockers: none

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