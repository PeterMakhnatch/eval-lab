# CLI Subsystem (src/evallab/cli/)

## Purpose
Reserved namespace for CLI entrypoints and command line interfaces.
The active CLI implementation lives in `src/evallab/cli.py`.

## What lives here / entry points
- Currently a reserved empty package; primary entry point is `src/evallab/cli.py`.

## Invariants or rules
1. Clean Exit Codes: Successful runs return code 0; invalid arguments or missing prerequisites return non-zero with informative stderr messages.
2. Fast Startup: The CLI parser must defer heavy package imports until subcommand dispatch.
3. Comprehensive Subcommand Dispatch: Every leaf parser must map to a callable handler without unhandled crashes.

## Tests or checks
- Targeted unit tests: `pytest tests/test_cli_registry.py tests/test_cli_audit.py`

## What not to add here
Do not add ad-hoc scripts or unmigrated commands here; package locations are frozen.
