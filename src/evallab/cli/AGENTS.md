# CLI Subsystem

This directory is a **reserved empty package**. The CLI lives in
`src/evallab/cli.py`. Do not add modules here; package locations are frozen.

## Invariants (enforced on `cli.py`)
1. Clean Exit Codes: Successful runs return code 0; invalid arguments or missing prerequisites return non-zero with informative stderr messages.
2. Fast Startup: The CLI parser must defer heavy package imports until subcommand dispatch.
3. Comprehensive Subcommand Dispatch: Every leaf parser must map to a callable handler without unhandled crashes.

## Testing & Verification
- Targeted unit tests: `pytest tests/test_cli_registry.py tests/test_cli_audit.py`
