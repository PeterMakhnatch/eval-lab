# Audit Evidence: status/status_generator

- **Subject**: `status/status_generator`
- **Handoff Audited**: `agents/handoffs/storm-status.md` / `src/evallab/status_generator.py`
- **Audit Date**: 2026-08-18
- **Auditor**: M015 (LOOP-AUDIT)

## Claims Extracted from Handoff
1. `src/evallab/status_generator.py` implements status markdown generation projecting catalog, queue, and `PROGRAM.json` state.
2. `tests/test_status_generator.py` passes unit tests (claimed 7 tests, currently 9 tests).
3. Live status projection contains structured sections:
   - `RECENT (Yesterday: YYYY-MM-DD)`
   - `RUNNING NOW`
   - `NEXT`
   - `TASK DECISIONS`
   - `SYSTEM HEALTH & OPERATIONAL SMOKE`
4. Idempotent disk updates via `update_status_file`.

## Re-run & Reproduction Commands
```bash
# 1. Run unit and idempotency tests for status_generator
uv run pytest tests/test_status_generator.py

# 2. Re-generate status markdown live from repo state
uv run python -c "
from pathlib import Path
from evallab.status_generator import generate_status_markdown
print(generate_status_markdown(Path('.')))
"

# 3. Check CLI and destination file locations
test -f research/experiments/STATUS.md && echo "research/experiments/STATUS.md exists"
test -f docs/STATUS.md || echo "docs/STATUS.md does not exist"
```

## Captured Outputs & Verdict
1. `uv run pytest tests/test_status_generator.py`:
   Output: `9 passed in 0.49s` (CONFIRMED)
2. Live markdown projection:
   Generated 5,625 characters of status markdown summarizing 3 tasks, open program decisions from `PROGRAM.json`, queue status, and health (CONFIRMED)
3. Integration & Destination Drift:
   - `status_generator.py` writes to `research/experiments/STATUS.md` by default.
   - `docs/STATUS.md` does not exist.
   - `evallab` CLI has `evallab status` (terminal snapshot via `src/evallab/status.py`) but no CLI entrypoint for `status_generator` / `docs/STATUS.md`.
   - Verdict: **CONFIRMED** for the generator logic and tests; target path divergence (`research/experiments/STATUS.md` vs `docs/STATUS.md`) recorded.

## Risk Note
`status_generator.py` targets `research/experiments/STATUS.md` by default while documentation expectations anticipate `docs/STATUS.md`, and no top-level CLI command generates the markdown file directly.
