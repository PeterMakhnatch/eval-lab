# Evallab Data Assets (src/evallab/data/)

## Purpose
Static data assets, package manifests, and task migration definitions bundled
directly with the evallab library distribution.

## What lives here / entry points
- `fastmcp-3.4.7-cp312-manylinux_2_17_x86_64-manifest.json`: FastMCP runtime distribution manifest.
- `terminal-bench-4-migration.json`: Terminal-bench migration mappings.

## Invariants or rules
1. Static and read-only: files here are immutable bundled package assets loaded at runtime.
2. No code: executable logic belongs in `src/evallab/`, not in this data directory.
3. Size bounds: only compact schema and metadata definitions belong in package data.

## Tests or checks
- Verified during package build and asset loading in test runs:
  `pytest tests/test_fastmcp.py tests/test_terminal_bench.py`

## What not to add here
Do not place runtime caches, large binary datasets, model weights, or scratch files here.
