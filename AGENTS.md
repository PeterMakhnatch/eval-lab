# Harbor Experiment Lab

This private repository is the durable home for Peter Makhnatch's Harbor-style
evaluation experiments. Keep evaluation definitions, infrastructure, analysis
code, and small curated evidence here. Treat generated runs as immutable once
they have been promoted to `evidence/runs/`.

## Working rules

- Read this file and `docs/architecture.md` before substantial changes.
- Treat this as a Python repository. New application code, adapters, verifiers,
  and benchmark tasks must be Python. Shell, SQL, Dockerfiles, and data/config
  formats are allowed as supporting files. Do not add Java/JVM code or build
  tooling. Ask Peter before introducing TypeScript or another programming
  language, including inside an imported or generated task.
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
- Make meaningful changes on a named branch and open a pull request; do not push
  directly to `main` unless Peter explicitly asks. Treat every `quality` check
  as required even when the GitHub plan cannot enforce branch protection.
- The authoring agent must run the repository checks before pushing. After CI,
  Peter or a different agent reviews the pull request before merge.

## Repository map

- `tasks/`: Harbor task definitions and deterministic verifiers.
- `experiments/`: checked-in run matrices and hypotheses.
- `src/harbor_lab/`: run, inspect, summarize, and ingest tooling.
- `sql/`: PostgreSQL schema and analysis views.
- `runs/`: ignored raw Harbor output.
- `queue/`: ignored runtime state; atomic files and `events.jsonl` drive unattended work.
- `policy/`: committed standing approvals; agents must never loosen this policy.
- `digests/`: committed daily derived reports; nightly stages only its dated digest.
- `evidence/runs/`: small, intentionally tracked control runs.
- `docs/`: architecture, operating procedures, and scaling decisions.
- `prompts/`: ordered, bounded implementation briefs for coding agents.

## Safe run pattern

Use the wrapper so run provenance is recorded and billable adapters require an
explicit acknowledgement:

```bash
uv run harbor-lab run \
  --task tasks/event-summary \
  --agent oracle \
  --name event-summary-oracle
```
