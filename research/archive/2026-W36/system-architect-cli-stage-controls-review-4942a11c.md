# System Architect — CLI staged-control bootstrap review

## Exact target

Read-only review:

- Worktree: `/private/tmp/eval-lab-cli-stage-controls`
- Branch: `fix/cli-stage-controls`
- Base: `770488cfb1c5318c6b1f39d386c2e251d8864487`
- Exact candidate head: `4942a11cae4e806fb69416e2d9ff0a653ed12bf9`
- Commits: `e5969ac6` + `4942a11c`
- Changed paths: only `src/evallab/cli.py`, `tests/test_registry.py`
- Defect source: `/tmp/platform-builder-registry-test-adaptation-map.md` lines 177–183

Do not edit, rebase, integrate, or run models/controls.

## Required contract

Verify APPROVE or BLOCK:

1. `registry promote --stage-controls` is a public explicit phase-1 surface and passes `stage_controls=True` exactly to the approved `promote_task` API.
2. Before any registry mutation it rejects:
   - state not explicitly `registered`;
   - missing/blank actor;
   - missing/blank certification packet;
   - any combination with legacy/immediate `--register`, including the full `--register --state registered` case.
3. A valid call persists the exact bound registered pending revision: `state=registered`, `state_reason=control_evidence_pending`, `allowed_uses=[canary]`, no control evidence, bound actor/certification/immutable identity.
4. Human CLI output says `control-pending` and `measurement unavailable`; JSON remains the exact record contract.
5. Pending state does not admit measurement; only existing oracle/nop baseline bootstrap resolution remains possible.
6. Flagless phase 2 through CLI discovers strict durable oracle/nop evidence, finalizes the same staged revision, and preserves runtime identity and external lineage.
7. Existing non-staged CLI behavior and strict legacy-refusal/external-lineage contracts are preserved. No shim, synthetic authority, weakened source check, or broad test deletion.
8. Tests are public behavioral tests, especially invalid-combination no-persistence and full stage→controls→CLI-finalize flow.

## Writer evidence

- New contract tests: 3 passed
- Full `tests/test_registry.py`: 67 passed
- `tests/test_trial_admission_bootstrap.py`: 5 passed
- Focused missed-guard test: 1 passed
- Ruff format/check and `git diff --check`: passed
- Clean worktree; no model/control/integration/canonical edit.

## Required output

Write `/tmp/system-architect-cli-stage-controls-review-4942a11c.md` with first-line `APPROVE` or `BLOCK`, requirement evidence, exact test commands/results, remaining blockers, and no-edit confirmation. Page `wH:p9` with verdict, exact report path, and exact head.
