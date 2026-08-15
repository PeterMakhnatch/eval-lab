# Eval Lab dashboard

`uv run evallab dashboard` starts the local Streamlit operator view at
`http://127.0.0.1:8501`. It renders the same typed snapshot as
`evallab status [--json]` (`Recent`, `Now`, `Next`, `Tasks`, `Health`,
`Analysis`). Missing Postgres or Phoenix stay labeled `unavailable` instead
of crashing the page. Approvals and all other state transitions remain
CLI-only.

The query helpers in `queries.py` remain SELECT-only catalog utilities for
tests and later panes. They are not a second operator meaning.

The root lockfile is outside DASHBOARD's write scope, so the CLI supplies the
observed Streamlit 1.61.1 release as a pinned, ephemeral `uv run --with`
dependency. The app's own data-load timing is shown at the bottom of the page
and excludes that environment setup.

Run the dashboard query suite explicitly because the repository's root pytest
configuration intentionally limits normal discovery to `tests/`:

```bash
uv run pytest dashboard/tests
```
