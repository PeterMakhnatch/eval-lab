# Architecture and scaling decisions

## What this lab is

The lab is an evaluation research workbench around Harbor. It is not a second
agent harness and it is not initially a hosted evaluation platform.

Its job is to make this loop reproducible:

1. Author or select a task and state a capability hypothesis.
2. Run controlled Harbor trials.
3. Preserve the complete evidence for every trial.
4. Extract deterministic facts from results and ATIF trajectories.
5. Ask analysis agents bounded, auditable questions about that evidence.
6. Compare cohorts, classify failures, and draft the next experiment.
7. Require a human approval before any billable or externally consequential run.

Harbor remains the execution engine. This repository owns experiment intent,
evidence retention, indexing, analysis provenance, and the feedback loop.

## Decision summary

Build the smallest version that preserves the eventual boundaries:

- **Now:** Harbor + local Docker, immutable Harbor job directories, PostgreSQL
  metadata index, checked-in experiment specs, and CLI workflows.
- **Next:** ingest ATIF structure, write derived columnar data as Parquet, query it
  locally with DuckDB, and add structured analysis artifacts and approval gates.
- **Later:** move raw evidence to S3-compatible object storage when it must outlive
  or be shared across machines.
- **Only at measured scale:** use a Harbor Kubernetes environment for distributed
  execution and ClickHouse for concurrent, low-latency analytics over very large
  event tables.

Do not start with Kubernetes, ClickHouse, Kafka, Airflow, or a custom agent
orchestrator. None solves the current research bottleneck, which is producing
trustworthy experiments and analyses.

## Logical architecture

```text
                     versioned definitions
          tasks + experiment specs + rubrics + policies
                              |
                              v
                    evallab control plane
             validate -> approve -> invoke -> record
                              |
                              v
                 Harbor execution and verification
          Docker now; cloud sandbox / GKE only when needed
                              |
                              v
              immutable Harbor job/trial directories
       configs, locks, ATIF, logs, artifacts, verifier, reward
                 |                         |
                 |                         +--> reviewed evidence bundles
                 v
           deterministic ingestion
          /          |             \
         v           v              v
 PostgreSQL       Parquet        file/object store
 catalog/index  trajectory facts  canonical raw evidence
         \           |              /
          \          v             /
           +--> comparison + analysis pipeline
                  |             |
                  v             v
          structured findings  experiment proposal
                  |             |
                  +------> human approval ------> next run
```

## The five planes

### 1. Definition plane

Git owns task definitions, experiment matrices, analysis rubrics, policies, and
small reports. An experiment spec should name the hypothesis, fixed conditions,
independent variable, task versions, adapters/models, attempts, concurrency,
timeouts, and expected controls.

This plane answers, “What did we intend to test?” It must be reviewable before a
run begins.

### 2. Execution plane

Harbor owns environment creation, agent invocation, verification, rewards,
artifacts, and native trial/job results. `evallab` should delegate those
responsibilities rather than copying Harbor internals.

The local Docker provider is the authoring default. Harbor already supports
remote providers and Kubernetes-backed environments, including GKE. The lab's
runner boundary should therefore accept a Harbor environment selection without
encoding Docker-specific assumptions into experiment semantics.

### 3. Evidence plane

The complete Harbor job directory is the canonical run evidence. Once a job is
complete, analysis code must not edit it. Any enrichment is written beside it as
a new, provenance-bearing artifact or into a separate derived-data directory.

Local filesystem storage is appropriate while one workstation owns the run
corpus. Object storage becomes the canonical evidence layer when workers or
analysts span machines. In either case, content digests and original
Harbor-relative paths must be preserved.

### 4. Catalog and analytics plane

PostgreSQL is a rebuildable catalog for jobs, trials, rewards, artifacts,
versions, analyses, and relationships. It is well suited to relational filtering
and relational questions such as “show Codex trials for task version X with an
environment exception.” Small original JSON documents may be retained in JSONB,
but large logs and trajectories do not belong in database blob columns.

ATIF trajectories are nested event data. The next storage step should be a
deterministic ATIF-to-Parquet projection with tables for trajectories, steps,
tool calls, observations, and token/cost metrics. DuckDB can query Parquet
directly with projection and filter pushdown, so it supplies a local analytical
engine without adding a service.

ClickHouse is a later serving layer, not an immediate dependency. It becomes
useful when the lab needs sustained event ingestion or interactive,
high-concurrency queries across a corpus too large for the local
Parquet/DuckDB workflow. The Parquet projection is still valuable then: it is a
portable backfill and interchange format rather than a dead-end prototype.

### 5. Analysis and decision plane

Analysis has two deliberately separate stages:

1. **Deterministic extraction:** rewards, exceptions, durations, token counts,
   tool usage, command failures, changed artifacts, verifier checks, and ATIF
   structure.
2. **Model-assisted interpretation:** failure classification, reward-hacking
   suspicion, specification critique, cross-trial patterns, and proposed
   experiments.

Every model-assisted finding must record its source trials, evidence references,
rubric and prompt digest, analysis agent/model/version, timestamp, and structured
output. It is a hypothesis, not ground truth. A second model may critique it,
but agreement between models is not a substitute for evidence or human review.

See [analysis-loop.md](analysis-loop.md) for the concrete state machine and
artifact contracts.

## Data lifecycle and immutability

```text
draft spec -> validated spec -> approved run -> immutable raw job
                                             -> deterministic derived facts
                                             -> model-assisted analysis
                                             -> reviewed finding
                                             -> proposed experiment
                                             -> explicit approval
```

These are separate records. Never overwrite an earlier analysis after changing
a rubric or model; produce a new analysis invocation linked to the same source
trial. Never edit an agent trajectory to make parsing or a later analysis pass.

The database can be dropped and reconstructed from raw evidence and structured
sidecars. If a fact exists only in PostgreSQL, the design has violated the
rebuildability contract.

## Target data model

The current schema covers jobs, trials, rewards, artifacts, and file inventories.
Extend it incrementally as real workflows arrive:

| Entity | Purpose | Durable source |
|---|---|---|
| `experiments` | Hypothesis and controlled-variable definition | checked-in spec |
| `jobs` / `trials` | Harbor execution results | raw Harbor job |
| `rewards` / `artifacts` | Verifier dimensions and output inventory | raw Harbor job |
| `trajectories` | ATIF document identity and validation status | raw ATIF file |
| `trajectory_steps` | Queryable step-level facts or Parquet location | derived projection |
| `analysis_invocations` | Prompt/rubric/model provenance | analysis sidecar |
| `analysis_findings` | Structured claims with evidence references | analysis sidecar |
| `experiment_proposals` | Follow-up hypothesis and one-variable change | proposal document |
| `reviews` | Human disposition and rationale | review document |

Do not add all tables speculatively. Add each idempotent schema change with the
parser and fixture that prove how the source record is reconstructed.

## The agent-assisted research loop

Agents may automatically inspect evidence and draft follow-up experiments. They
must not silently create an uncontrolled self-modifying benchmark loop.

Required gates:

- deterministic ingestion must succeed before model analysis;
- the source task, verifier, and trial evidence are read-only;
- each claim cites file paths and, when available, ATIF step/tool-call IDs;
- analyses use a versioned output schema and bounded rubric;
- proposals identify exactly one primary experimental variable;
- duplicate proposals are detected by task/config/prompt digests;
- Oracle and no-op controls may run locally under policy;
- real-model, cloud, large-sweep, deployment, and publication actions require
  explicit human approval;
- a new run never edits the task or verifier used by the source run.

This design allows useful automation without allowing an analysis agent to
spend money, move the goalposts, or manufacture confirmatory evidence.

## Why Kubernetes is deferred

Kubernetes is an execution substrate. It helps with worker placement, resource
quotas, accelerator pools, retries after node loss, and multi-user isolation. It
does not improve the single-machine task-authoring loop or the validity of an
eval.

Add Kubernetes only when measurements show at least one of these conditions:

- sustained queues saturate the local Docker host;
- tasks require multiple worker machines or heterogeneous accelerators;
- several users need namespaces, quotas, and shared scheduling;
- unattended jobs need controller-level reconciliation after worker failure;
- the selected Harbor Kubernetes environment has passed the lab's artifact,
  network, secret, and verifier-isolation contract tests.

At that point, use Harbor's provider boundary. Do not initially write a custom
Kubernetes operator. A Kubernetes Job is appropriate for a run-to-completion
worker, while Harbor still owns the trial semantics inside it.

## Storage decision table

| Need | Use now | Add later when measured | Avoid |
|---|---|---|---|
| canonical run evidence | local immutable job directory | S3-compatible object storage | PostgreSQL blobs |
| experiment/trial catalog | PostgreSQL | managed PostgreSQL if shared | ClickHouse as transactional catalog |
| local trajectory analytics | Parquet + DuckDB | partitioned object-store Parquet | loading every log line into Postgres |
| high-volume concurrent analytics | not needed | ClickHouse | operating it for a few thousand trials |
| task execution | Harbor + Docker | Harbor remote/GKE environment | custom scheduler before a queue exists |
| workflow automation | explicit CLI + specs | small queue/controller after unattended demand | self-triggering agent loop |

See [scaling.md](scaling.md) for measurable migration gates.

## Reproducibility contract

Every experiment should preserve:

- explicit job name and experiment-spec digest;
- Harbor version and job/trial locks;
- task digest and source revision;
- agent adapter, version, model, arguments, and tools;
- environment provider, image digest, resources, network policy, and timeouts;
- attempts, concurrency, seed or other stochastic controls when available;
- raw and per-dimension rewards;
- timing, token use, cost, and exception data;
- artifact/log/trajectory paths, byte sizes, and SHA-256 digests;
- analysis prompt, rubric, model, output schema, and source references;
- the hypothesis and the single intended variable change.

The included control matrix changes only the agent adapter (`oracle` versus
`nop`). All task, environment, and verifier inputs stay fixed.

## Security and cost boundaries

- Local controls require no model secrets.
- Credentials come from the process environment and are never copied into
  provenance, prompts, logs, or the database.
- Analysis agents receive a reviewed evidence bundle, not arbitrary access to
  the workstation or the full repository.
- Hidden verifier inputs and oracle solutions remain outside the evaluated
  agent's environment.
- Non-control agents require `--allow-billable`; future queued runs must preserve
  an equivalent approval record.
- Evidence promoted to Git must pass secret, size, and sensitive-content review.
- Raw prompts and trajectories may contain proprietary or personal data even
  when a repository is private; retention and redaction are explicit policies.

## Build order

1. Keep the current local controls, immutable run format, Postgres catalog, and
   fixture-tested parsers healthy.
2. Normalize and validate ATIF trajectories; export queryable Parquet facts.
3. Add deterministic cohort comparison and failure-taxonomy reports.
4. Wrap Harbor's existing analysis capability with versioned rubrics,
   provenance, and a billable-run gate.
5. Generate structured experiment proposals and require approval before running
   them.
6. Add object storage, workers, Kubernetes, or ClickHouse only when the scaling
   gates are actually crossed.

The implementation briefs in [`docs/prompts/`](../docs/prompts/) follow this order.

## References

- Harbor's [ATIF trajectory documentation](https://github.com/harbor-framework/harbor/blob/main/docs/content/docs/agents/trajectory-format.mdx)
  defines the portable trajectory source used by the analytics layer.
- Harbor's [analysis implementation](https://github.com/harbor-framework/harbor/blob/main/src/harbor/analyze/analyzer.py)
  already runs bounded analysis tasks; the lab should wrap and version it.
- Harbor includes a [GKE environment](https://github.com/harbor-framework/harbor/blob/main/src/harbor/environments/gke.py),
  so Kubernetes scaling can stay behind Harbor's environment interface.
- DuckDB can [query Parquet directly](https://duckdb.org/docs/current/guides/file_formats/query_parquet)
  with filter and projection pushdown.
- Kubernetes [Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
  are run-to-completion workloads; they are an execution mechanism, not an eval
  data model.
- ClickHouse is designed for [large-scale real-time analytics and observability](https://clickhouse.com/use-cases),
  which is why it is a scale-triggered option rather than a starting dependency.
