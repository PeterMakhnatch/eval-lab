Status: done
Last: merged as PR #95 (`902fcb7`)
Next: none
Blockers: none

## Summary
Implemented `src/evallab/lineage.py` and wired the `evallab lineage <path|id>` subcommand into `src/evallab/cli.py`.
- Resolves artifact provenance recursively, stopping at Zone 1 (immutable evidence).
- Reads the `inputs: [{path|id, digest}]` contract from Z4 markdown front-matter, JSON sidecars, and Parquet metadata.
- Reports missing input declarations honestly as `unrecorded` without guessing edges.
- Validates SHA-256 content digests at each hop, failing on `digest_mismatch`.
- Detects cycles and bounds traversal depth.
- Reads derived metadata via the DuckDB attach surface (`evallab.attach.attach`).
- Emits both human tree and deterministic byte-identical `--json` output.
- Documented in `docs/lineage.md` with regenerated `docs/INDEX.md` and `docs/repo-map.md`.

## Verification
- `uv run pytest tests/test_lineage.py` passes (12 tests covering chains, digests, unrecorded, cycles, determinism, missing targets, CLI).
- Full test suite passes.
- `uv run ruff check .` clean.
- `uvx ty@0.0.71 check src/` clean (28 diagnostics, within budget).
- `repomap` and `docindex` generate and check cleanly.
