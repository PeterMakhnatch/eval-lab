# Eval Lab

Eval Lab is an evaluation research lab and workbench for agent evaluation in real environments,
with Harbor as its execution engine. It enforces immutable evidence, verifiable provenance,
and guarded execution feedback loops without turning the database into the only copy of an experiment.

The first checked-in evaluation is deliberately small. The Oracle control must
produce the correct event summary and the no-op control must fail. Together they
exercise the full Harbor path—agent container, artifact collection, separate
verifier, reward parsing, and persisted job result—without calling a model.

## Architecture at a glance

```text
[1. Task & Experiment Spec] ──► [2. Admission & Control Plane] ──► [3. Harbor Execution & Sandbox]
                                                                                │
                                                                                ▼
[6. Trajectory Interpretation] ◄── [5. Metadata Catalog & Lake] ◄── [4. Raw Evidence & CAS (Zone 1)]
              │
              ▼
[7. Governed Feedback & Human Verdicts]
```

Raw Harbor job directories own the evidence: configs, locks, agent logs,
artifacts, verifier output, reward, timing, token use, cost, and exceptions.
PostgreSQL catalogs jobs, trials, rewards, and verdicts. Parquet and DuckDB provide
the fast columnar query surface.

See [docs/SYSTEM-TOUR.md](docs/SYSTEM-TOUR.md) for the end-to-end architectural tour,
[docs/GLOSSARY.md](docs/GLOSSARY.md) for disambiguated terminology and exact code carriers,
[docs/WHERE-DOES-THIS-GO.md](docs/WHERE-DOES-THIS-GO.md) for the file placement decision tree,
and [docs/ACTIVE-VS-HISTORICAL.md](docs/ACTIVE-VS-HISTORICAL.md) for asset lifecycle rules.

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
| `derived/evidence-cas/` | Zone 1 durable content-addressed storage (ignored, immutable) |
| `derived/parquet/` | Zone 3 rebuildable columnar lake (ignored, rebuildable) |
| `research/evidence/runs/` | Small reviewed controls only; versioned intentionally |
| PostgreSQL volume | Local derived state; never versioned |
| `.env` and credentials | Never versioned |

## Authoritative Subpackages

Eval Lab is organized into modular domain subpackages under `src/evallab/`:

- `evallab.schemas`: Pydantic v2 domain schemas, immutable contracts, and join spine invariants.
- `evallab.storage`: Path resolution (`paths.py`), DuckDB unified attach (`attach.py`), Parquet compaction (`parquet_compaction.py`), and historical backfill (`data_backfill.py`).
- `evallab.evidence`: Canonical ATIF normalization (`atif.py`), fact extraction (`facts.py`), and event marts (`event_mart.py`).
- `evallab.interpretation`: Trajectory IR (`trajectory_ir.py`), bounded context packing (`evidence_pack.py`), machine judgment (`trajectory_judgment.py`), quality screening (`trajectory_quality.py`), and platform acceptance gates (`trajectory_acceptance.py`).
- `evallab.recovery`: State recovery certification (`certify.py`), state bundles (`bundle.py`), and paired pilots (`pilot.py`, `wrapper.py`).

## Core commands

```bash
# Control Plane & Operations
uv run evallab doctor
uv run evallab preflight
uv run evallab submit /path/to/experiment-spec.json
uv run evallab approve <spec_id> --actor <name>
uv run evallab tick
uv run evallab stop
uv run evallab resume

# Local Controls & Execution
uv run evallab run --task library/tasks/event-summary --agent oracle
uv run evallab matrix research/experiments/local-controls.json
uv run evallab summarize runs

# Data, Catalog, & Storage
uv run evallab db init
uv run evallab db list
uv run evallab db attach
uv run evallab ingest runs research/evidence/runs
uv run evallab data backfill --all
uv run evallab gc

# Trajectory Interpretation & Analysis
uv run evallab analyze batch <manifest.json>
uv run evallab traj outline <trial_path>
uv run evallab traj ir <trial_path>
uv run evallab traj pack <trial_path>
uv run evallab ladder generate --help
uv run evallab curve build <spec.json>
uv run evallab card generate <spec_id>
uv run evallab verdict record <discovery_id> --status ACCEPTED --by <name>

# Testing & Hygiene
uv run pytest tests/test_repomap.py
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

## Feature-Unblocked Status

Package 1 (Storage & Evidence Layer) and Package 2 (Interpretation & Judgment Engine) are stabilized and locked. Infrastructure migration is complete. Development is fully **FEATURE-UNBLOCKED** for active capability evaluations, difficulty screening, and automated feedback loops.

## Repository layout and agent workflow

All work — human and agent — lives inside this one folder. Parallel agent
worktrees are hidden under `.worktrees/` (gitignored). The multi-agent
protocol is `agents/WORKFLOW.md`; the role registry with current status is
`agents/ROLES.md`; per-role handoffs are `agents/handoffs/`. Fleet state at
any moment: `scripts/fleet-status.sh`.
