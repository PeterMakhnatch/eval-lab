# Eval Platform — final two-blocker isolation/admissibility closure

## Authority and workspace

- Continue as sole writer in `/Users/petermakhnatch/Developer/eval-lab/.worktrees/darwin-isolation-admission-final`, branch `fix/darwin-isolation-admission-final`.
- Current reviewed head: `b52a451641e3b10507e5907d8b129d1ba2f212a3`.
- Read exact report first: `/tmp/system-architect-isolation-admission-rereview-b52a451.md`.
- Preserve all independently closed R1-R3 and normal campaign/queue R4 behavior. Do not rebase, integrate, touch the dirty root, or run a model/calibration.

## Blocker 1 — execution-entrypoint isolation invariant

`Executor.execute_spec` itself must enforce the frozen campaign/runtime/live-isolation gate before any runner or producer can be reached. A public direct call for a registered oracle/nop baseline without authenticated campaign provenance must fail closed.

- Move or repeat the authoritative `_validate_campaign_dispatch_spec` boundary so `execute_spec` cannot bypass it. Prefer one production validation boundary rather than two live-rebind calls.
- Legitimate campaign execution must authenticate the manifest from the spec's frozen ledger/digest/attempt identities and live-rebind exactly once before runner execution.
- Direct noncampaign registered baseline control must refuse before invoking either the isolation identity provider or the runner.
- Preserve ordinary local noncampaign execution that is not a registered staged baseline, but do not infer campaign authority from a filename alone.
- Keep reconciliation fail-closed for resumed campaign records.
- Add a focused direct `execute_spec` negative matching the Architect probe and assert identity-provider calls == 0 and runner calls == 0. Retain queue/tick bypass coverage and normal oracle+nop lifecycle positives.

## Blocker 2 — digest-bound immutable completion time

Causal `TrialAdmissibilityV1.evaluated_at` must equal the exact timezone-aware `result.json.finished_at` from the digest-bound trial result source.

- Final causal publication must require one parseable, timezone-aware `finished_at` in `result.json`.
- Never substitute `started_at`, evidence observation time, verifier wall time, or Unix epoch for final causal publication.
- The shared strict verifier must parse the exact current/digest-bound result source and require `record.evaluated_at == finished_at` before reporting source/provenance verified or admissible.
- Missing, malformed, naive, or unequal completion times must fail closed with typed `TrialAdmissibilityError` and must not publish/accept causal authority.
- Add finalizer and strict-loader negatives for: no `finished_at`, start-only result, malformed time, naive time, and canonical record with a pre-expiry `evaluated_at` while exact result completion is post-expiry. Add a positive proving exact aware equality.

## Verification and delivery

Run the named focused matrix from the Architect report plus new direct-entrypoint/completion-time tests, touched Ruff, format check, and `git diff --check`. Commit the complete repair on the same branch. Page the parent with exact old/new heads, changed files, exact commands/counts, clean status, and confirmation that no model/calibration/integration occurred. Do not stop at a scope summary.
