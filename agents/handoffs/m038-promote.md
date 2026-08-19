# M038 — Implement `evallab registry promote`

Status: complete — ready for review
Last: implemented `evallab registry promote` and `evallab registry register` with control evidence discovery and cryptographic digest validation; added unit/e2e tests and validated against premerge and mutation testing.
Next: Peter can register the four real `library/tasks/` packages by running:
  1. `uv run python -m evallab.cli registry promote library/tasks/event-summary`
     `uv run python -m evallab.cli registry register event-summary --actor "Peter Makhnatch"`
  2. `uv run python -m evallab.cli registry promote library/tasks/query-optimize`
     `uv run python -m evallab.cli registry register query-optimize --actor "Peter Makhnatch"`
  3. `uv run python -m evallab.cli registry promote library/tasks/transaction-reconciliation`
     `uv run python -m evallab.cli registry register transaction-reconciliation --actor "Peter Makhnatch"`
  4. `uv run python -m evallab.cli registry promote library/tasks/terminal-bench-html-js-filter`
     `uv run python -m evallab.cli registry register terminal-bench-html-js-filter --actor "Peter Makhnatch"`
Blockers: none

## Pull Request

- URL: https://github.com/PeterMakhnatch/eval-lab/pull/133
- Branch: `role/m038-promote` tracking `origin/main`

## What Landed

1. **`evallab registry promote <task-path>`**:
   - Computes 5 cryptographic digests (`task_toml`, `instruction`, `environment`, `verifier`, `package`).
   - Discovers real Oracle (reward=1.0) and Nop (reward=0.0) control evidence by reading completed Harbor job directories under `runs/` (and `research/evidence/runs`).
   - Refuses promotion if either control evidence run is missing with the exact command to produce it (`uv run python -m evallab.cli run --task <path> --agent <agent> --name <name> --jobs-dir runs`).
   - Refuses contradictory evidence if Oracle reward != 1.0 or Nop reward != 0.0 with an explicit error that the instrument is broken.
   - Defaults new records to `state="candidate"`.
   - Idempotence: re-promoting an unchanged package is a no-op; re-promoting a package whose bytes changed on disk refuses with `TaskDigestMismatchError` unless `--version` is bumped.
2. **`evallab registry register <task-id> --actor "<name>"`**:
   - Explicit human-approved promotion step transitioning candidate records to `state="registered"`.
   - Re-verifies package completeness, on-disk digests, and control evidence.
   - Records `approved_by` and `approved_at` timestamp.
3. **CLI Integration & Golden Surface**:
   - Registered `promote` and `register` subparsers under `registry`.
   - Updated `tests/golden/cli_surface.json` and `test_cli_registry.py` leaf count (54).
   - Regenerated `docs/repo-map.md` and `docs/INDEX.md`.

## Premerge Gate

`bash scripts/premerge.sh` output:

```
Resolved 75 packages in 4ms
Audited 51 packages in 1ms
All checks passed!
1491 passed, 1 skipped, 1 xfailed in 112.93s (0:01:52)
evallab preflight — is it safe and sensible to run right now
PASS doctor mode=docker-free
PASS submit->tick job=smoke-oracle-fat7hvb040gd trials=1
PASS catalog job_id=886e92a2-0de4-4384-b7ad-aa8c623e96b1
PASS parquet job_id=886e92a2-0de4-4384-b7ad-aa8c623e96b1
PASS digest path=runs/_smoke/smoke-oracle-fat7hvb040gd/digests/2026-08-19.md
PASS analysis sidecar=runs/_smoke/smoke-oracle-fat7hvb040gd/analyses/92b2c72c-5ad4-4f4b-82ae-27e02dbad9bf/analysis.json validation=valid
PASS status snapshot sections=Recent,Now,Next,Tasks,Health,Analysis analysis=draft
SMOKE PASS both-stores-agree
Found 27 diagnostics
notice: ty is down to 27; lower the baseline from 28
premerge green: Python 3.12; ty 27 <= 28
```

## Mutation Evidence

### Mutation 1: Remove Control-Evidence Gate

When the missing evidence check is removed in `discover_control_evidence`:

```
FAILED tests/test_registry.py::test_promote_task_refuses_when_oracle_evidence_missing - Failed: DID NOT RAISE <class 'evallab.registry.TaskControlEvidenceError'>
FAILED tests/test_registry.py::test_promote_task_refuses_when_nop_evidence_missing - Failed: DID NOT RAISE <class 'evallab.registry.TaskControlEvidenceError'>
2 failed, 31 deselected in 0.30s
```

Restored:
```
tests/test_registry.py::test_promote_task_refuses_when_oracle_evidence_missing PASSED
tests/test_registry.py::test_promote_task_refuses_when_nop_evidence_missing PASSED
```

### Mutation 2: Accept Contradictory Evidence

When contradictory reward checks (`oracle != 1.0` and `nop != 0.0`) are disabled:

```
FAILED tests/test_registry.py::test_promote_task_refuses_contradictory_oracle_evidence - Failed: DID NOT RAISE <class 'evallab.registry.TaskControlEvidenceError'>
FAILED tests/test_registry.py::test_promote_task_refuses_contradictory_nop_evidence - Failed: DID NOT RAISE <class 'evallab.registry.TaskControlEvidenceError'>
2 failed, 31 deselected in 0.28s
```

Restored:
```
tests/test_registry.py::test_promote_task_refuses_contradictory_oracle_evidence PASSED
tests/test_registry.py::test_promote_task_refuses_contradictory_nop_evidence PASSED
```

### Mutation 3: Break Idempotence / Version Digest Check

When `TaskDigestMismatchError` on tampered on-disk bytes for an existing version is disabled:

```
FAILED tests/test_registry.py::test_promote_task_refuses_tampered_package_without_version_bump - Failed: DID NOT RAISE <class 'evallab.registry.TaskDigestMismatchError'>
1 failed, 32 deselected in 0.25s
```

Restored:
```
tests/test_registry.py::test_promote_task_refuses_tampered_package_without_version_bump PASSED
```
