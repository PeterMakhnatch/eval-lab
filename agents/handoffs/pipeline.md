Status: building
Last: Backfilled 14 existing jobs, auto-ingested queued Oracle as job 15, and proved destructive rebuild parity
Next: Commit, run make premerge, push PR, require green GitHub checks, then merge
Blockers: none

## Scope

- Branch/worktree: `role/pipeline` in `.worktrees/pipeline`
- Owned code: `atif.py`; ingest call sites in `queue.py`, `automation.py`, and
  additive CLI changes; tests; this handoff; one operations section.

## Implementation

- `evallab.atif.ingest_and_project` commits base and deterministic catalog data
  before projecting each job. Per-job Parquet exceptions are returned without
  undoing catalog ingest.
- Queue, direct executor, nightly backfill, `ingest`, and `trajectories` all use
  that function. Queue events use
  `projection_failed:<job-id>:<error-type>`; catalog failures are distinct.
- `jobs.parquet` supplies a job-level row for the two completed records with no
  completed trials. Trial-bearing jobs additionally require all eight trial
  tables.
- `evallab doctor` verifies catalog jobs equal complete projected jobs, allowing
  only exact job IDs with recorded projection exceptions.

## Verification log

### Backfill

The supplied acceptance text expected 13 existing jobs. Fresh discovery found
and ingested 14 completed Harbor job records, including two zero-trial jobs:

```text
$ .venv/bin/evallab ingest <main-runs> <main-research-evidence>
ingested 14 job(s)
jobs: 14 row(s)
trajectories: 6 row(s)
trial_facts: 22 row(s)
```

```text
$ .venv/bin/evallab doctor
ok    catalog-parquet catalog=14 projected=14 exceptions=0 missing=0 extra=0
```

### Automatic queue path

```text
$ .venv/bin/evallab submit queue/pipeline-oracle-acceptance.json
approved: .../queue/approved/oracle-01M00RQC0H645KXQCRXQETZRKS.json
admitted by standing policy rule local-controls

$ .venv/bin/evallab tick
Reward 1.000; Exceptions 0
dispatched 1 experiment(s)
quarantined: no
```

No manual ingest/rebuild ran between `tick` and these checks:

```text
catalog id: f0024b3d-c25e-4e08-bf21-0b501dde0f6a
job: pipeline-oracle-acceptance-20260814
trials: 1
reward: 1
Parquet: jobs.parquet plus all eight trial_id=18968080-... tables
queue terminal event: dispatch_completed (running -> done)
```

```text
$ .venv/bin/evallab doctor
ok    catalog-parquet catalog=15 projected=15 exceptions=0 missing=0 extra=0
```

### Destructive rebuild

Before deletion and after `rm -rf <worktree>/derived/parquet` followed by
`evallab trajectories` over the same raw roots, aggregate counts were identical:

```text
artifact_facts: 49
jobs: 15
observations: 0
reward_facts: 35
steps: 30
tool_calls: 0
tool_usage: 0
trajectories: 6
trial_facts: 23
```

Post-rebuild doctor: `catalog=15 projected=15 exceptions=0 missing=0 extra=0`.

### Repository checks (pre-commit)

```text
$ .venv/bin/pytest
60 passed in 1.00s
$ .venv/bin/ruff check .
All checks passed!
$ uvx ty@0.0.71 check src/ --output-format=concise
Found 33 diagnostics
```

The 33-diagnostic type output is the repository's documented passing ratchet.
`make premerge` and GitHub check evidence will be appended after the commit.
