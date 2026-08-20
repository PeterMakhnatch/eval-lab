# M030 TRAJ handoff

## Status
PR #142 is open from `role/m030-traj` to `main`. Commit `782df9f` is pushed. No billable models run. Do not merge.

## Last
- `git fetch origin && git worktree list --porcelain && git -C .worktrees/m030-traj status --short --branch`
  - Existing worktree preserved at `role/m030-traj`; partial M030 files retained.
- `uv sync --quiet && bash scripts/setup-git.sh`
  - Hooks configured; generated-doc merge attributes confirmed.
- `uv run pytest tests/test_traj.py`
  - `19 passed`.
- `uv run pytest tests/test_attach.py tests/test_attach_properties.py tests/test_cli_audit.py tests/test_cli_registry.py`
  - `157 passed`.
- `uv run ruff check src/evallab/traj.py src/evallab/attach.py tests/test_traj.py`
  - `All checks passed!`.
- `uv run evallab traj outline research/evidence/runs/canary-event-summary-codex-20260815/event-summary__h2D9f6f`
  - Rendered `[FEATURED]`, 11 steps, 5 tools, 0 errors, token/cost summary, ordered phases, and first-edit timing.
- `uv run evallab traj queue --limit 3 --runs-dir research/evidence/runs`
  - Emitted 3 deterministic unlabeled real-agent candidates; no labels were persisted.
- Mutation checks:
  - SQL mutation `WHERE loop_suspicion_detected` → `WHERE loop_suspicion_score > 0.99` made `test_sql_traj_views_with_data` fail (`assert 0 == 1`).
  - Python mutation repeated-error threshold `>= 3` → `>= 5` made `test_loop_suspicion_failing_commands` fail (`repeated_error_count == 0`).
  - Source was restored; targeted tests passed afterward.
- `env -u EVALLAB_DERIVED_ROOT bash scripts/premerge.sh`
  - Exit code `1`.
  - Completed `1563 passed, 2 skipped, 1 xfailed`; failed only at `tests/test_repomap.py::test_check_passes_on_real_repository_tree` because `docs/repo-map.md` was stale.
- `uv run python -m evallab.repomap generate && uv run python -m evallab.docindex generate`
  - Wrote both generated indexes.
- `uv run pytest tests/test_repomap.py tests/test_docindex.py && uv run python -m evallab.repomap check && uv run python -m evallab.docindex check`
  - `27 passed`; `repomap check passed`; `docindex check passed`.
- `git commit -m "feat: add deterministic trajectory analysis"`
  - `[role/m030-traj 782df9f] feat: add deterministic trajectory analysis`.
- `git push -u origin role/m030-traj`
  - Created and pushed `origin/role/m030-traj`.
- `gh pr create --base main --head role/m030-traj ...`
  - Opened `https://github.com/PeterMakhnatch/eval-lab/pull/142`.

## Next
- Reviewer/CI owns PR #142. Do not merge.

## Blockers
The one required `premerge.sh` invocation exited `1` on stale generated documentation; the generated files were refreshed and their dedicated checks pass. A second full premerge invocation was not run because the assignment required running it once.
