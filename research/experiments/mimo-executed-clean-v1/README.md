# mimo-executed-clean-v1 — dataset card

**A pool defined by executed evidence, not by static-screen silence.**
The hackbook v0 screen in mimo-defect-map-v1 fires TS04/TS05 on all 567
tasks, so a no-hit screen pool is 0 and means nothing. This release instead
selects exact packages whose graders have actually been executed against
strict controls.

## Definition (exact package: task ID + package digest)

A package is in the pool only if all of these hold, from already-published
sources (no new executions, $0):

1. `mimo-clean-v7` status is `unchanged-clean-controls` or `repaired`;
2. strict repeat-3 controls on that exact package: reference rewards
   `[1,1,1]`, nop rewards `[0,0,0]`;
3. no source-supported or differential-confirmed finding on that exact
   package in `mimo-defect-map-v1` (an open finding excludes the package
   even when controls pass);
4. repaired packages additionally have a source-qualified repair row on the
   exact child package in the defect map (v7 / v7.1 / hardening rounds as
   recorded there).

## Files

- `manifest.json`: `mimo.executed-clean/v1`; pool members keyed by
  `record_id`, exclusion counts, training-overlap accounting, source digests.
- `build_pool.py`: standard-library producer; rerun from repo root with
  `python research/experiments/mimo-executed-clean-v1/build_pool.py`.

## Limits

- "Clean" here means: the stated controls were executed and passed on these
  exact bytes, and no supported finding is open on them. It is not semantic
  certification, not a claim that every wrong program is rejected, and not
  training admission. The frozen 33 held-out task IDs remain excluded from
  training under every package version (held-out packages in the pool are
  listed with `heldout_excluded_from_training: true`).
- Historical labels never transfer between packages: a wrong pass on an
  original grader does not taint a repaired child package, and a repaired
  child's controls do not clear the original.
