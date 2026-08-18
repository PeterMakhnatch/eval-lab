Status: building
Last: Cycle 3: Added board note for lessons, wired DISCOVERIES.md links in DigestRenderer with full test coverage
Next: Cycle 4: Final golden-file coverage and night verification across all generated surfaces
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
- Committed and pushed Cycle 1. PR: https://github.com/PeterMakhnatch/eval-lab/pull/119

## Cycle 2 Log

### 1. RECHECK
- Rechecked Cycle 1 acceptance with `scripts/premerge.sh`: passed (1275 passed, clean ruff, ty 28 diagnostics).

### 2. EXTEND
- Verified that STATUS refreshes on the existing nightly via `status_update` step in `NightlyCycle` (`src/evallab/automation.py`), targeting `docs/STATUS.md`.
- Evaluated determinism across consecutive generations on live repo data.

### 3. PROVE
Exact shasum comparison command and output proving byte-identical determinism:
```
Run 1 SHA256: 5588fafe1a3eaf0e7eeb235a7a31546f70fa6d99ce831ab6b13ced9f8e503555  -
Run 2 SHA256: 5588fafe1a3eaf0e7eeb235a7a31546f70fa6d99ce831ab6b13ced9f8e503555  -
PROVEN: Generations are BYTE-IDENTICAL
```

Full pytest summary line:
```
1277 passed, 1 skipped, 1 xfailed in 55.60s
```

### 4. HARDEN
- Added `test_status_update_file_default_path` and `test_status_generator_sha256_byte_identity` in `tests/test_status_generator.py`.
- Verified that `NightlyCycle.run` produces `docs/STATUS.md` directly and is byte-identical across multiple invocations on unchanged state.

### 5. RECORD
- Premerge verification: `bash scripts/premerge.sh` (pass).
- Committed and pushed Cycle 2. PR: https://github.com/PeterMakhnatch/eval-lab/pull/119

## Cycle 3 Log

### 1. RECHECK
- Rechecked Cycle 2 acceptance with `scripts/premerge.sh`: passed (1277 passed, clean ruff, ty 28 diagnostics).

### 2. EXTEND
- Verified that preflight section and storm-alarm sections already exist in `DigestRenderer`.
- Checked `lessons.py` on `origin/main` for `lessons_digest_section()`: confirmed absent. Created `research/audits/board-notes.md` recording that `lessons.py` was not touched (M019's lease) and deferred lessons digest rendering until exported.
- Extended `DigestRenderer` with `parse_discoveries_awaiting_verdicts` and a read-only rendered `## Discoveries awaiting verdict` section linking `digests/DISCOVERIES.md` entries (e.g. `[**D-20260815-KTXJSHGZ**](DISCOVERIES.md#d-20260815-ktxjshgz)`).
- Regenerated `docs/repo-map.md` and `docs/INDEX.md`.

### 3. PROVE
Command output from running pytest covering the new digest discoveries parser and rendering:
```
1281 passed, 1 skipped, 1 xfailed in 69.61s (0:01:09)
```

### 4. HARDEN
- Added unit tests in `tests/test_digest.py`:
  - `test_digest_renders_pending_discoveries_with_links`
  - `test_digest_renders_empty_discoveries_quietly`
  - `test_digest_renders_unavailable_discoveries_when_loader_raises`
  - `test_parse_discoveries_awaiting_verdicts_from_file`
- Updated `tests/test_golden_rendering.py` and `tests/golden/digest.md`.

### 5. RECORD
- Premerge verification: `bash scripts/premerge.sh` (pass).
- Committed and pushed Cycle 3. PR: https://github.com/PeterMakhnatch/eval-lab/pull/119
