# Repo Custodian: integrate approved external lineage onto canonical spine

## Exact inputs

- Integration worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Integration branch/current head: `analyst/synth-data-promotion-hardening @ 677e28147dc5e36bd60edf57a37eeacf01885c7e`
- This spine already contains provisional external lineage `eaf9984c06bd2983b582d3ba491b2bc8ae367290` plus the approved resolver replay. Do not duplicate `eaf9984c`.
- Approved source branch: `/private/tmp/eval-lab-external-lineage-b1-b4-repair`, `fix/external-lineage-b1-b4-repair @ 27c2df40efa7439a3b50b0d6ec358a7d8d5eb168`
- Replay only reviewed semantic delta `eaf9984c06bd2983b582d3ba491b2bc8ae367290..27c2df40efa7439a3b50b0d6ec358a7d8d5eb168` (commits `721afd52`, `53e0e981`, `9aa63c31`, `817b7068`, `27c2df40`).
- Final approval report: `/tmp/system-architect-external-lineage-final-rereview-27c2df40.md`; prior reports remain supporting history.

## Integration contract

Integrate as sole writer by semantic replay/cherry-pick with conflict resolution against current spine. Preserve all current resolver-backed scale/analysis behavior at `677e2814`. Preserve the approved external contract exactly:

- typed durable external import lineage and strict candidate-source authority;
- packet-backed m049-v1 cannot enter a new registered state;
- already-registered historical m049-v1 is read/reopen/audit only and cannot mutate actor, time, certification, or bytes;
- typed distinct two-build attestations with exact environment/toolchain/output/time parity;
- one strict canonical UTC serialization;
- parsed CLI/API failures leave no mutated registry state.

Do not activate LoCoMo, create/modify registry data, authorize a canary, run a model, or carry unrelated source-branch history. Never touch/switch/reset/clean the dirty primary checkout.

## Verification and delivery

Run the focused external suites `tests/test_task_workbench.py tests/test_registry.py`, targeted B1-B4/read-only/CLI tests, the existing 55-test resolver regression matrix named in the integration history, touched Ruff/format, and diff-check. Regenerate only official required indexes if the repository workflow demands it. Commit the integrated replay on the canonical branch and page exact new head, replay commits, conflict resolutions, test counts, changed paths, and remaining ordered lanes. Do not begin isolation replay until its separate final approval.
