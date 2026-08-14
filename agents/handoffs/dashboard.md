Status: building
Last: Rendered every pane against live data in 0.177s; 8 dashboard tests and 54 repo tests pass.
Next: Run the full premerge gate, rebase on origin/main, and publish the DASHBOARD PR.
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
