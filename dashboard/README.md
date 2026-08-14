# Eval Lab dashboard

`uv run evallab dashboard` starts the local Streamlit research overview at
`http://127.0.0.1:8501`. It reads PostgreSQL with
`default_transaction_read_only=on`, queries ATIF-derived Parquet through an
in-memory DuckDB connection, and reads queue, policy, calibration, and discovery
files without changing them. Approvals and all other state transitions remain
CLI-only.

The root lockfile is outside DASHBOARD's write scope, so the CLI supplies the
observed Streamlit 1.61.1 release as a pinned, ephemeral `uv run --with`
dependency. The app's own data-load timing is shown at the bottom of the page
and excludes that environment setup.

Run the dashboard query suite explicitly because the repository's root pytest
configuration intentionally limits normal discovery to `tests/`:

```bash
uv run pytest dashboard/tests
```
