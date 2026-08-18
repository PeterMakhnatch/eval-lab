Status: building
Last: Cycle 1: Wire status CLI entrypoint, generate first docs/STATUS.md, and add golden test skeleton
Next: Cycle 2: Regeneration hook in automation nightly and determinism proof (byte-identical shasums)
Blockers: none

# M016 - LOOP-SURFACE: the generated surfaces actually generate

## Cycle 1 Log

### 1. RECHECK
- Baseline commit: `f836f6c` (PR #116 / `origin/main`).
- Ran baseline premerge checks: `scripts/premerge.sh` passed with 1271 passed tests, clean ruff, and ty 28 diagnostics.

### 2. EXTEND
- Set default status generation destination to `docs/STATUS.md` in `src/evallab/status_generator.py`.
- Added `--generate`, `--update`, `--target-date`, and `--output` CLI options to `evallab status` entrypoint in `src/evallab/cli.py`.
- Added quiet vs active vs unavailable 3-state discrimination for storm alarms in `status_generator.py`.
- Generated the first real `docs/STATUS.md` from live catalog and queue state.
- Regenerated `docs/INDEX.md` and `docs/repo-map.md`.

### 3. PROVE
Command output from generating `docs/STATUS.md` via `uv run evallab status --update`:
```
updated: /Users/petermakhnatch/Developer/eval-lab/.worktrees/m016-surface/docs/STATUS.md
```

Command output from full pytest suite:
```
=========================== short test summary info ============================
1275 passed, 1 skipped, 1 xfailed in 53.35s
```

### 4. HARDEN
- Added `test_status_rendering_matches_golden`, `test_status_rendering_is_stable_across_two_regenerations`, and `test_status_rendering_storm_quiet_vs_active_vs_unavailable` in `tests/test_golden_rendering.py`.
- Added golden reference file `tests/golden/status.md`.

### 5. RECORD
- Premerge verification: `bash scripts/premerge.sh` (pass).
- Committed and pushed Cycle 1.
