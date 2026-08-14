# Harbor Experiment Lab

A private, local-first lab for authoring Harbor tasks, running controlled agent
evaluations, preserving raw evidence, and querying many runs without turning the
database into the only copy of the experiment.

The first checked-in evaluation is deliberately small. The Oracle control must
produce the correct event summary and the no-op control must fail. Together they
exercise the full Harbor path—agent container, artifact collection, separate
verifier, reward parsing, and persisted job result—without calling a model.

## Architecture at a glance

```text
task + experiment spec
          |
          v
 Harbor execution + verification
          |
          v
 immutable job directory -----> reviewed evidence bundle
          |
          +----> PostgreSQL metadata catalog
          |
          +----> ATIF -> Parquet/DuckDB analytics (next)
          |
          v
 deterministic comparison -> structured agent analysis
          |
          v
 reviewed experiment proposal -> approval -> next run
```

Raw Harbor job directories own the evidence: configs, locks, agent logs,
artifacts, verifier output, reward, timing, token use, cost, and exceptions.
PostgreSQL currently indexes those files as jobs, trials, rewards, artifacts,
and file digests. Experiment, trajectory, and analysis records are staged in the
architecture plan rather than claimed as implemented. Large blobs stay out of
PostgreSQL and Git.

See [docs/architecture.md](docs/architecture.md) for the system boundaries,
[docs/analysis-loop.md](docs/analysis-loop.md) for the evidence-to-experiment
state machine, and [docs/scaling.md](docs/scaling.md) for the gates governing
object storage, Kubernetes, and ClickHouse. Ordered implementation briefs live
under [prompts/](prompts/README.md).
The reusable queries in [analysis/queries.sql](analysis/queries.sql) cover
leaderboards, exceptions, cost, latency, and artifact-transfer failures.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- Harbor 0.21 or newer on `PATH`
- Docker Desktop or another Docker daemon
- Docker Compose v2

No model credential is needed for the control experiments.

## Quick start

```bash
uv sync
cp .env.example .env
docker compose up -d postgres
uv run harbor-lab db init
uv run harbor-lab doctor
```

Run both local controls:

```bash
uv run harbor-lab matrix experiments/local-controls.json
```

By default, generated jobs go under ignored `runs/`. Inspect and ingest them:

```bash
uv run harbor-lab summarize runs
uv run harbor-lab ingest runs
uv run harbor-lab db list
```

Run one explicit experiment:

```bash
uv run harbor-lab run \
  --task tasks/event-summary \
  --agent oracle \
  --name event-summary-oracle-local
```

Direct runs are restricted to the `oracle` and `nop` controls. Billable work is
submitted to the directory queue and admitted only by the committed standing
policy:

```bash
uv run harbor-lab submit /path/to/experiment-spec.json
uv run harbor-lab tick
```

See [docs/operations.md](docs/operations.md) for approvals, STOP/resume, and
queue recovery.

## What is versioned

| Path | Policy |
|---|---|
| `tasks/`, `experiments/`, `src/`, `sql/`, `docs/`, `prompts/` | Always versioned |
| `analysis/` | Versioned SQL and notebook-ready queries |
| `runs/` | Generated, local, ignored |
| `evidence/runs/` | Small reviewed controls only; versioned intentionally |
| PostgreSQL volume | Local derived state; never versioned |
| `.env` and credentials | Never versioned |

## Core commands

```bash
uv run harbor-lab doctor
uv run harbor-lab run --help
uv run harbor-lab matrix --help
uv run harbor-lab submit --help
uv run harbor-lab tick
uv run harbor-lab summarize runs evidence/runs
uv run harbor-lab db init
uv run harbor-lab ingest runs evidence/runs
uv run harbor-lab db list
uv run pytest
uv run ruff check .
```

## Experimental interpretation

Oracle reward `1` shows the task is solvable by its reference solution and that
the verifier accepts the intended output. No-op reward `0` shows the initial
state does not pass. Neither result measures a real model. A model experiment
becomes interpretable only after both controls are healthy and after the
trajectory and verifier evidence have been inspected.
