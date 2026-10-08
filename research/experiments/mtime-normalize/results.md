# mtime-normalize@1 validation (2026-10-08, UTC, $0)

Transform: `mtime-normalize@1` (`src/evallab/mtime_normalize.py`, registered in
`src/evallab/hardening.py`). Every work-tree path gets one fixed timestamp
(`2000-01-01` local) at the end of setup, before the ready sentinel; the
setup fail-closes when any path stays newer. Unit tests:
`tests/test_mtime_normalize.py`.

## (a) Leak reproduced by registry layer streaming

`scan_mtimes.py` reuses the HAR-177 `Registry` client and iterates tar
headers only (names/mtimes/sizes recorded, no file bytes written). Both
images streamed via `mirror.gcr.io-stream`. Receipts: `receipts/scan-*.json`.

002402 (`sha256:6d706c…`, 36,293 members, 1 layer): `testbed` root has 418
files over 12 distinct mtimes. Bulk checkout sits at 22:49:11–12Z; **6 files
stand at 22:59:13Z**: `Makefile`, `numpyro/distributions/continuous.py`,
`discrete.py`, `truncated.py`, `test/test_distributions.py`, plus
`.git/ORIG_HEAD`. The layer also bakes a `.pytest_cache` (`lastfailed`,
`nodeids`, `stepwise`) and `numpyro.egg-info`. Note: the 22:59:13Z files are
not this task's fix (fix commit `56f63eb6` touches `batch_util.py`); they
still leak adjacent fix/test surface, and neither `git clean` (untracked
only) nor the strip `read-tree` (index only) rewrites tracked-file mtimes,
so the cluster reaches the agent.

002552 (`sha256:2dcbed…`, 28,921 members, 1 layer, control): `testbed` root
has 604 files over 9 mtimes inside a ~40 s window (bulk 320 files at
06:22:11Z, remainder 06:22:09–48Z build/test artifacts: `__pycache__`,
`.pytest_cache`). No fix-commit outlier. Only `.git` repack files (2026-09)
are later.

## (b) Transform applied in local Docker (Harbor 0.24, --env docker)

`run_mtime_validation.py` derives into an isolated `/tmp` store
(validation-only lineage) and stages Harbor packages with a read-only
post-setup mtime listing in `solution/solve.sh`:

- 002402 full chain snapshot → strip → purge → mtime (`4185c51a31d2`,
  markers verified in strip<purge<mtime order in the executed `setup.sh`):
  **oracle reward 1.0, nop reward 0.0**. In-run listing (before the oracle
  fix is applied): `mtime_distinct 1`, `mtime_newer_than_fixed_count 0`.
- 002552 control, snapshot → strip → mtime (`69fa067231db`): **oracle
  reward 1.0, nop reward 0.0**, `mtime_distinct 1`,
  `mtime_newer_than_fixed_count 0`. Unchanged behavior: oracle still passes,
  nop still fails; the transform is a behavioral no-op where no cluster
  existed.

Trial receipts: `receipts/collect-final.json` (4 trials, rewards
`1.0 / 0.0 / 1.0 / 0.0`).

Purge scoping note: `purge-installed-copies@1` is scoped to its
`CONFIRMED_PURGE` tasks and fail-closed on the poetry-backend miio tree
(`pip install -e --no-index` cannot import `poetry.core.masonry.api`
offline; the link fallback finds no worktree source), so the 002552 chain
is strip → mtime. The mtime block anchors on the ready sentinel, so its
runtime position — after strip, and after purge wherever purge applies
(002402 proves the 3-chain) — holds by construction.

## Prevalence gap

Two images are not a census: 002402 shows the leak shape, 002552 does not.
Corpus-wide mtime prevalence is FullLeakScan's job, not this receipt's.
