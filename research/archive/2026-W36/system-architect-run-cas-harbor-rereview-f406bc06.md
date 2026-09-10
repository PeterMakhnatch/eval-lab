# System Architect — final stable-object run CAS / Harbor re-review at `f406bc06`

## Review target

- Read-only worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/eval-runner-cas-harbor-settlement`, branch `fix/run-cas-harbor-settlement`.
- Exact new head: `f406bc06bf713caf39820daa7099244b3743704f`.
- Base authority: `93d2e7c184ce607ea57c86f732bd493bfba2489d`.
- Prior blocked head: `d013f766c812bd7e3d62760a1e1ec3a52cb0b26b`.
- Review full range `93d2e7c..f406bc06`, focusing on stable-object delta `d013f766..f406bc06` and prior report `/tmp/system-architect-run-cas-harbor-rereview-d013f766.md`.
- Repair brief: `research/inbox/ops-run-cas-stable-object-final-repair.md`.
- Do not edit, integrate, rebase, or run any model evaluation.

## Claimed closure to verify adversarially

1. `archive_evidence` returns an independent digest of the exact producer canonical record bytes. `reopen_evidence_archive` requires `expected_record_digest` and validates it before parsing. `_settle_completed_job` passes only the producer-returned digest; no unauthenticated record-path-only compatibility mode exists.
2. Semantically valid alternate `archived_at` or `source_path` values are refused by the independent producer digest, in addition to strict schema/type/canonical JSON/UTC/path validation.
3. Reopen captures archive bytes once, validates those bytes, and restores those same captured bytes. It performs final record/blob stability checks before returning and re-inventories/re-digests the optional live source after restore. Injected record replacement, same-content/different-gzip archive replacement, and source mutation during restore fail closed.
4. `_settle_completed_job` maps expected reopen failures to typed `evidence_cas_unsettled`; `run_experiment` attempts terminal `failed` for every settlement or launch-identity failure and re-raises the original unexpected exception.
5. Harbor process creation uses an executor-owned `.harbor-launch` artifact copied from the lock-pinned executable and verified against the captured executable digest. Atomic replacement of the configured executable after final pathname verification either makes the copy digest fail before launch or leaves the verified staged bytes unchanged; changed configured bytes must never execute.
6. Synthetic `/bin/tool` fixture shortcuts have been replaced with real executable bytes for launch-boundary coverage. The explicit `SettledRun` cutover and record digest handoff remain intact.
7. Record-only reopen after mutable source deletion still succeeds only when the independent already-settled digest is supplied.

Probe not just the exact hooks in the committed tests: attempt mutation after each record/blob validation snapshot and between configured-path verification, staging copy, staged digest verification, and process creation. Distinguish a configured-path race (required closure) from arbitrary same-user mutation of executor-owned state, but BLOCK if returned authorities or launched bytes can change silently inside the claimed protocol.

## Reported evidence to corroborate

- `tests/test_runner.py tests/test_queue.py`: 125 passed, only pre-existing warnings.
- Touched Ruff: passed.
- Ruff format check: passed.
- `git diff --check`: passed.
- Worktree reported clean.

## Deliverable

Write `/tmp/system-architect-run-cas-harbor-rereview-f406bc06.md` with exact APPROVE/BLOCK verdict, evidence, and any remaining blocking finding tied to paths/symbols plus a reproducible probe. Page the parent. Do not integrate.
