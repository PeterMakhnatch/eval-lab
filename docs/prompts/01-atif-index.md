# Build validated ATIF indexing and Parquet projection

## Mission

Extend Harbor Experiment Lab so it can discover, validate, summarize, and query
ATIF trajectories without modifying the source Harbor job directory. This is the
first implementation of the trajectory analytics plane described in
`docs/architecture.md`.

Work on a named branch/worktree. Read `AGENTS.md`, `docs/architecture.md`,
`docs/analysis-loop.md`, the current result ingester, and Harbor's installed ATIF
models/validator before editing.

## Current state

- `harbor-lab ingest` indexes job/trial/reward/artifact/file metadata in
  PostgreSQL.
- `src/harbor_lab/results.py` inventories files but does not parse ATIF.
- The raw Harbor job directory is immutable and canonical.
- PostgreSQL is rebuildable; large trajectories must not be inserted as blobs.

## Required behavior

1. Discover every Harbor-supported trajectory document in a completed trial,
   including continuation and subagent files where Harbor represents them as
   separate ATIF documents.
2. Validate each document using the installed Harbor schema/validator when
   available. Record a clear invalid/unsupported status rather than crashing the
   ingestion of an otherwise valid job.
3. Extract deterministic facts into typed internal records:
   - trajectory/session/document identity and schema version;
   - step ID, source, timestamps when present, copied-context flag, and LLM call
     count;
   - token and cost metrics;
   - tool call ID, function name, and arguments digest;
   - observation source call ID, content byte length/digest, and subagent refs;
   - source file relative path and SHA-256.
4. Do not copy full prompts, reasoning, tool arguments, or observations into
   PostgreSQL. Preserve raw content only in the canonical ATIF file. Store
   structural/query facts and digests in the catalog.
5. Export step/tool/observation facts to partitioned Parquet under an ignored
   `derived/` directory. Partition keys must be stable and include at least the
   source job and trial identities. Re-export must be deterministic and
   idempotent.
6. Add a `harbor-lab trajectories` command that reports validation status and
   basic counts from raw job directories without requiring PostgreSQL.
7. Add a `harbor-lab trajectories export` command, or an equally clear
   subcommand, that writes Parquet and prints the exact output paths and row
   counts.
8. Extend the PostgreSQL schema only for document-level catalog facts and
   Parquet locations needed for discovery. Keep schema changes idempotent.
9. Document example DuckDB queries over the produced Parquet files. Use DuckDB
   as a query client, not a daemon or new source of truth.

## Privacy and integrity requirements

- Never edit, normalize in place, or repair the source trajectory.
- Do not log trajectory content during ordinary indexing.
- Treat reasoning content, prompts, tool arguments, and observations as
  potentially sensitive.
- Every derived row must be traceable to a source path, document digest, and
  step/tool identifier.
- A failed trajectory validation must not be mislabeled as an agent failure.

## Tests

Create small fixture job directories covering:

- one valid trajectory with tool call and observation;
- copied context that remains identifiable;
- continuation or subagent trajectory discovery;
- invalid schema or broken reference;
- a completed trial with no ATIF;
- repeat export producing identical row counts and content;
- source files unchanged before and after export;
- sensitive raw text absent from PostgreSQL rows and ordinary CLI output.

Do not use committed production trajectories if a minimal synthetic fixture can
exercise the parser.

## Acceptance commands

At minimum, run:

```bash
uv run pytest
uv run ruff check .
uv run harbor-lab trajectories evidence/runs
```

Also run a local DuckDB query against the fixture-derived Parquet if the chosen
dependency makes that possible without an external service.

## Handoff

Update the README and architecture/operations docs with the exact commands,
output paths, schema limitations, and unsupported ATIF cases. Report changed
files, validations run, and any dependency/version decision. Do not commit
generated `derived/` files.

## Implemented contract (ANALYST, 2026-08-14)

- `harbor-lab trajectories [paths...]` reports `valid`, `invalid`,
  `unsupported`, or `none` without PostgreSQL; `--export` writes all ATIF and
  trial-fact tables beneath `derived/parquet/job_id=*/trial_id=*/`.
- Harbor is installed as isolated tool version 0.21.0 and cannot be imported by
  the project venv. `harbor_lab.atif` uses Harbor's Pydantic model when it is
  importable and otherwise records use of its strict ATIF-v1.0–v1.7 fallback.
- The fallback validates sequential steps, tool/observation links, embedded
  subagent IDs, continuations, and local external subagent files. Remote
  trajectory references are unsupported; missing/escaping references are
  invalid status, never an agent exception.
- Raw prompts, reasoning, tool arguments, and observations remain in ATIF.
  Derived rows contain only structure, counts, byte lengths, and SHA-256
  digests. See `research/analysis/README.md` and `queries.sql`.
- Fixture coverage lives under `research/analysis/tests/` because ANALYST may
  not write BUILDER-owned `tests/`. The focused suite includes rebuild,
  idempotency, copied context, continuation/subagent discovery, broken refs,
  no-ATIF, DuckDB, privacy, and raw-byte immutability checks.
