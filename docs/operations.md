# Operations

## Local database

Copy `.env.example` to `.env`, change the password if the database is exposed
beyond localhost, and start PostgreSQL:

```bash
docker compose up -d postgres
uv run harbor-lab db init
uv run harbor-lab db list
```

`docker compose down` stops PostgreSQL and preserves the named volume. Deleting
the volume is intentionally not part of a Make target.

## Run controls before model experiments

```bash
uv run harbor-lab matrix experiments/local-controls.json
uv run harbor-lab summarize runs
```

Expected outcome: Oracle has `reward=1`, no-op has `reward=0`, neither has an
exception, and both have verifier evidence. If either expectation fails, debug
the task or harness before spending model tokens.

## Ingest and query

```bash
uv run harbor-lab ingest runs evidence/runs
uv run harbor-lab db list --limit 50
```

The ingester is idempotent. It updates jobs and trials by Harbor UUID, replaces
their reward/artifact/file inventories, and leaves raw files untouched.

## Evidence promotion checklist

Only small representative runs belong in `evidence/runs/`:

1. Confirm the run has final job and trial `result.json` files.
2. Inspect agent/verifier logs and all declared artifacts.
3. Check that no token, `.env`, prompt secret, credential file, or unrelated
   user data is present.
4. Confirm each file is small enough for ordinary Git review.
5. Copy the complete small job directory and run `uv run harbor-lab summarize`.
6. Run the repository secret/size audit in `uv run pytest` before commit.

For large evidence, leave the job in ignored `runs/`, ingest its metadata, and
write a small report with digests and a durable artifact URI once object storage
is configured.

