Status: review-wanted
Last: opened PR #42; smoke x3, 313 pytest, ruff, premerge green
Next: wait for GitHub checks; do not self-merge
Blockers: none; never self-merge M002 #42

Worktree `.worktrees/m002-operability` on `role/m002-operability` from
`origin/main` `9ed9874`.

## Slice

Docker-free smoke now proves spec → approved queue → Oracle fixture →
Harbor job → catalog + Parquet → saved stub stage-5 sidecar → digest →
typed `evallab status` snapshot. No live model call.

`evallab status [--json] [--from PATH]` emits Recent, Now, Next, Tasks,
Health, Analysis. Every item is `observed` / `unavailable` / `draft` /
`review-needed`. Status is a pure reader. The dashboard renders
`dashboard.projection.load_operator_snapshot` → `build_status_snapshot`.

Postgres/Phoenix are probed only on a real lab checkout. Scratch/fixture
trees label those stores `unavailable` instead of borrowing the host.

## Verification

- smoke ×3: `SMOKE PASS both-stores-agree`; each names job, catalog,
  parquet, sidecar, digest, status sections
- `uv run pytest`: 313 passed
- `uv run ruff check .`: All checks passed
- `scripts/premerge.sh`: `premerge green: Python 3.12; ty 28 <= 28`
- status JSON ×2 against last smoke scratch; human `status.txt`
- `status-cold.json` against `tests/fixtures/operability/missing-stores`
  (postgres/phoenix/queue/parquet `unavailable`, no traceback)
- `status-nowrite.txt`: fixture bytes/mtime unchanged

Scratch:
`/var/folders/zv/j4ds5l7j01ldjyw0t0yzcv8w0000gn/T/grok-goal-f2968a49a6fd/implementer/`

## Live integrator checks (merged main only)

Do not start Compose from this worktree. After merge, from
`~/Developer/eval-lab`:

- `docker compose up -d postgres` then `uv run evallab db init`
- `docker compose up -d phoenix` for Health phoenix=observed
- `uv run python -m evallab.smoke` (no `--docker-free`) for live Harbor
- `uv run evallab dashboard` for the Streamlit click-through

Details: `docs/operator-demo.md`.

PR: https://github.com/PeterMakhnatch/eval-lab/pull/42
