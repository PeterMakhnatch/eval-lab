# Ops Eval Runner — stable-object CAS and Harbor launch repair

## Authority and workspace

- Continue as the sole writer in `/Users/petermakhnatch/Developer/eval-lab/.worktrees/eval-runner-cas-harbor-settlement` on `fix/run-cas-harbor-settlement`.
- Current reviewed head: `d013f766c812bd7e3d62760a1e1ec3a52cb0b26b`.
- Read the exact BLOCK report first: `/tmp/system-architect-run-cas-harbor-rereview-d013f766.md`.
- Preserve every previously closed behavior and focused test. Do not rebase, integrate, touch the dirty root checkout, or run any model evaluation.

## Required repair

### 1. Independent producer record trust anchor

- `archive_evidence` must return the exact producer record digest (or exact canonical bytes plus digest) computed from the bytes it atomically wrote.
- `_settle_completed_job` must pass that independently produced expected digest into `reopen_evidence_archive`; it must never derive the expected digest from the record being authenticated.
- Record-only reopen must require an independently supplied already-settled record digest or a truly content-addressed immutable record reference. There must be no unauthenticated record-path-only success mode.
- Semantically valid alternate `archived_at` or `source_path` values must be rejected when they do not match the producer record bytes.
- Preserve strict exact schema/type/canonical-JSON/UTC/path validation.

### 2. Stable opened record and archive objects

- Verification and restoration must operate on the same stable opened objects. Do not validate a pathname and later reopen that mutable pathname for restore.
- The exact archive bytes whose digest is validated must be the bytes restored.
- The returned record/blob identities must remain valid at return. A fail-closed equivalent stability protocol is acceptable only if it proves pathname/object stability across the whole operation.
- Refuse injected record replacement after record validation.
- Refuse injected same-content/different-gzip-byte archive replacement after archive validation.
- Preserve record-only reopen after the mutable source directory has been deleted, but only with the independent expected record digest/reference.

### 3. Live-source end stability

- Optional live-source settlement must take a stable source snapshot or re-inventory/re-digest after restore and require before/after equality for digest, file count, and uncompressed bytes.
- A mutation during restore must raise typed `ExecutionFailure("evidence_cas_unsettled", ...)` through `_settle_completed_job`, and `run_experiment` must attempt terminal `failed` before re-raising.

### 4. Spawn bound to verified Harbor object

- Bind process creation to the verified executable object, not a mutable pathname resolved again after verification.
- On Darwin, use a verified executor-owned stable launch artifact or descriptor/inode-bound design. Preserve executable behavior and lock compatibility; do not rely on a second pathname stat/digest check alone.
- Add an adversarial hook that atomically replaces the configured executable after final verification but before spawn. Changed bytes must never execute and executor state must become terminal `failed`.
- The launched Harbor identity must still match the lockfile/API compatibility checks and the actual pinned bytes.

## Shared API contract

Engineer Data consumes this authority. Keep `EvidenceArchive`/`reopen_evidence_archive` public and boring, but change the reopen API as needed so an independent expected record digest/reference is mandatory. Do not retain a fail-open optional digest compatibility path. Parent will have Engineer Data rebase onto the approved final authority.

## Verification and delivery

Add focused adversarial tests for all four report findings, including semantically valid record-field tamper, record swap during reopen, same-content/different-gzip archive swap, source mutation during restore, and executable replacement after final verification. Run only the focused runner/queue/evidence-store tests, touched-file Ruff, format check, and `git diff --check`. Commit all changes on the same branch and page the parent with exact old/new heads, changed files, focused command results, clean status, and confirmation that no model run occurred.
