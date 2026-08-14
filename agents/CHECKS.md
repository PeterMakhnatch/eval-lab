# Definition of Green

Green is a property of the exact pull-request head on GitHub, not a local claim.
Every reported GitHub Actions check must finish successfully. Pending, skipped,
cancelled, missing, neutral, and failed checks are not green.

## CI contract

The repository supports Python 3.12 and newer. Python 3.12 is the development and
lint floor; CI also exercises Python 3.14.

| Gate | Command | Version / interpreter |
|---|---|---|
| Locked install | `uv sync --locked` | uv 0.9.24; Python 3.12 and 3.14 |
| Lint | `uv run ruff check .` | locked Ruff; Python 3.12 |
| Tests | `uv run pytest` | locked pytest; Python 3.12 and 3.14 |
| Types | `uvx ty@0.0.71 check src/ --output-format=concise` | Python 3.12; ratchet at 33 diagnostics |

The ty job passes at or below 33 diagnostics and fails above 33. Lower the ratchet
when diagnostics are removed; never raise it without rationale in the PR.

Run `make premerge` before pushing. `scripts/premerge.sh` pins Python 3.12, checks
uv 0.9.24, performs the locked install, runs the complete lint and test suite, and
applies the same ty 0.0.71 ratchet. It is the local reproduction of the combined
`quality` and `typecheck` workflows; GitHub remains the merge authority.

## Deterministic-test rule

Tests must inject every external-state probe or seam. They must never depend on a
developer's Keychain, `~/.codex`, Docker daemon, network, wall clock, database, or
other host state. Use explicit temporary homes, fixed dates/times, stub credential
sets, fake runtimes, and injected I/O collaborators. A test that passes because a
developer happens to be authenticated is a failing test design.

## Merge rule

Before any role, human, or integrator merges a PR:

1. Fetch the current PR head and confirm the intended diff.
2. Run `gh pr checks <number>` and require every reported check to be complete and
   successful for that head SHA.
3. Do not substitute local green, an old run, mergeability, or unavailable branch
   protection for step 2.

If an integrator must make a local merge, the corresponding PR must already be
fully green. Run `scripts/premerge.sh` on the merge result before pushing, and put
both `Premerge: scripts/premerge.sh (pass)` and the green PR number/head SHA in the
merge commit body.

## 2026-08-14 incident lessons

- `pyproject.toml` advertised Python 3.11 while the lock supported only 3.12+;
  lint and the 3.11 test job could never install.
- A canary test used the executor's real credential probe. It passed on an
  authenticated workstation and deferred all dispatch on a clean CI runner.
- Private-repository branch protection is unavailable on the current GitHub plan.
  Several locally integrated PRs therefore reached main with red GitHub checks.

The process substitute is intentionally redundant: deterministic seams, a local
CI-parity command, explicit `gh pr checks`, and an auditable merge record.
