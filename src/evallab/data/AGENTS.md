# Bundled data assets

## Purpose
Small static package assets: the pinned FastMCP wheel manifest and Terminal-Bench 4 migration metadata.

## What lives here / entry points
- `fastmcp-3.4.7-cp312-manylinux_2_17_x86_64-manifest.json`: FastMCP runtime distribution manifest.
- `terminal-bench-4-migration.json`: Terminal-bench migration mappings.

## Invariants or rules
Assets are read-only at runtime. Source updates are reviewed/versioned with their consuming code when required by the assigned change. Do not embed executable policy in data files.

## Tests or checks
The assigned verifier checks consumers of the changed manifest and package inclusion; use the existing tests that exercise the changed consumer.

## What not to add here
Not a cache, dataset download, weights directory, or runtime workspace.
