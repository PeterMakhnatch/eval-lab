Status: review-wanted
Last: implemented E02 grid files, ladder CLI, resume-not-duplicate expansion, provider round-robin ordering, and withholding report; full test suite passing (1098 passed); documentation and repo-map generated.
Next: integrator review; do not merge.
Blockers: none.

## E02: grid files, ladder CLI, and resume-not-duplicate expansion

Worktree: `.worktrees/e02-ladder` (branch `role/e02-ladder`)

### Key Deliverables

1. **`src/evallab/schemas.py`**:
   - Added typed models conforming to v2 §4: `GridAxes`, `GridSpec`, `LadderGridSpec`, `TaskSpec`, `AgentSpec`, `ProviderLimit`, `GridLimits`.
   - Fields: `axes: {task_refs[], agents[], preamble: [hash…], k: [1,3,5]}`, `constraints`, `purpose` (required `ExperimentPurpose`), `daily_budget_units`.
   - Added `grid_id` and `grid_point` coordinates to `ExperimentSpec`.

2. **`src/evallab/ladder.py`**:
   - Grid specification expansion into validated `ExperimentSpec` items minus exclusion constraints.
   - **Deduplication & Resume**: `find_existing_grid_points` scans queue state directories across all states; dedupe key `grid_id + coordinates` ensures re-running on a partially-run grid emits only missing points and zero duplicates.
   - **Provider Round-Robin**: Interleaves candidate points across providers to balance quota consumption.
   - **Quota & Daily Budget Enforcement**: Respects `daily_budget_units` and provider quota headroom; withholding report provides explicit reasons for all withheld points.
   - Default dry-run mode printing expansion and dedupe decisions; `--submit` writes directly to queue.

3. **`src/evallab/cli.py` & `tests/test_cli_audit.py`**:
   - Registered `evallab ladder generate <grid.yaml>` parser and command handler.
   - Added `ladder` to `TOP_LEVEL_COMMANDS` in parser-registration order and `("ladder", "generate")` to `NESTED_COMMANDS`.

4. **`grids/event-summary-elicitation.yaml`**:
   - Shipped valid and minimal example grid adhering to the v2 §4 format.

5. **`docs/ladder.md` & Indexes**:
   - Extended `docs/ladder.md` documenting the v2 §4 grid schema, dedupe key, resume semantics, provider round-robin, withholding report, and CLI options.
   - Regenerated `docs/INDEX.md` and `docs/repo-map.md`.

6. **`tests/test_ladder.py`**:
   - 31 unit and integration tests covering Cartesian expansion, constraint filtering, dedupe/resume on partial grids, daily budget units truncation with withholding report, rejection of grids missing purpose, dry-run neutrality (directory digest verification), byte-identical deterministic output, and CLI execution.

### Verification

```bash
uv run pytest tests/test_ladder.py
# 31 passed in 0.43s

uv run pytest tests/test_cli_audit.py
# 53 passed in 0.37s

uv run pytest
# 1098 passed, 2 skipped, 1 xfailed in 34.41s

uv run ruff check .
# All checks passed!

uvx ty@0.0.71 check src/ --output-format=concise 2>&1 | tail -2
# Found 28 diagnostics (at ratchet <= 28)

uv run python -m evallab.repomap check
# repomap check passed

uv run python -m evallab.docindex check
# docindex check passed

uv run evallab ladder generate grids/event-summary-elicitation.yaml --dry-run
# LADDER Grid Generation: 12 specs generated for 'grid-event-summary-elicitation', 20 total trials, $0.00 est. cost.
```
