# Architecture and scaling decisions

## Decision

Start local-first with Harbor + Docker Compose + PostgreSQL. Keep raw Harbor job
directories as immutable evidence and treat PostgreSQL as a rebuildable index.
Do not start with Kubernetes.

This is intentionally less elaborate than a production evaluation service. It
has the important boundaries now and leaves clean seams for distributed
scheduling and object storage later.

## Components and ownership

| Component | Owns | Does not own |
|---|---|---|
| Harbor | Trial isolation, agents, environments, artifact transfer, verification, reward and job files | Cross-job catalog and exploratory analysis |
| Filesystem | Complete raw job bundles and large byproducts | Fast cross-run queries |
| PostgreSQL 18 | Searchable experiment, trial, reward, artifact and file metadata; raw JSON snapshots | Canonical copies of logs and binary artifacts |
| `harbor-lab` | Safe run wrapper, provenance sidecar, parsing, ingestion and summaries | Agent implementation or scoring policy |
| Git/GitHub | Task definitions, experiment specs, code, schema, docs and small reviewed evidence controls | Arbitrary trajectories, secrets, large sweeps or database state |

PostgreSQL 18.4 is pinned in Compose. It is the current stable major/release at
the time this lab was created; the pin prevents a workstation pull from changing
the database underneath an experiment.

## Data flow

1. An experiment spec names a task, adapter, optional model, repetitions,
   concurrency, and destination.
2. `harbor-lab` records a safe command and host/tool provenance, then delegates
   execution to the installed `harbor` CLI.
3. Harbor writes the authoritative job and trial directories.
4. `harbor-lab summarize` reads those directories directly, so analysis still
   works with PostgreSQL stopped.
5. `harbor-lab ingest` upserts job/trial facts and a SHA-256 inventory into
   PostgreSQL. Re-ingestion is safe.
6. SQL clients and notebooks query the database and follow relative file paths
   back to raw evidence when detailed inspection is needed.

## Why logs and artifacts are not database blobs

JSONB is useful for Harbor's small config, lock, and result documents. Agent
logs, trajectories, archives, workbooks, images, and other byproducts grow much
faster and are naturally file/object data. Keeping their relative path, size,
kind, and digest in PostgreSQL makes them discoverable without making backups,
vacuum, or ordinary queries carry large binary values.

## Why Kubernetes is deferred

Kubernetes helps when the bottleneck is distributed scheduling: multiple worker
machines, heterogeneous GPU pools, quotas, preemption, network policy, and
failure recovery. It does not improve a single-machine authoring loop, and it
would duplicate environment orchestration that Harbor already provides.

Add Kubernetes only after at least one of these is true:

- the local Docker host is saturated by sustained queues;
- runs require multiple worker machines or GPU types;
- multiple users need isolated quotas and shared scheduling;
- unattended jobs require controller-level retry and reconciliation;
- a supported Harbor Kubernetes backend has been validated against the lab's
  artifact and secret contracts.

At that point, keep the same task directories and database schema. Replace the
local runner with a queue/controller, put artifacts in an S3-compatible store,
and add `artifact_uri` values; do not make pods write binary output to Postgres.

## Object storage threshold

The filesystem is appropriate while one machine owns the runs and backup volume
is manageable. Add object storage when evidence must survive host loss, be read
by multiple workers, or exceed ordinary Git/filesystem workflows. Prefer a
standard S3 API so local MinIO-compatible development and managed or
self-hosted production stores can share paths. Store content-addressed objects
and preserve the original Harbor-relative path in PostgreSQL.

## Reproducibility contract

Every experiment should preserve:

- explicit job name and run spec;
- Harbor version and job/trial locks;
- task digest and source revision;
- agent adapter, version, model and arguments;
- environment type, resource/time limits and concurrency;
- raw rewards, timing, token use, cost and exception data;
- artifact/log paths, byte sizes and SHA-256 digests;
- a short hypothesis and the variable changed.

The included control matrix changes only the agent adapter (`oracle` versus
`nop`). All task, environment and verifier inputs stay constant.

## Security and cost boundaries

- Local controls need no model secrets.
- The included task has a `public` network baseline because local Docker Desktop
  on macOS cannot enforce Harbor's `no-network` mode. This is a documented local
  provider limitation, not a claim of network isolation.
- Credentials come from the environment and are never copied into metadata.
- The wrapper does not record environment values or full process environments.
- Non-control agents require `--allow-billable`.
- Evidence intended for Git must be reviewed for secrets and size first.
- Private GitHub visibility is required, but private Git is not a secret store.
