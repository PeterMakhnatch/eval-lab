# Eval Lab

Eval Lab is an evaluation research lab for agent evaluation in real environments,
with Harbor as its execution engine. It preserves experiment intent, raw evidence,
analysis provenance, and the guarded feedback loop without turning the database
into the only copy of an experiment.

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
under [docs/prompts/](docs/prompts/README.md).
The reusable queries in [research/analysis/queries.sql](research/analysis/queries.sql) cover
leaderboards, exceptions, cost, latency, and artifact-transfer failures.

## Requirements

- Python 3.12 or newer
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
uv run evallab db init
uv run evallab doctor
```

Run both local controls:

```bash
uv run evallab matrix research/experiments/local-controls.json
```

By default, generated jobs go under ignored `runs/`. Inspect and ingest them:

```bash
uv run evallab summarize runs
uv run evallab ingest runs
uv run evallab db list
```

Run one explicit experiment:

```bash
uv run evallab run \
  --task library/tasks/event-summary \
  --agent oracle \
  --name event-summary-oracle-local
```

Direct runs are restricted to the `oracle` and `nop` controls. Billable work is
submitted to the directory queue and admitted only by the committed standing
policy:

```bash
uv run evallab submit /path/to/experiment-spec.json
uv run evallab tick
```

See [docs/operations.md](docs/operations.md) for approvals, STOP/resume, and
queue recovery.

## What is versioned

| Path | Policy |
|---|---|
| `library/tasks/`, `research/experiments/`, `src/`, `sql/`, `docs/`, `docs/prompts/` | Always versioned |
| `research/analysis/` | Versioned SQL and notebook-ready queries |
| `runs/` | Generated, local, ignored |
| `research/evidence/runs/` | Small reviewed controls only; versioned intentionally |
| PostgreSQL volume | Local derived state; never versioned |
| `.env` and credentials | Never versioned |

## Core commands

```bash
uv run evallab doctor
uv run evallab run --help
uv run evallab matrix --help
uv run evallab submit --help
uv run evallab tick
uv run evallab doctor --headless
uv run evallab schedule install
uv run evallab nightly
uv run evallab summarize runs research/evidence/runs
uv run evallab db init
uv run evallab ingest runs research/evidence/runs
uv run evallab db list
uv run pytest
uv run ruff check .
```

`harbor-lab` remains supported as a backwards-compatibility command alias for existing
automation. Canonical commands and documentation use `evallab`.

Generated navigation and status pages are outputs, not hand-edited sources.
Refresh them with the live entrypoints (do not patch the files in place):

```bash
uv run python -m evallab.repomap generate
uv run python -m evallab.docindex generate
uv run evallab status --update
```

`python -m evallab.repomap check` and `python -m evallab.docindex check` fail
closed on a stale committed copy. `evallab status --generate` prints the same
STATUS projection without writing `docs/STATUS.md`.

## Experimental interpretation

Oracle reward `1` shows the task is solvable by its reference solution and that
the verifier accepts the intended output. No-op reward `0` shows the initial
state does not pass. Neither result measures a real model. A model experiment
becomes interpretable only after both controls are healthy and after the
trajectory and verifier evidence have been inspected.

## Repository layout and agent workflow

All work — human and agent — lives inside this one folder. Parallel agent
worktrees are hidden under `.worktrees/` (gitignored). The multi-agent
protocol is `agents/WORKFLOW.md`; the role registry with current status is
`agents/ROLES.md`; per-role handoffs are `agents/handoffs/`. Fleet state at
any moment: `scripts/fleet-status.sh`.
