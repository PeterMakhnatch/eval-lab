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
  Harbor local Docker run
          |
          v
 immutable job directory  ----> reviewed small bundle in evidence/runs/
          |
          v
 harbor-lab ingest
          |
          v
 PostgreSQL metadata index ----> SQL / notebooks / future dashboards
```

Raw Harbor job directories own the evidence: configs, locks, agent logs,
artifacts, verifier output, reward, timing, token use, cost, and exceptions.
PostgreSQL indexes those files as experiments, trials, rewards, artifacts, and
file digests. Large blobs stay out of PostgreSQL and Git.

See [docs/architecture.md](docs/architecture.md) for the trade-offs and the
threshold for adding S3-compatible storage or Kubernetes.
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

Adapters other than `oracle` and `nop` require `--allow-billable`; this prevents
an experiment typo from silently invoking a paid model. The flag is an explicit
acknowledgement, not a credential or cost limit.

## What is versioned

| Path | Policy |
|---|---|
| `tasks/`, `experiments/`, `src/`, `sql/`, `docs/` | Always versioned |
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
