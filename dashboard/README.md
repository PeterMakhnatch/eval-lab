# Eval Lab dashboard

`uv run evallab dashboard` starts the local Streamlit research overview at
`http://127.0.0.1:8501`. Its operator-status section renders the same typed snapshot as
`evallab status [--json]` (`Recent`, `Now`, `Next`, `Tasks`, `Health`,
`Analysis`). Missing Postgres or Phoenix stay labeled `unavailable` instead
of crashing the page. The established leaderboard, canary, spend, queue,
calibration, ATIF, and discovery panes remain available below it. Approvals
and all other state transitions remain CLI-only.

The query helpers in `queries.py` remain SELECT-only catalog utilities. They
render research measurements; they do not redefine operator status.

The root lockfile is outside DASHBOARD's write scope, so the CLI supplies the
observed Streamlit 1.61.1 release as a pinned, ephemeral `uv run --with`
dependency. The app's own data-load timing is shown at the bottom of the page
and excludes that environment setup.

Run the dashboard query suite explicitly because the repository's root pytest
configuration intentionally limits normal discovery to `tests/`:

```bash
uv run pytest dashboard/tests
```

## Explorer page (M005)

`dashboard/explorer.py` is the read-only drill-down companion to the
overview: Tasks → Jobs/Trials → Trajectory → Artifacts → Analyses, every
field labeled observed / derived / draft / unavailable, infrastructure
exceptions separated from reward failures, and Next Action rendered as
shell-safe, copyable `evallab` / `harbor view <jobs-root> --jobs` commands
that the page never executes.

```bash
uv run --with streamlit==1.61.1 streamlit run dashboard/explorer.py   # repo evidence
EVALLAB_EXPLORER_ROOT=tests/fixtures/explorer \
  uv run --with streamlit==1.61.1 streamlit run dashboard/explorer.py # fixture demo
```

Logic lives in `src/evallab/explorer.py` (`uv run pytest tests/test_explorer.py`).
