# Repo health best-practices audit — 2026-08-29

Scope: CI/local parity, governance/rules truthfulness, dependency/workflow drift,
generated indexes, worktree/branch hygiene, stale active docs, dead/duplicate
code, evidence boundaries, and Pstack mechanism gaps on `origin/main`
(`bf4120ce`). Files leased by open PRs #300/#302/#303/#304/#305 and the
`hygiene/agent-orientation` PR #272 were inspected for context but not edited.

## Measured estate

| Surface | Measurement |
|---|---|
| Base | `origin/main` `bf4120ce` (Merge PR #301), clean worktree `hygiene/repo-best-practices` |
| CI workflows | 14 under `.github/workflows/` |
| Local branch estate | 184 local / 170 remote branches |
| Merged remote branches not yet swept | 11 (`ci/*`, `feat/zai-opencode-adapter`, `cleanup/*`, `hygiene/*`, `lane/execution`, `research/repo-standards-pstack`, `research/tutor-state-report-*`, `fix/*`) |
| Premerge parity gates | 8 `uv run` gates in `scripts/premerge.sh` |
| CI deterministic gates (lint job) | ruff, docindex, repomap, governance, registry audit, lessons |
| ty diagnostics | 3 without observability group; 0 with observability (CI syncs it) |

## Verified clean (no change needed)

- `ruff check .` — pass on origin/main.
- `docindex check`, `repomap check`, `governance check`, `registry audit`,
  `lessons` — all pass on a clean `origin/main` checkout.
- `uv sync --locked` — lock and `pyproject.toml` in sync; dependency drift none.
- `docs/INDEX.md` / `docs/repo-map.md` — merge-driver + post-merge/post-rewrite
  regeneration path (`scripts/git-merge-regen.sh`, `.githooks/*`) is coherent and
  the `regen` driver is wired by `scripts/setup-git.sh`.
- CI `typecheck` baseline is 0; `scripts/premerge.sh` baseline is 0 (the
  stale `TY_BASELINE=28` copy exists only in the primary checkout, 76 commits
  behind origin/main — see residual item).
- Governance markers and root-freeze (`agents/STRUCTURE.md`) intact.

## P0–P3 ranking

| Rank | Finding | Adopt / adapt / reject |
|---|---|---|
| P1 | `scripts/premerge.sh` syncs `uv sync --locked` (dev+observability) but CI test job syncs `--group benchmarks`; the live fastmcp/cryptography contract tests (`tests/test_mcp_recovery_v1.py::test_live_fastmcp_*`) silently skip locally via `pytest.importorskip` while CI runs them. Premerge therefore is not the faithful "reproduction of the combined `quality` and `typecheck` workflows" that `agents/CHECKS.md` claims. | **Adopt — fixed** (this change) |
| P2 | `agents/CHECKS.md` CI-contract table omits four gates CI lint actually runs (docindex, repomap, registry audit, lessons) and does not document the benchmarks group, so the written contract understates the true gate set. | **Adopt — fixed** (this change) |
| P2 | Primary checkout is 76 commits behind origin/main and carries a stale `scripts/premerge.sh` (`TY_BASELINE=28`) plus untracked evidence files; anyone running premerge from the primary checkout gets a different (looser) gate. | **Adapt — report only.** Primary checkout is the user's dirty tree; not edited per scope. |
| P3 | 11 remote branches are merged into main but unswept; `ci/quality-parity-gates` (PR #292, closed) is 0 commits ahead of main. | **Adapt — report only.** Deleting branches is not a safe reversible change here; leave to integrator. |
| P3 | `src/evallab/semantic_facts.py:58,173` emit pydantic `UserWarning` (field `construct` shadows parent `FactRow`) on every `lessons`/`registry` run. Cosmetic, deterministic. | **Reject (not fixed).** Outside best-practices-cleaned scope; requires model review. |

## Exact changes

1. `scripts/premerge.sh` — change the sync step to `uv sync --locked --group benchmarks`
   so the benchmark dependency group (fastmcp, cryptography) is installed and the live
   fastmcp contract tests run locally instead of skipping. Comment explains the parity
   reason.
2. `agents/CHECKS.md` — CI-contract table now lists every gate CI actually runs
   (adds docindex, repomap, registry audit, lessons; notes benchmarks group in the
   locked-install row's premerge paragraph). No governance markers altered; markers
   verified by `evallab.governance check`.

## Verification

- `uv sync --locked --group benchmarks` installs fastmcp + cryptography (confirmed).
- `tests/test_mcp_recovery_v1.py -k live_fastmcp` collects 10 tests (previously the
  live-fastmcp tests were skipped without the group).
- `uv run ruff check .` — pass.
- `uv run python -m evallab.governance check` — pass.

## Residual design items

- Full `scripts/premerge.sh` run was not executed here (runs the whole pytest suite
  and smoke; CI is the merge authority). The parity change was verified by the focused
  sync + collect + gate checks above.
- Primary checkout drift (76 behind) should be reconciled by Peter or the integrator.
- Unswept merged remote branches remain for the integrator's `tidy`/sweep step.
- `.required-ci-checks.json` exists only on the unmerged `research/repo-standards-pstack`
  branch, not on origin/main; its branch-protection substitute is not live in main.

## Provenance

Performed per `.omp/skills/repository-health/SKILL.md` (Pstack
`principle-encode-lessons-in-structure`): baseline first, route the recurring
parity gap to a deterministic mechanism (the premerge gate), delete the prose that
understated it, and verify the touched contract with focused checks.
