# System Architect — final isolation/admissibility re-review at `2f3b7bee`

## Exact target

- Read-only worktree `/Users/petermakhnatch/Developer/eval-lab/.worktrees/darwin-isolation-admission-final`, branch `fix/darwin-isolation-admission-final`.
- Exact clean head `2f3b7bee8b12138616e7800a3294219e34aca0b9`.
- Review full authority `ba95065b5ebe580b352ee370b08a2836a84f7c15..2f3b7bee8b12138616e7800a3294219e34aca0b9`, focusing on the two-blocker repair `b52a451..d57fd3c7` and unskippable-entrypoint closure `d57fd3c7..2f3b7bee`.
- Prior report: `/tmp/system-architect-isolation-admission-rereview-b52a451.md`.
- Repair brief: `research/inbox/eval-platform-isolation-final-two-blocker-closure.md`.
- Do not edit, integrate, rebase, or run a model/calibration.

## Exact closure to verify

1. `Executor.execute_spec` has no caller-supplied sentinel/token/skip keyword and always executes the full authenticated manifest/runtime/current-isolation/live-identity validation before any task resolution or runner call.
2. `_dispatch_one` performs only structural manifest binding preflight (`live_rebind=False`) for early typed queue refusal; `execute_spec` performs the sole live rebind. Normal oracle+nop campaign dispatch calls the identity provider exactly once.
3. Direct unbound registered oracle/nop baseline execution refuses before identity-provider and runner. Attempting the removed `_campaign_validation` keyword raises `TypeError`; no module-global skip sentinel exists.
4. Final trial authority requires parseable timezone-aware exact `result.json.finished_at`; no `started_at` or epoch fallback is used for published authority.
5. The shared strict verifier reparses the exact digest-bound result source and requires `record.evaluated_at == finished_at`. Missing, start-only, malformed, naive, or unequal/post-expiry completion fails closed.
6. Every R1-R3 and normal R4 lifecycle closure previously accepted in the b52a report remains intact, including certification/approval/runtime identity preservation, canonical fsynced authority, strict sidecar chain, current control evidence, live dispatch, and production oracle+nop lifecycle.

Re-run the prior direct execute and forged completion probes, plus try importing/passing any old/private sentinel path. Verify no alternate caller path reaches runner without the full gate.

## Reported evidence

- Exact 13-file matrix from prior report: 333 passed, two existing warnings.
- Touched Ruff and format check passed.
- `git diff --check` passed.
- Worktree reported clean.
- No model/calibration/rebase/integration.

## Deliverable

Write `/tmp/system-architect-isolation-admission-rereview-2f3b7bee.md`, page exact APPROVE/BLOCK with any reproducible blocker, and do not integrate.
