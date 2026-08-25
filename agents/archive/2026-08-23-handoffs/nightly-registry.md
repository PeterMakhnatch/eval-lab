Status: done
Last: merged as PR #106 (`2dab2ae`)
Next: none
Blockers: none

# Nightly Step Registry Pipeline Handoff

## 1. Summary of Changes

Implemented the extensible Nightly Step Registry pipeline in accordance with `docs/platform-architecture.md` (v2 §3.2, §2.3):

1. **Step Registry and Pipeline (`src/evallab/automation.py`)**:
   - Refactored `NightlyCycle.run` from a monolithic hardcoded sequence into an ordered registry pipeline (`NightlyStep`, `NightlyContext`, `StepOutcome`, `DEFAULT_NIGHTLY_STEPS`).
   - Each registered step explicitly defines:
     - `name`: Stable step identifier.
     - `fn`: Callable acting on `NightlyContext`.
     - `timeout`: Wall-clock allowance in seconds.
     - `on_fail`: Policy (`abort` vs `continue`).
     - `idempotent`: Boolean declaring whether multiple executions over unchanged state produce identical artifacts.
     - `description`: Human-readable explanation.
   - Preserved exact canonical step execution order:
     1. `doctor` (timeout 60s, `on_fail="abort"`, idempotent)
     2. `catalog_ingest` (timeout 300s, `on_fail="abort"`, idempotent)
     3. `analysis_staging` (timeout 60s, `on_fail="continue"`, idempotent)
     4. `parquet_compaction` (timeout 300s, `on_fail="continue"`, idempotent) — **wired dormant module**
     5. `postgres_backup` (timeout 120s, `on_fail="abort"`, idempotent)
     6. `canary_enqueue` (timeout 60s, `on_fail="abort"`, non-idempotent)
     7. `dispatch` (timeout 600s, `on_fail="abort"`, non-idempotent)
     8. `researcher_pass` (timeout 300s, `on_fail="continue"`, non-idempotent)
     9. `lessons` (timeout 120s, `on_fail="continue"`, idempotent) — **wired dormant module**
     10. `digest` (timeout 60s, `on_fail="abort"`, idempotent)
     11. `status_update` (timeout 30s, `on_fail="continue"`, idempotent)

2. **Dormant Module Functionalization**:
   - `evallab.parquet_compaction.compact` registered after `analysis_staging` / `catalog_ingest` (F3 position).
   - `evallab.lessons.generate_lessons_file` registered after `researcher_pass` / facts availability (F4/F9 position).
   - Default implementations provided with test injection seams (`compactor`, `lessons_generator`, `status_updater`, etc.).

3. **Per-Step Outcome Reporting**:
   - `NightlyResult` records `steps: tuple[StepOutcome, ...]` capturing `status` (`ran`, `skipped`, `failed`), `duration_s`, `reason` (for skips like `no_backup_configured`, `quarantined_by_prior_step`), and `error` (for failures).
   - Provided `result.format_step_report()`, `result.step_outcomes`, and `result.step_by_name(name)`.

4. **`on_fail` Policy Enforcement**:
   - Steps with `on_fail="abort"` set `context.quarantined = True` and skip subsequent non-surface steps while letting terminal reporting surfaces (`digest`, `status_update`) render the quarantine state.
   - Steps with `on_fail="continue"` log error events without quarantining or aborting the cycle.

5. **Documentation & Index**:
   - Extended `docs/operations.md` with registry details, step table, `on_fail` semantics, idempotence classification, custom step guide, and per-step report reading instructions.
   - Regenerated `docs/INDEX.md` via `docindex generate`.

6. **Test Suites (`tests/test_automation.py`, `tests/test_unattended.py`)**:
   - Added unit tests in `tests/test_automation.py` asserting exact sequence preservation, step metadata, continue-on-fail execution, abort-on-fail stopping, skip reasons, idempotence over unchanged state, and custom step injection.
   - Extended `tests/test_unattended.py` validating that healthy nightly runs execute compaction, lessons, backups, dispatch, and digest.

## 2. Verification Evidence

- `uv run pytest tests/test_automation.py tests/test_unattended.py` passed with 33/33 tests passing.
- `uv run pytest` passed with 1144 passed, 2 skipped, 1 xfailed.
- `uv run ruff check .` passed with 0 errors.
- `uvx ty@0.0.71 check src/ --output-format=concise` passed (reported 0 errors; within threshold <= 28).
- `uv run python -m evallab.docindex generate -o docs/INDEX.md` and `uv run python -m evallab.docindex check` passed cleanly.

## 3. Leased Paths

- `src/evallab/automation.py`
- `tests/test_automation.py`
- `tests/test_unattended.py`
- `docs/operations.md`
- `docs/INDEX.md`
- `agents/handoffs/nightly-registry.md`
