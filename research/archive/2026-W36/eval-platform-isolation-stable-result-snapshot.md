# Eval Platform — stable result-byte admissibility closure

## Authority

- Continue sole-writer work in `/Users/petermakhnatch/Developer/eval-lab/.worktrees/darwin-isolation-admission-final`, branch `fix/darwin-isolation-admission-final`.
- Exact reviewed head: `2f3b7bee8b12138616e7800a3294219e34aca0b9`.
- Read exact BLOCK report: `/tmp/system-architect-isolation-admission-rereview-2f3b7bee.md`.
- Preserve every closed entrypoint, R1-R4, canonical-authority, sidecar, isolation, and completion-time behavior. No rebase/integration/model/calibration/root edits.

## Single required repair

`result.json` digest authority and causal `finished_at` must derive from the same captured byte sequence.

- Capture the exact `result.json` bytes once for each finalization/strict-verification transaction, preferably through a no-follow regular-file descriptor.
- Thread that immutable byte snapshot through source-authority calculation and completion parsing. Do not hash `result.json` through one pathname read and later reopen it for `finished_at`.
- `_source_authority` and `_validate_interpretation_source` must use the same captured result bytes/digest where they validate the sidecar result binding; avoid introducing another independent result-path snapshot.
- `verify_trial_admissibility` must compare `record.source_digests.result` to the digest of the captured bytes and `record.evaluated_at` to `finished_at` parsed from those exact bytes.
- `finalize_trial_admissibility` must build from one captured result authority and pass the same authority into immediate strict verification, or otherwise ensure it cannot publish a mixed-snapshot/conflicting first artifact.
- Replacement of `result.json` after the captured read may either be detected by a final fail-closed stability check or be irrelevant to the accepted content identity, but the verifier must never combine an old digest with a new timestamp or claim current source equality when current bytes already differ. Prefer a stable opened descriptor plus path identity/end check if `source_binding_verified=True` claims live-path equality.

## Required tests

Add the exact `/tmp/probe_isolation_d57fd3c7.py` race as committed strict-loader coverage: replace result bytes between digest capture and time parsing; acceptance must fail or both digest/time must remain bound to the one snapshot without falsely claiming current live equality. Cover the finalizer equivalent. Retain all missing/start-only/malformed/naive/late completion and direct-entrypoint tests.

Run the exact 13-file 333-test matrix plus new tests, touched Ruff/format, and `git diff --check`. Commit and page exact old/new heads/evidence only when approval-ready.
