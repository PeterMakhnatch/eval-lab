Status: review-wanted
Last: Rebased onto 5e10db5; floor premerge is green and Python 3.14 passes all 51 tests.
Next: Push, open the bootstrap MEDIC PR, and require all GitHub checks green.
Blockers: none for bootstrap; branch-protection product decision remains Peter's.

# MEDIC handoff

Worktree: `.worktrees/medic` on `role/medic`, based on `origin/main` at
`5e10db5`.

## Verified diagnosis

- PR #6 quality run `31806665517`: lint and test 3.11 fail at
  `uv sync --locked` because the lock supports Python 3.12+; test 3.14 reaches
  50 passing tests and fails only the canary dispatch assertion (`3 == 0`).
- PR #6 typecheck run `31806665580`: ty passes at the 33-diagnostic ratchet.
- The canary helper constructs a Codex executor without its available
  `credential_probe` seam. The queue helper already demonstrates the intended
  fixed-credential pattern.
- A suite sweep found external services represented by injectable seams or
  temporary state. No second CI-sensitive live-host dependency was found.

## Local verification

- `make premerge` on Python 3.12: locked sync, Ruff clean, 51 tests passed,
  ty 0.0.71 at `33 <= 33`.
- `UV_PYTHON=3.14 uv sync --locked && UV_PYTHON=3.14 uv run pytest`:
  51 tests passed, including the formerly credential-dependent canary.
- The first premerge attempt exposed a redundant `[tool.uv].environments`
  marker that uv 0.9.24 serialized as an empty supported-marker set but then
  considered stale. Removing the now-redundant marker made `uv lock --check`
  reproducible under Python 3.12.

## Integration sequence

1. Land MEDIC's Python-floor, deterministic-test, premerge, and process repair
   only after its GitHub quality checks are fully green.
2. Bring fixed main into PR #6 without force-pushing, require both quality and
   typecheck green, then merge it.
3. Follow with the FORGE status, engineering-floor note, final run links, PR
   annotations, and explicit main workflow dispatches.

## Peter decision, not MEDIC

GitHub Pro or making the repository public are the available paths to physically
enforced required checks. MEDIC does not choose between them. Until Peter does,
`agents/CHECKS.md`, `make premerge`, and mandatory `gh pr checks` verification
are the process substitute.
