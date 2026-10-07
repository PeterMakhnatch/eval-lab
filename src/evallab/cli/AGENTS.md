# CLI namespace

## Purpose
This directory is reserved. Production CLI lives in `src/evallab/cli.py`; do not create duplicate command routing here.

## What lives here / entry points
- Currently a reserved empty package; primary entry point is `src/evallab/cli.py`.

## Invariants or rules
- Expected usage failures have clean exits, not internal tracebacks.
- Keep expensive optional imports lazy and leaf handlers directly callable.
- Preserve deterministic registration/dispatch and the public command contract.

## Tests or checks
Relevant existing tests: `tests/test_cli_registry.py` and `tests/test_cli_audit.py`. The assigned verifier uses focused checks; see `agents/CHECKS.md` for delivery.

## What not to add here
No parallel CLI implementation or incidental package-layout migration.
