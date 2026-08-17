Status: complete
Last: generators declare their upstream inputs in front-matter with SHA-256 digests; lineage graph resolves deterministically
Next: author evaluation cards with declared lineage inputs; expand recursive provenance to pipeline sidecars
Blockers: none

## Summary
Updated all three core repository generators to declare their input dependencies in YAML front-matter conforming to the `inputs: [{path, digest}]` schema read by `evallab.lineage`:

1. `src/evallab/docindex.py` (`docs/INDEX.md`): Declares every indexed markdown document under `docs/` and `docs/research/` (excluding `INDEX.md` itself) with its `sha256:` content digest.
2. `src/evallab/repomap.py` (`docs/repo-map.md`): Declares every discovered Python module in `src/evallab/` (including `cli.py`), sorted deterministically by path with its `sha256:` content digest.
3. `src/evallab/lessons.py` (`research/lessons.md`): Declares the SQL view file (`sql/lessons.sql`), craft task manifests, observation records, analysis sidecars, and trial facts Parquet partitions.

## Determinism & Convergence Rules
- Documented in `docs/lineage.md` §7:
  - Strict DAG structure: `src/evallab/*.py` -> `docs/repo-map.md` -> `docs/INDEX.md` ensures single-pass convergence without cyclic digest chasing.
  - Partition convention: Small partition sets ($\le 100$) record all member files individually; large sets ($> 100$) record the glob pattern with a composite SHA-256 digest computed over the sorted member digests.

## Verification
- Unit and regression tests added across `tests/test_docindex.py`, `tests/test_repomap.py`, and `tests/test_lessons.py` asserting non-empty inputs, byte-identical consecutive generation, fixture digest correctness, and lineage resolution.
- Two consecutive generations confirmed byte-identical convergence:
  - `python -m evallab.repomap check` passed
  - `python -m evallab.docindex check` passed
- Full test suite: 1101 passed, 2 skipped, 1 xfailed.
- `ruff check .` clean.
- `ty` typecheck: 28 diagnostics (within limit).
- `evallab lineage docs/repo-map.md` resolves all 48 input modules.
