Status: done
Last: merged as PR #119 (`e2bce8d`)
Next: none
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

## Cycle 4 Log

### 1. RECHECK
- Rechecked Cycle 3 acceptance with `scripts/premerge.sh`: passed (1281 passed, clean ruff, ty 28 diagnostics).

### 2. EXTEND
- Added golden coverage across all newly added and updated surfaces (`docs/STATUS.md`, `tests/golden/status.md`, `tests/golden/digest.md`).
- Validated all 3-state behaviors (quiet, active, unavailable) for storm alarms and discoveries in `tests/test_golden_rendering.py`.

### 3. PROVE
Command output from full pytest suite:
```
1282 passed, 1 skipped, 1 xfailed in 79.80s (0:01:19)
```

Premerge script execution:
```
premerge green: Python 3.12; ty 28 <= 28
```

### 4. HARDEN
- Added `test_digest_discoveries_quiet_vs_loaded_vs_unavailable` in `tests/test_golden_rendering.py`.
- Verified golden files for `status.md`, `digest.md`, and `preflight.txt`.

### 5. RECORD
- Premerge verification: `bash scripts/premerge.sh` (pass).
- Committed and pushed Cycle 4. PR: https://github.com/PeterMakhnatch/eval-lab/pull/119
- Set Status: done.

## Cycle 5 Log (Integrator Feedback Remediation)

### 1. RECHECK
- Ran baseline premerge checks: `scripts/premerge.sh` passed with 1282 passed tests, clean ruff, and ty 28 diagnostics.

### 2. EXTEND
- Fixed dataset substitution defect in `src/evallab/status_generator.py`: when catalog is accessible and returns no rows for reporting date (`yesterday`), status generator renders the authentic "none ran" state (`No completed trials observed in the reporting window.`) without substituting historical filesystem data.
- Implemented date-filtered filesystem fallback strictly scoped to the reporting date (`yesterday`) and labeled with an explicit source disclaimer `*(Source: filesystem fallback — catalog unavailable)*` when the catalog is unreachable.
- Fixed error handling: ceased swallowing parse failures silently by tracking `unreadable_jobs_count` and surfacing it in `## RECENT` (warning) and `## SYSTEM HEALTH & OPERATIONAL SMOKE` (`Unreadable job directories: N`).
- Fixed `via` list rendering: correctly extracts and formats agent names and model info (e.g. `via codex (gpt-5.6-terra)` or `via oracle`) rather than leaking trial directory identifiers.
- Regenerated live `docs/STATUS.md`, `docs/repo-map.md`, and `docs/INDEX.md`.

### 3. PROVE
Command output from generating `docs/STATUS.md` via `uv run evallab status --update`:
```
updated: /Users/petermakhnatch/Developer/eval-lab/.worktrees/m016-surface/docs/STATUS.md
```

Exact shasum comparison proving byte-identical determinism on consecutive generations:
```
Run 1 SHA256: 6af9bf3013bdb0a312dfb43c6fb32bfdc9309194612d60916781763940215c2d
Run 2 SHA256: 6af9bf3013bdb0a312dfb43c6fb32bfdc9309194612d60916781763940215c2d
PROVEN: Consecutive generations on unchanged input are BYTE-IDENTICAL.
```

Full pytest summary line:
```
1286 passed, 1 skipped, 1 xfailed in 50.01s
```

Premerge script execution:
```
premerge green: Python 3.12; ty 28 <= 28
```

Full generated `docs/STATUS.md` content from live data:
```markdown
---
status: living
audience:
  - operator
  - builder
  - runner
---

# Research status — 2026-08-18

Projection of live catalog, queue state, and `PROGRAM.json`.
Answers what happened yesterday and what is running now deterministically.

## RECENT (Yesterday: 2026-08-17)

No completed trials observed in the reporting window.

## RUNNING NOW

Nothing in `queue/running/` or `queue/approved/`.

## NEXT

No queued work waiting in `queue/waiting/`, `queue/pending/`, or `queue/proposed/`.

### Program Ledger Next Actions

1. **EXP-S02-txn-recon-k** (`status: waiting`): Does changing only attempt count on transaction-reconciliation change interval width more than the point estimate?
   - *Blocker:* k=5 hits per_job_cost_ceiling and canary max_attempts=3. k=1 spec was approved in runner worktree and never scored on primary.
   - *Next Action:* Peter: register n=5 or raise ceiling / measure per-attempt cost from 2026-08-15 actual 0.079/3≈0.026 (would be <$3 at k=5) but canary still caps attempts at 3.
1. **EXP-S03-preamble-ab** (`status: designed`): Does a short contract-discipline preamble change Codex pass@3 on event-summary?
   - *Blocker:* ExperimentSpec still has no extra_instruction_path; build_command does not forward --extra-instruction-path (confirmed grep on src/evallab/schemas.py ExperimentSpec).
   - *Next Action:* BUILDER adds the field. Then submit treatment only; pair with 2026-08-15 control. Do not submit a fake second control.
1. **EXP-S04-claude-vs-codex** (`status: designed`): Does claude-code complete a scored event-summary canary trial, and how does pass@3 sit beside Codex?
   - *Blocker:* Current availability of the Claude OAuth keychain item harbor-practice-claude-oauth is unresolved; the prior removed-worktree record reported it absent. Auth exceptions are harness, not capability.
   - *Next Action:* Peter decides whether a separate authorized workflow should verify/provision the keychain item; only then consider Study 04, without expanding to three tasks first.
1. **EXP-S05-curated-nominees** (`status: waiting`): What is Codex pass@5 on CURATOR's five nominated cards?
   - *Blocker:* Cards only (no task.toml here); not canary/*; k=5 exceeds canary max; estimated $4.17 exceeds $3. Representative was out_of_policy.
   - *Next Action:* Peter registers a slice or promotes nominees with digests. PROGRAM does not copy frontier-bench trees.
1. **EXP-S06-query-optimize-register** (`status: waiting`): Does standing policy admit Codex on lab-authored query-optimize, and is the family valid?
   - *Blocker:* out_of_policy for billable Codex. Poor canary (slow amd64 image, ~10 min/trial).
   - *Next Action:* Peter decides whether to register. Do not add to nightly canary suite.
1. **EXP-N2-event-summary-sol-vs-terra** (`status: designed`): On event-summary, does gpt-5.6-sol differ from the already-scored gpt-5.6-terra pin?
   - *Blocker:* Human decision: whether sol vs terra is still worth a night. Proposed spec uses registered/event-summary and a superseded hypothesis.
   - *Next Action:* Do not submit this draft. Do not approve/reject/delete the proposed spec from this role. Peter decides.
1. **EXP-N3-claude-code-event-summary** (`status: designed`): Can claude-code produce a scored event-summary canary trial?
   - *Blocker:* Claude keychain availability is unresolved; the prior removed-worktree record reported the item absent.
   - *Next Action:* Peter decides whether to verify/provision harbor-practice-claude-oauth in a separate authorized workflow; then reassess the existing Study 04 spec.

## TASK DECISIONS

Human-owned, unresolved decisions from active proposals and policy review.

- **EXP-S01-canary-codex-k3**: none on the 2026-08-15 scored jobs
- **EXP-S02-txn-recon-k**: k=5 hits per_job_cost_ceiling and canary max_attempts=3. k=1 spec was approved in runner worktree and never scored on primary.
- **EXP-S03-preamble-ab**: ExperimentSpec still has no extra_instruction_path; build_command does not forward --extra-instruction-path (confirmed grep on src/evallab/schemas.py ExperimentSpec).
- **EXP-S04-claude-vs-codex**: Current availability of the Claude OAuth keychain item harbor-practice-claude-oauth is unresolved; the prior removed-worktree record reported it absent. Auth exceptions are harness, not capability.
- **EXP-S05-curated-nominees**: Cards only (no task.toml here); not canary/*; k=5 exceeds canary max; estimated $4.17 exceeds $3. Representative was out_of_policy.
- **EXP-S06-query-optimize-register**: out_of_policy for billable Codex. Poor canary (slow amd64 image, ~10 min/trial).
- **EXP-N1-html-js-official-tests**: tests/test_outputs.py is hidden in the separate verifier and must never be copied, mounted, or made runnable in the evaluated agent image.
- **EXP-N2-event-summary-sol-vs-terra**: Human decision: whether sol vs terra is still worth a night. Proposed spec uses registered/event-summary and a superseded hypothesis.
- **EXP-N3-claude-code-event-summary**: Claude keychain availability is unresolved; the prior removed-worktree record reported the item absent.

## SYSTEM HEALTH & OPERATIONAL SMOKE

- Catalog accessible: yes
- Operational smoke/control specs count: 0
- Active storm alarms: 0 (quiet: no alarms in window)
```

### 4. HARDEN
- Added tests in `tests/test_status_generator.py`:
  - `test_status_rendering_zero_trials_renders_nothing_ran_and_no_trial_ids`
  - `test_status_rendering_three_states_distinguishable`
  - `test_status_rendering_unreadable_jobs_surfaced_as_count`
  - `test_status_filesystem_fallback_honors_date_filter_and_label`
- Updated golden reference `tests/golden/status.md` and test assertions in `tests/test_golden_rendering.py`.

### 5. RECORD
- Premerge verification: `bash scripts/premerge.sh` (pass).
- Committed and pushed Cycle 5. PR: https://github.com/PeterMakhnatch/eval-lab/pull/119

## Cycle 6 Log (Integrator Third Pass Remediation: Mutation Test Hardening)

### 1. RECHECK & ROOT CAUSE ANALYSIS
- Baseline check: re-ran `scripts/premerge.sh` (passed with 1286 tests, clean ruff, ty 28 diagnostics).
- **Why the mutant `if not recent_trials:` was unobservable in previous tests:**
  1. `_setup_mock_repo(tmp_path)` created directory structures without seeding any job files in `runs/` or `research/evidence/runs/`. When `trial_loader` returned `[]` and the mutant triggered the filesystem fallback, `rglob("result.json")` found 0 jobs, keeping `recent_trials` empty. `render_status_markdown` then checked `elif data.catalog_accessible:` (which was `True`), rendering `"No completed trials observed in the reporting window."` identically to the unmutated code.
  2. When historical jobs (e.g. dated 2026-08-13) were present on disk, line 259's date filter (`if finished_dt.date() != yesterday: continue`) filtered them out, also keeping `recent_trials` empty and yielding identical rendered output.
  3. The mutant is only observable when valid jobs exist on disk (particularly matching yesterday's date, or when checking `trials_source` directly) while the catalog is accessible and empty. Under the correct guard (`if not catalog_accessible:`), the filesystem is never touched and `trials_source` remains `"catalog"`. Under the mutant (`if not recent_trials:`), the empty catalog causes an erroneous filesystem scan, loading the disk jobs, setting `trials_source = "filesystem"`, and leaking filesystem trials into the report.

### 2. EXTEND
- Implemented `_write_mock_job` in `tests/test_status_generator.py` to create valid, authentic Harbor job structures on disk (`config.json`, `lock.json`, `result.json`, `trial/result.json`).
- Updated `test_status_rendering_zero_trials_renders_nothing_ran_and_no_trial_ids` to seed both yesterday (`2026-08-15`) and historical (`2026-08-12`) jobs on disk, asserting `catalog_accessible is True`, `trials_source == "catalog"`, `len(recent_trials) == 0`, and verifying no disk trial IDs or fallback disclaimers leak into the rendered markdown.
- Added `test_status_accessible_empty_catalog_never_falls_back_to_filesystem_even_with_runs_on_disk` verifying both the accessible-empty-catalog case (no filesystem scan) and the inaccessible-catalog case (filesystem fallback with explicit disclaimer).

### 3. PROVE (MUTATION TEST VERIFICATION)

**Proof 1: Reverting guard to `if not recent_trials:` FAILS the test suite:**
```
FAILED tests/test_status_generator.py::test_status_rendering_zero_trials_renders_nothing_ran_and_no_trial_ids - AssertionError: assert 'filesystem' == 'catalog'
  - catalog
  + filesystem
FAILED tests/test_status_generator.py::test_status_accessible_empty_catalog_never_falls_back_to_filesystem_even_with_runs_on_disk - AssertionError: assert 'filesystem' == 'catalog'
  - catalog
  + filesystem
=========================== short test summary info ============================
FAILED tests/test_status_generator.py::test_status_rendering_zero_trials_renders_nothing_ran_and_no_trial_ids - AssertionError: assert 'filesystem' == 'catalog'
FAILED tests/test_status_generator.py::test_status_accessible_empty_catalog_never_falls_back_to_filesystem_even_with_runs_on_disk - AssertionError: assert 'filesystem' == 'catalog'
pytest: 2 failed, 14 passed in 0.53s
```
Under the mutant, `job-yesterday` on disk was erroneously loaded and rendered as `- **task-yesterday** — 1/1 reward==1.0 via codex (gpt-5.6-terra)` with `*(Source: filesystem fallback — catalog unavailable)*`, failing all assertions.

**Proof 2: Restoring correct guard `if not catalog_accessible:` PASSES the test suite:**
```
tests/test_status_generator.py ................                                                         [100%]
16 passed in 0.52s
```

Full test suite output (`uv run pytest`):
```
=========================== short test summary info ============================
1287 passed, 1 skipped, 1 xfailed in 50.19s
```

### 4. HARDEN
- Hardened regression tests across `tests/test_status_generator.py`:
  - `test_status_rendering_zero_trials_renders_nothing_ran_and_no_trial_ids` (with disk fixtures)
  - `test_status_accessible_empty_catalog_never_falls_back_to_filesystem_even_with_runs_on_disk` (both accessible-empty and inaccessible-fallback paths)

### 5. RECORD
- Premerge verification: `bash scripts/premerge.sh` (pass):
```
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 28
```
- Note on CI: GitHub PR workflows are currently failing globally in 2-3s with 0 steps due to external runner infrastructure outages. Local `scripts/premerge.sh` with 1287 passing tests and 28 ty diagnostics serves as authoritative evidence.
- Committed and pushed Cycle 6. PR: https://github.com/PeterMakhnatch/eval-lab/pull/119
- Set Status: done.
