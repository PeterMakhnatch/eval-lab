# Operator demo — one truthful analysis loop

This is the M002 vertical slice: completed Harbor evidence becomes a
typed, read-only status snapshot that both the CLI and the dashboard
render. There is no live model call.

## Docker-free proof (always safe)

From a worktree or the main checkout:

```bash
uv run python -m evallab.smoke --docker-free
uv run evallab status --json --from runs/_smoke/<smoke-oracle-*>
uv run evallab status --from runs/_smoke/<smoke-oracle-*>
```

The smoke reuses the existing composed path:

1. experiment spec admitted to `approved/`
2. one tick copies the committed Oracle fixture (free Harbor job)
3. catalog + Parquet facts agree on the job id
4. `research/analysis/stub-oracle-analysis.json` is validated as a
   stage-5 sidecar (saved bytes, agent=`stub`, model=`saved-response`)
5. digest names the control
6. `evallab status` prints Recent / Now / Next / Tasks / Health / Analysis

Every item is labeled `observed`, `unavailable`, `draft`, or
`review-needed`. Status never writes.

The dashboard consumes the same projection:

```bash
uv run evallab dashboard
```

`dashboard/projection.py` calls `evallab.status.build_status_snapshot`.
It does not invent a second meaning.

## What you should see

- **Recent** — the smoke (or evidence) job/trial, joined to experiment
  id, trajectory presence, and analysis id when a sidecar exists.
- **Now** — approved/running specs, or an explicit empty row.
- **Next** — waiting/pending/proposed work, or an explicit empty row.
- **Tasks** — task names from jobs and the queue.
- **Health** — postgres, phoenix, queue, parquet, each labeled.
- **Analysis** — sidecar provenance (`agent`, `model`, digests). An
  unreviewed valid sidecar is `draft`.

Harness exceptions appear on Recent/Tasks with
`scored_as_model_failure=false`. They are not model failures.

## Live checks the integrator still has to start

These are **not** part of the Docker-free smoke or default pytest.
Start them only from the **merged main checkout**, not from a worker
worktree:

| Check | How | Why it is still live |
|---|---|---|
| PostgreSQL catalog | `cd ~/Developer/eval-lab && docker compose up -d postgres` then `uv run evallab db init` | Status probes the catalog; missing Postgres is `unavailable`, not a crash. |
| Phoenix traces | `cd ~/Developer/eval-lab && docker compose up -d phoenix` | Health `phoenix` becomes `observed` on `:6006`. |
| Full smoke | `uv run python -m evallab.smoke` (no `--docker-free`) | Uses real doctor + live Harbor oracle in Docker. |
| Streamlit click-through | `uv run evallab dashboard` after compose is up | Visual only; CLI/dashboard projection tests are the merge bar. |
| Live stage-5 LLM | `evallab analyze plan` / a queued researcher | Out of scope. M002 uses the saved stub only. |

Do not start or stop Compose from `.worktrees/m002-operability`.
