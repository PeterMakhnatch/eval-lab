# CLI Subsystem (src/evallab/cli/)

## Responsibilities
Provides the terminal CLI interface (`evallab <subcommand>`) and subcommands for runners, data backfills, and tidy operations.

## Core Invariants
1. Golden Surface Preservation: Any CLI argument or command addition must be accompanied by a regeneration of `tests/golden/cli_surface.json`.
2. Clean Exit Codes: Successful runs return code 0; invalid arguments or missing prerequisites return non-zero with informative stderr messages.
3. Fast Startup: The CLI parser must defer heavy package imports until subcommand dispatch.

## Testing & Verification
- Targeted unit tests: `pytest tests/test_cli_registry.py tests/test_golden_rendering.py`
