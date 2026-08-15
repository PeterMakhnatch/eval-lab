# Operations

## Local database

Copy `.env.example` to `.env`, change the password if the database is exposed
beyond localhost, and start PostgreSQL:

```bash
docker compose up -d postgres
uv run evallab db init
uv run evallab db list
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
  "task": "library/tasks/event-summary",
  "agent": "oracle",
  "submitted_by": "peter",
  "priority": 100,
  "est_cost_usd": 0
}
```

Submit and drain:

```bash
uv run evallab submit /path/to/spec.json
uv run evallab tick
```

Out-of-policy work waits for an explicit decision:

```bash
uv run evallab approve <spec-id> --actor peter
uv run evallab reject <spec-id> --actor peter --reason "not this week"
```

An approval does not override the hard per-job or daily cost ceiling. Pause new
dispatch after the current trial with `uv run evallab stop`; re-enable it
with `uv run evallab resume`. A restart reconciles `queue/running/` against
completed immutable Harbor jobs before starting new work.

The legacy `run` and `matrix` commands are restricted to Oracle/no-op controls.
All real-model work must pass through the queue and standing policy.

## Headless readiness and scheduling

The scheduler fails closed. This command prints a versioned JSON object whose
runtime fields are booleans only:

```bash
uv run evallab doctor --headless
```

It checks that the Claude OAuth Keychain item is readable without a GUI prompt,
Codex's `~/.codex/auth.json` exists, Docker is reachable, PostgreSQL responds,
and at least the configured disk headroom remains. It never prints or stores a
credential value. The default Keychain service remains
`harbor-practice-claude-oauth` until brief 11 migrates the auth scripts; override
only the service/account names with `HARBOR_CLAUDE_KEYCHAIN_SERVICE` and
`HARBOR_CLAUDE_KEYCHAIN_ACCOUNT`.

Install the two user-session LaunchAgents:

```bash
uv run evallab schedule install
```

- `com.petermakhnatch.evallab.tick` runs every 30 minutes.
- `com.petermakhnatch.evallab.nightly` runs at 02:30 local time.

Both invoke `/bin/zsh -lc 'cd <repo> && uv run evallab …'`. Logs live under
`~/Library/Logs/evallab/`. Reinstalling replaces and reloads the definitions.
Because these are LaunchAgents, not system daemons, they run inside the logged-in
user session where Keychain access is possible. The plist supplies a bounded
command `PATH` including `~/.local/bin`, so launchd can find `uv` without
depending on interactive shell startup files. It also captures the resolved,
non-secret `EVALLAB_DERIVED_ROOT`; reinstall the schedule after changing that
setting.

Queue events rotate before an append would take `queue/events.jsonl` past
10 MiB. Seven numbered archives are retained (`events.jsonl.1` is newest), and
all application readers scan the archives from oldest to newest before the
active file. A process lock serializes rotation and appends across launchd and
manual commands. The oldest archive is the only event file removed during a
rotation; size the retained window into any external backup or audit policy.

`tick` and `nightly` both run the headless doctor first. If any check fails,
they append a boolean-only quarantine event and dispatch nothing. In particular,
a locked or unreadable Keychain produces zero reward-bearing trials rather than
misclassifying authentication failures as agent failures.

## Executor resilience boundary

Every queue spec carries `timeout_seconds` (default 1,800; maximum 21,600). The
executor gives the Harbor process one such wall-clock allowance per requested
attempt, runs it in a separate process group, and terminates that group when the
deadline expires. Timeout evidence remains under the job directory and the queue
records `trial_wall_clock_timeout`; it is never reported as an agent score.

After an interrupted Harbor run, cleanup snapshots only containers with all of
these properties: Docker Compose project labels, a Compose config path inside
`harbor/environments/docker`, a working directory under the current task, and a
Compose project matching a trial-session directory recorded in the current job.
The container ID must also have been absent before the run. Only those exact IDs
are passed to `docker rm -f`. The executor never runs a global container, image,
volume, or system prune, so the lab's PostgreSQL/Phoenix Compose project,
concurrent Harbor jobs, and unrelated containers are outside the cleanup set.

Provider HTTP 429 and 5xx failures are normalized to `transient_harness` with
the more specific queue reason `transient_harness:provider_http_429` or
`transient_harness:provider_http_5xx`. The executor permits at most two
infrastructure retries, with 5- then 10-second backoff (hard-capped at 30
seconds). Failed attempt evidence moves to
`runs/.transient-attempts/<job>/attempt-N/` before the same declared job is
retried. A billable retry reserves another full `est_cost_usd` and is refused if
that reservation would cross the standing ceiling. Transient provider capacity
is shown separately in digests and skipped, rather than counted or treated as a
success, by the quiet-failure circuit breaker.

Harbor subprocesses receive a non-secret environment allowlist. Model API-key
variables are neither accessed nor forwarded; supported model access remains
subscription authentication through Keychain or the agent's auth file.

Render a digest on demand (the file date is the morning/report date; its primary
reporting period is the preceding catalog day):

```bash
uv run evallab digest --date 2026-08-14
```

The nightly command additionally commits only `digests/YYYY-MM-DD.md`; unrelated
working-tree changes are never staged:

```bash
uv run evallab nightly
```

The digest includes the prior day's trials, early-morning automation, policy
attribution, cost, exception taxonomy, queue depth, waiting rationales, evidence
growth, and quarantine state. PostgreSQL remains rebuildable from raw jobs; the
digest is a human-facing derived report.

Nightly also validates and enqueues the pinned suite in
[`canaries.md`](canaries.md) before draining the queue. A changed task digest or
floating source ref quarantines the cycle before dispatch. Digest excursions
are labeled harness-drift suspects, never capability news.

## Run controls before model experiments

```bash
uv run evallab matrix research/experiments/local-controls.json
uv run evallab summarize runs
```

Expected outcome: Oracle has `reward=1`, no-op has `reward=0`, neither has an
exception, and both have verifier evidence. If either expectation fails, debug
the task or harness before spending model tokens.

## Pinned benchmark fetch

Acquire a Harbor Hub dataset into `library/benchmarks/<name>/` at an immutable
pin. `@latest`, `@head`, and other unpinned refs are refused.

```bash
uv run evallab fetch --list
uv run evallab fetch hello-world@1.0
uv run evallab fetch hello-world@1.0 --verify-sample 1
uv run evallab fetch --audit
```

`--verify-sample N` runs free `oracle` then `nop` on N tasks with Harbor
`-n` ≤ 2 and writes the rewards into that bench's `MANIFEST.md`. Re-fetching
the same pin verifies the recorded Harbor sync digest and no-ops. Existing
INGEST pins (`aime`, `gpqa-diamond`, `humanevalfix`, `terminal-bench-sample`)
are never rewritten. `--audit` walks every `library/benchmarks/*/MANIFEST.md`
and prints pass or the exact drift reason.

## Disk retention (`evallab gc`)

Unpromoted Harbor jobs under `runs/` are eligible only after they are
completed, ingested, and projected. The policy is:

- compress after 14 days
- prune after 60 days
- each action writes `runs/.tombstones/<job-id>.json` (job id, spec id,
  digests, reward summary, why removed)
- `--apply` also appends `gc_compressed` / `gc_pruned` lines to
  `queue/events.jsonl` and retargets the catalog to the tombstone: it writes
  `derived/gc-catalog.json` (reloaded on the next `evallab gc`) and best-effort
  `UPDATE`s `jobs.evidence_path` / `trials.evidence_path` when Postgres is up
  so no row points at a deleted directory

Never eligible: `research/evidence/`, anything named in a digest or
`digests/DISCOVERIES.md`, jobs that are not ingested+projected, and the
tombstones themselves.

Default is a dry-run plan:

```bash
uv run evallab gc
uv run evallab gc --apply    # human-triggered only
```

Nightly calls the same planner in plan-only mode and appends a
**GC would reclaim** section to the digest. Doctor prints one `disk …`
trend line with candidate counts. Apply is never invoked by nightly.

## Ingest and query

```bash
uv run evallab ingest runs research/evidence/runs
uv run evallab trajectories runs research/evidence/runs
uv run evallab db list --limit 50
```

Every completed job uses one idempotent ingest-and-project path. Queue completion,
the nightly backfill, `ingest`, and the manual `trajectories` rebuild all update
PostgreSQL first, then write a `jobs.parquet` marker per job and the eight
deterministic trial tables below the configured shared Parquet root at
`job_id=*/trial_id=*/`. The
job marker keeps zero-trial completed jobs inside the same invariant. Rebuilds
replace catalog inventories and Parquet partitions by stable Harbor UUID while
leaving raw evidence untouched.

PostgreSQL is shared across linked Git worktrees, so Parquet must be shared too.
By default, every worktree resolves `derived/parquet` against the repository's
primary checkout, not against the invoking worktree. `.env.example` records the
equivalent explicit setting:

```dotenv
EVALLAB_DERIVED_ROOT=derived/parquet
```

A relative value is resolved against the primary checkout; an absolute value
may instead select a shared volume. The `ingest --derived-dir` and
`trajectories --output-dir` flags are deliberate one-command overrides and
remain relative to the invoking checkout. This setting is storage topology,
not authentication: model access remains subscription-only through Keychain or
the agent's auth file, and API-key variables do not belong in this lab's `.env`.

To migrate an older worktree-local store, stop dispatch, copy each complete
`job_id=<uuid>` directory into the configured shared root without overwriting an
existing UUID, and run `uv run evallab doctor`. If the catalog contains a job
whose raw evidence was intentionally discarded, remove only that exact derived
catalog row; never drop or recreate the shared database to repair one stale job.
Once doctor reports equal catalog and projected counts, reinstall the schedule
and resume dispatch.

A Parquet failure cannot roll back catalog ingest or turn a completed agent run
into an execution failure. It appends a
`projection_failed:<job-id>:<error-type>` queue event and leaves the job done so
the cause is visibly attributed to the harness. `uv run evallab doctor` enforces
the operational invariant: every catalog job has complete trial partitions, or
its exact job ID has such a recorded exception. To prove rebuildability, point
`EVALLAB_DERIVED_ROOT` at an empty isolated directory and run
`evallab trajectories` over all raw roots; do not clear the live shared root.
Identical table row counts are expected.

## Evidence promotion checklist

Only small representative runs belong in `research/evidence/runs/`:

1. Confirm the run has final job and trial `result.json` files.
2. Inspect agent/verifier logs and all declared artifacts.
3. Check that no token, `.env`, prompt secret, credential file, or unrelated
   user data is present.
4. Confirm each file is small enough for ordinary Git review.
5. Copy the complete small job directory and run `uv run evallab summarize`.
6. Run the repository secret/size audit in `uv run pytest` before commit.

For large evidence, leave the job in ignored `runs/`, ingest its metadata, and
write a small report with digests and a durable artifact URI once object storage
is configured.
