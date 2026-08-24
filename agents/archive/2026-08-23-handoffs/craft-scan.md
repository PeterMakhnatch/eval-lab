Status: done
Last: merged as PR #74 (`30b5e94`)
Next: none
Blockers: none

# CRAFT deterministic scan handoff (WS-A)

Implements the deterministic half of `docs/build-plan.md` WS-A.
Files:
- `src/evallab/craft.py`
- `sql/craft_views.sql`
- `docs/craft.md`
- `tests/test_craft.py`

## Verification
- `uv run pytest tests/test_craft.py` (51 passed in 0.24s)
- `uv run pytest` full suite (795 passed, 1 xfailed)
- `uv run ruff check .` (clean)
- `python -m evallab.craft scan --tb3` scans 74 tasks from `~/Developer/agent-evals/terminal-bench/tasks` idempotently
- `python -m evallab.craft scan --all-local` scans 551 total tasks across TB3 + `library/`
