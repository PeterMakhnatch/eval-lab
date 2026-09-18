# Repo Custodian — integrate approved CLI staged-control bootstrap

## Exact target

- Canonical worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Canonical branch/head: `analyst/synth-data-promotion-hardening @ 1ecfc4a587be1301fa1e5a3ddf4e4bf8a942c3ee`
- Approved source worktree: `/private/tmp/eval-lab-cli-stage-controls`
- Source branch/head: `fix/cli-stage-controls @ 4942a11cae4e806fb69416e2d9ff0a653ed12bf9`
- Source base: `770488cfb1c5318c6b1f39d386c2e251d8864487`
- Source commits: `e5969ac6` + `4942a11c`
- Architect approval: `/tmp/system-architect-cli-stage-controls-review-4942a11c.md`

Do not edit dirty root, run models/controls, or integrate unrelated work.

## Exact scope

Only:

1. `src/evallab/cli.py`
2. `tests/test_registry.py`

The context-ordering landing `770488cf..1ecfc4a5` touched neither file, so replay the exact approved source delta without broad formatting or test replacement. Preserve all 57 canonical+strict registry test functions already integrated; the source adds public CLI staged-control coverage and updates existing external/CLI bootstrap tests without dropping authority coverage.

## Required contract

- Public `registry promote --stage-controls` passes exact `stage_controls=True`.
- Pre-mutation guards reject non-registered state, blank actor, blank/missing certification packet, and every `--register` combination including explicit registered state.
- Valid phase 1 persists the exact registered `control_evidence_pending` revision with `allowed_uses=[canary]`, no controls, bound actor/certification/runtime identity, and measurement-unavailable wording.
- Pending resolution remains baseline oracle/nop only; no measurement admission.
- Flagless CLI phase 2 discovers strict durable controls, finalizes the same revision, and preserves runtime identity and external lineage.
- No source authority weakening, synthetic control, compatibility shim, or model/control run.

## Verification

Run at integrated exact head:

- `uv run pytest tests/test_registry.py tests/test_trial_admission_bootstrap.py`
- touched Ruff check/format check;
- `git diff --check`;
- exact two-path diff and canonical registry test inventory preservation.

Commit cleanly and page `wH:p9` with old/new exact heads, commit, path/stat, test counts, clean status, and no-model/no-control/no-root-edit confirmation.
