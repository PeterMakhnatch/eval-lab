Status: done
Last: merged as PR #77 (`abae7b2`)
Next: none
Blockers: none

# LADDER Grid Generator Handoff (WS-E Item 3)

Implements the LADDER evaluation grid generator (`docs/build-plan.md` WS-E item 3):

1. **Grid Generation Engine (`src/evallab/ladder.py`):**
   - CLI entrypoint: `python -m evallab.ladder generate <grid_spec.yaml> [-o queue_dir]`
   - Cartesian expansion across $tasks \times agents \times preambles \times k\text{ attempts}$.
   - Resolves builtin agent profiles (e.g. `codex-gpt-5.6-terra`).
   - Validates generated `ExperimentSpec` files containing required `purpose` (default: `elicitation` or `comparison`).
   - Respects per-provider quotas, subscription headroom (`evallab.quota`), and batch limits (`max_specs`, `max_trials`, `max_cost_usd`) without overflowing.

2. **Verification & Tests (`tests/test_ladder.py`):**
   - 24 unit and integration tests covering:
     - Slug sanitization and deterministic naming ($\le 80$ characters, regex conformant).
     - YAML and JSON parsing with schema validation.
     - Full Cartesian grid expansion and hypothesis templating.
     - Quota headroom integration and paid agent pruning on exhaustion.
     - Global and per-provider batch limits (specs, trials, dollar cost).
     - CLI execution and file writing to queue directories.

3. **Documentation:**
   - `docs/ladder.md`: Architecture, schema definition, quota enforcement rules, CLI usage, and Python API.

## Verification
- `uv run pytest tests/test_ladder.py` (24 passed)
- `uv run pytest` (902 passed, 1 xfailed)
- `uv run ruff check .` (clean)
- `python -m evallab.ladder generate` tested via subprocess and CLI invocation
