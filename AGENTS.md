# Harbor Experiment Lab

This private repository is the durable home for Peter Makhnatch's Harbor-style
evaluation experiments. Keep evaluation definitions, infrastructure, analysis
code, and small curated evidence here. Treat generated runs as immutable once
they have been promoted to `evidence/runs/`.

## Working rules

- Read this file and `docs/architecture.md` before substantial changes.
- Do not invoke a paid model, cloud sandbox, large sweep, deploy, or publish a
  task without explicit approval.
- `oracle` and `nop` are the default local controls. They test task and harness
  validity; they are not evidence of model capability.
- Never commit `.env`, API keys, OAuth data, database volumes, unredacted model
  prompts, or arbitrary large run directories.
- Keep Harbor job directories under `runs/` during exploration. Promote only a
  small, reviewed evidence bundle to `evidence/runs/`.
- Preserve hidden verifier inputs: do not place `tests/` or `solution/` in an
  evaluated agent's environment image.
- Change one experimental variable at a time and record the exact task, agent,
  model, Harbor version, and run name.
- PostgreSQL is a derived search/index layer. The Harbor job directory is the
  immutable source of truth and must remain interpretable without the database.
- Add schema changes idempotently to `sql/schema.sql` and cover parsers with
  fixture-based tests.
- Use `uv run pytest`, `uv run ruff check .`, and `uv run harbor-lab doctor`
  before a meaningful checkpoint.

## Repository map

- `tasks/`: Harbor task definitions and deterministic verifiers.
- `experiments/`: checked-in run matrices and hypotheses.
- `src/harbor_lab/`: run, inspect, summarize, and ingest tooling.
- `sql/`: PostgreSQL schema and analysis views.
- `runs/`: ignored raw Harbor output.
- `evidence/runs/`: small, intentionally tracked control runs.
- `docs/`: architecture, operating procedures, and scaling decisions.

## Safe run pattern

Use the wrapper so run provenance is recorded and billable adapters require an
explicit acknowledgement:

```bash
uv run harbor-lab run \
  --task tasks/event-summary \
  --agent oracle \
  --name event-summary-oracle
```
