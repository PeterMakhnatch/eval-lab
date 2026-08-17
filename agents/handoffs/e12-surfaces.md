Status: review-wanted
Last: wired storm alarms into digest.py with storm_loader seam and quiet/unavailable distinctions, wired STATUS.md into NightlyCycle as an idempotent step, updated golden rendering, added comprehensive tests, and created docs/surfaces.md.
Next: peer review / integrator review; do not merge.
Blockers: none.

## E12: Wire Storm Alarms into the Digest and STATUS into the Nightly Path

Branch: `role/e12-surfaces` (worktree: `.worktrees/e12-surfaces`)

### Summary of Changes

1. **`src/evallab/digest.py`**:
   - Added `StormLoader = Callable[[date], Sequence[StormAlarm]]` seam for injection.
   - Updated `DigestRenderer.__init__` to accept `storm_loader`, defaulting to `_load_storm_alarms`.
   - Wired `## Storm alarms` section into `DigestRenderer.write` following §9 section order (after `## Queue events` and before run-bytes comment footer).
   - Distinguishes between healthy quiet corpus (`- Status: quiet (no reason_code storm detected in 1h window)`), active storm alarm table, and degraded unavailable loader (`- Unavailable: storm alarms could not be evaluated (<error>). That is not a statement that no event storm occurred.`).

2. **`src/evallab/automation.py`**:
   - Added `StatusUpdater = Callable[[date], Path]` seam.
   - Added `status_path: Path | None = None` to `NightlyResult`.
   - Wired STATUS.md generation into `NightlyCycle.run` as an ordered step after digest rendering and enrichment.
   - Defaults to invoking `update_status_file(self.renderer.repo_root, target_date=day)` with failure recording as `status_generation_failed` events.

3. **`src/evallab/storm.py`**:
   - Improved `load_events_from_source` to handle `Path` inputs and generic `Sequence[QueueEvent]` objects.

4. **Tests & Golden Files**:
   - `tests/test_digest.py`: Verified quiet rendering, injected storm alarms table rendering, unavailable source error reporting, section ordering, and default queue event loading.
   - `tests/test_storm.py`: Added tests for `load_events_from_source` across input types.
   - `tests/test_status_generator.py`: Verified that `NightlyCycle` invokes STATUS generation, that repeated runs yield byte-identical outputs, and that updater failures record events properly.
   - `tests/golden/digest.md`: Regenerated golden file; diff is strictly the addition of the `## Storm alarms` quiet section.

5. **Documentation**:
   - `docs/surfaces.md`: Created living documentation covering section order, storm alarm states (quiet, unavailable, active), and STATUS.md lifecycle/idempotency.
   - `docs/INDEX.md` and `docs/repo-map.md`: Regenerated and validated with docindex/repomap checkers.

### Golden Diff

```diff
diff --git a/tests/golden/digest.md b/tests/golden/digest.md
index 4b3f91c..fb6ff2c 100644
--- a/tests/golden/digest.md
+++ b/tests/golden/digest.md
@@ -136,4 +136,8 @@ A run of consecutive events identical in event, job, and policy/reason collapses
 | 2026-08-15T19:00:00+00:00 | dispatch_started | canary-event-summary-codex | canary |
 | 2026-08-16T11:30:00+00:00 | nightly_quarantined |  | headless_doctor_failed:docker_reachable |
 
+## Storm alarms
+
+- Status: quiet (no reason_code storm detected in 1h window)
+
 <!-- run-bytes: 1013 -->
```

### Verification Commands Run

```bash
uv run pytest tests/test_storm.py tests/test_status_generator.py tests/test_digest.py
uv run pytest tests/test_golden_rendering.py
uv run pytest
uv run ruff check .
uvx ty@0.0.71 check src/ --output-format=concise 2>&1 | tail -2
uv run python -m evallab.repomap generate -o docs/repo-map.md
uv run python -m evallab.repomap check
uv run python -m evallab.docindex generate -o docs/INDEX.md
uv run python -m evallab.docindex check
uv run evallab digest --help
```
