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

## Standing-policy queue

The committed policy in `policy/standing-approvals.yaml` is the unattended
executor's authorization boundary. Only Peter changes that file. Agents submit
a Pydantic-validated experiment spec; the policy gate moves it atomically to
`approved/` or `waiting/` and records every transition in ignored local
`queue/events.jsonl`.

Example control spec:

```json
{
  "schema_version": 1,
  "name": "event-summary-oracle-queue-1",
  "hypothesis": "The reference solution remains accepted.",
  "task": "tasks/event-summary",
  "agent": "oracle",
  "submitted_by": "peter",
  "priority": 100,
  "est_cost_usd": 0
}
```

Submit and drain:

```bash
uv run harbor-lab submit /path/to/spec.json
uv run harbor-lab tick
```

Out-of-policy work waits for an explicit decision:

```bash
uv run harbor-lab approve <spec-id> --actor peter
uv run harbor-lab reject <spec-id> --actor peter --reason "not this week"
```

An approval does not override the hard per-job or daily cost ceiling. Pause new
dispatch after the current trial with `uv run harbor-lab stop`; re-enable it
with `uv run harbor-lab resume`. A restart reconciles `queue/running/` against
completed immutable Harbor jobs before starting new work.

The legacy `run` and `matrix` commands are restricted to Oracle/no-op controls.
All real-model work must pass through the queue and standing policy.

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
