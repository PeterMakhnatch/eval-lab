Status: done
Last: PR #11 squash-merged after all checks passed; post-merge main workflows are green.
Next: none
Blockers: none

# DASHBOARD handoff

## 2026-08-14 entry gate

- REFRAME handoff: `Status: done`.
- Branch/worktree: `role/dashboard` at `.worktrees/dashboard`, based on `origin/main`.
- Scope: `dashboard/`, this handoff, additive dashboard dependency lines, and the CLI command.
- Design boundary: PostgreSQL connections force `default_transaction_read_only=on`; DuckDB is
  in-memory and reads existing Parquet; the app contains no approval or state-transition controls.

## Implementation checkpoint

- `dashboard/app.py`: Streamlit overview with leaderboard, canary, spend, queue, calibration,
  ATIF activity, and DISCOVERIES panes.
- `dashboard/queries.py`: SELECT-only PostgreSQL queries, in-memory DuckDB Parquet queries, and
  read-only file projections. Leaderboard pass@1 and calibration agreement use `cohort.py`'s
  Wilson 95% interval; every rendered estimate carries its denominator.
- `evallab dashboard`: launches pinned Streamlit 1.61.1 through an ephemeral `uv run --with`.
  This avoids changing `uv.lock`, which is outside this role's explicit write scope.
- Fixture suite: `uv run pytest dashboard/tests -q` — 8 passed.
- Repository suite: `uv run pytest -q` — 54 passed.
- Targeted lint: `uv run ruff check dashboard src/evallab/cli.py` — clean.
- Live data snapshot: one leaderboard group, seven spend days, five queue states, one measured
  calibration, three ATIF-derived trial rows, and one discovery; 0.0494s at the query layer.
- Streamlit AppTest against `~/Developer/eval-lab`: all seven headers rendered, no exceptions,
  0.177s uncached app snapshot. The local health endpoint returned `ok`.
- Mutation proof: a deliberate `CREATE TEMP TABLE` through `ReadOnlyPostgres` failed with
  `ReadOnlySqlTransaction`; grep found no mutation SQL or filesystem-write calls in production
  dashboard sources.
- Browser note: no in-app or extension browser instance was connected, so the official Streamlit
  AppTest renderer supplied the element-level render proof instead of a screenshot.

## Pre-PR gate

- Rebased cleanly on `origin/main` at `e758df6`; dashboard implementation head before this handoff
  update: `0ca449f436cc1446df31a767192ce905e6283cee`.
- `make premerge` after rebase: pass — Ruff clean, 54 tests, ty 33 <= 33.
- `uv run pytest dashboard/tests -q` after rebase: 8 passed.

## Merge and closeout

- PR: https://github.com/PeterMakhnatch/eval-lab/pull/11
- Exact green head: `5bbd450e9f7ece7a4dfe07ee765d021c3f25df6d`.
- PR checks: quality run `31828924640` — lint, Python 3.12, and Python 3.14 passed;
  typecheck run `31828924657` — ty passed.
- Squash merge: `9afa97cc27eb8c33f816dc5c7584d96daf63b5ea`, 2026-08-14 18:30:52Z.
- Post-merge main: quality run `31829005654` and typecheck run `31829005672` passed.
- Latest main `acb6d335d9e6d9ce0738bc3a75e2e25e16ae31cf` also has green quality and
  typecheck runs (`31829073788`, `31829073883`).
- The spent `role/dashboard` branch was deleted locally and remotely. This handoff update is on
  fresh branch `role/dashboard-closeout` from `origin/main`; the old branch was not reused.
