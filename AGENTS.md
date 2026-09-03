# Eval Lab

This private repository is the durable home for Peter Makhnatch's agent-evaluation
research in real environments. Harbor is the execution engine; this lab owns the
evaluation definitions, infrastructure, analysis code, and small curated evidence.
Treat generated runs as immutable once promoted to `research/evidence/runs/`.

## Working rules

- **Read [`agents/CONTEXT-HUB.md`](agents/CONTEXT-HUB.md) FIRST, every session.** It is the single source of truth for current focus, model policy, and open items. If anything contradicts it, the hub wins; update the hub when decisions change instead of briefing agents individually.
- Read this file and `docs/architecture.md` before substantial changes.
- Treat this as a Python repository. New application code, adapters, verifiers,
  and benchmark tasks must be Python. Shell, SQL, Dockerfiles, and data/config
  formats are allowed as supporting files. Do not add Java/JVM code or build
  tooling. Ask Peter before introducing TypeScript or another programming
  language, including inside an imported or generated task.
- Do not invoke a paid model, cloud sandbox, large sweep, deploy, or publish a
  task without explicit approval. Cloud execution (Modal) is installed but
  token-gated; read `docs/execution-tiers.md` before deciding where any task
  can run or whether you may run it.
- `oracle` and `nop` are the default local controls. They test task and harness
  validity; they are not evidence of model capability.
- Never commit `.env`, API keys, OAuth data, database volumes, unredacted model
  prompts, or arbitrary large run directories.
- Keep Harbor job directories under `runs/` during exploration. Promote only a
  small, reviewed evidence bundle to `research/evidence/runs/`.
- Preserve hidden verifier inputs: do not place `tests/` or `solution/` in an
  evaluated agent's environment image.
- Change one experimental variable at a time and record the exact task, agent,
  model, Harbor version, and run name.
- PostgreSQL is a derived search/index layer. The Harbor job directory is the
  immutable source of truth and must remain interpretable without the database.
- Add schema changes idempotently to `sql/schema.sql` and cover parsers with
  fixture-based tests.
- Use `uv run pytest`, `uv run ruff check .`, and `uv run evallab doctor`
  before a meaningful checkpoint.
- Make meaningful changes on a named branch and open a pull request; do not push
  directly to `main` unless Peter explicitly asks. Treat every `quality` check
  as required even when the GitHub plan cannot enforce branch protection.
- The authoring agent must run the repository checks before pushing. After CI,
  Peter or a different agent reviews the pull request before merge.

## Repository map

The full, binding layout — including where anything new must go — is
`agents/STRUCTURE.md`. The root is frozen; adding a top-level entry requires
editing that file in the same PR. Buckets in one line each:

- `agents/`: coordination — workflow, role registry, structure, handoffs.
- `docs/`: design and decisions; implementation briefs under `docs/prompts/`.
- `library/`: evaluable task supply — `tasks/`, `curated/`, `adapters/`.
- `research/`: produced knowledge — `experiments/`, `calibration/`,
  `explorations/`, `analysis/`, `evidence/` (reviewed control runs).
- `policy/`: committed standing approvals; agents must never loosen this policy.
- `src/evallab/`, `tests/`, `sql/`, `scripts/`: the lab software.
- `digests/`: committed daily derived reports.
- `queue/`, `runs/`, `derived/`, `backups/`: ignored runtime state;
  `queue/events.jsonl` drives unattended work.

## Safe run pattern

Use the wrapper so run provenance is recorded and billable adapters require an
explicit acknowledgement:

```bash
uv run evallab run \
  --task library/tasks/event-summary \
  --agent oracle \
  --name event-summary-oracle
```
