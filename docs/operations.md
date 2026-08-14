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
depending on interactive shell startup files.

`tick` and `nightly` both run the headless doctor first. If any check fails,
they append a boolean-only quarantine event and dispatch nothing. In particular,
a locked or unreadable Keychain produces zero reward-bearing trials rather than
misclassifying authentication failures as agent failures.

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
  `queue/events.jsonl` and retargets the catalog path at the tombstone so
  no row points at a deleted directory

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
uv run evallab db list --limit 50
```

The ingester is idempotent. It updates jobs and trials by Harbor UUID, replaces
their reward/artifact/file inventories, and leaves raw files untouched.

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
