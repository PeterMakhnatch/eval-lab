# Scaling gates

## Principle

Scale a measured bottleneck while preserving experiment semantics. The task,
agent, verifier, raw evidence layout, and analysis contracts should not change
merely because execution or storage moves to another machine.

The numbers below are planning heuristics, not universal limits. Capture actual
run volume, queue time, query latency, storage growth, and operator time before a
migration.

## Current stage: one researcher, one workstation

Use:

- Harbor with Docker for execution;
- explicit JSON experiment matrices;
- local immutable `runs/` directories;
- PostgreSQL in Compose as a rebuildable catalog;
- Parquet plus DuckDB for derived ATIF analytics once implemented;
- Git for definitions, code, rubrics, reports, and small reviewed evidence.

This stage is sufficient while runs fit comfortably on one machine, queueing is
occasional, and one person performs analysis.

## Gate 1: object storage

Add S3-compatible object storage before adding distributed workers when any is
true:

- evidence must survive loss of the workstation;
- a second machine or analyst must read the same run corpus;
- local backup and retention are becoming manual or unreliable;
- the corpus is too large for ordinary workstation storage;
- cloud workers need a shared durable destination.

Required migration properties:

- content-addressed objects or a recorded SHA-256 for every file;
- immutable writes for completed run versions;
- original Harbor-relative paths preserved as metadata;
- lifecycle/retention policy separated from Git retention;
- upload completion recorded only after integrity verification;
- credentials scoped to the smallest required prefix and operation.

Local MinIO-compatible development is useful only when testing the object-store
contract. Do not operate it merely to make the local architecture look like
production.

## Gate 2: remote or Kubernetes execution

Use a Harbor remote environment first when a few tasks require resources absent
locally. Use Kubernetes when the scheduling problem itself is persistent:

- median queue wait is material relative to trial duration;
- sustained runs saturate local CPU, memory, storage I/O, or Docker capacity;
- several worker machines or accelerator pools are required;
- multiple researchers require quotas or isolation;
- unattended execution needs restart/reconciliation after host failure.

Before enabling a Kubernetes environment, run contract tests for:

- task image reproducibility and digest capture;
- hidden verifier and oracle isolation;
- artifact completeness and object-store upload;
- network policy behavior;
- secret scoping and redaction;
- timeout, cancellation, retry, and duplicate-run semantics;
- CPU, memory, disk, GPU/TPU, and concurrency limits;
- Harbor result parity with the same task under local Docker where supported.

Prefer Harbor's existing provider interface. Do not create a custom operator
until a provider plus ordinary Kubernetes Jobs demonstrably cannot express the
required reconciliation behavior.

## Gate 3: workflow queue

Add a durable queue/controller when experiments must run unattended or across
workers. A queue entry should contain an immutable experiment-spec digest and an
approval record, not an arbitrary shell command.

The minimum state machine is:

```text
draft -> validated -> awaiting_approval -> queued -> running
      -> completed | failed | cancelled
```

Requirements:

- idempotency key based on spec/task/config digests;
- leases or heartbeats so abandoned work can be recovered;
- explicit retry policy that distinguishes infrastructure retry from a new
  stochastic attempt;
- cancellation and cost/attempt limits;
- immutable link to the produced Harbor job directory;
- no automatic approval of billable, cloud, large-sweep, or publishing actions.

A PostgreSQL-backed queue can be adequate at modest scale. Temporal, a message
broker, or a Kubernetes-native controller is justified only when workflow
history, fan-out, or failure handling exceeds that simple contract.

## Gate 4: ClickHouse

Keep using PostgreSQL for relational catalog queries and Parquet/DuckDB for local
trajectory analytics until observed workloads require a serving database.

Consider ClickHouse when several of these are true:

- normalized trajectory/tool/log events reach tens of millions of rows or
  roughly hundreds of gigabytes;
- analysts need repeated sub-second slicing across high-cardinality attributes;
- multiple concurrent users or dashboards query the event corpus;
- ingestion is continuous enough that batch Parquet refreshes are operationally
  painful;
- representative DuckDB queries miss a documented latency objective after
  sensible partitioning and projection.

Do not migrate the transactional catalog to ClickHouse. Replicate or load the
derived event tables into it. Raw evidence remains in the file/object layer, and
PostgreSQL remains the catalog for experiments, trials, approvals, and lineage.

## Gate 5: shared service or UI

Build a web service only when the CLI and SQL/notebook workflow creates a real
collaboration bottleneck. A first UI should expose:

- experiment and cohort selection;
- run health and control status;
- trial trajectory and artifact links;
- deterministic comparisons;
- structured analyses with evidence citations;
- approval/rejection of proposed experiments.

It should not become a second store of task definitions or evidence. Reads and
writes go through the same provenance and approval contracts as the CLI.

## Metrics to collect before scaling

Record monthly or per-project:

- trials and ATIF steps generated;
- raw and derived bytes retained;
- local queue wait and worker utilization;
- ingestion throughput and failures;
- representative query p50/p95 latency;
- concurrent analysts;
- time spent manually recovering runs or moving evidence;
- cloud/model cost and percentage wasted on invalid trials;
- analysis-agent cost and human acceptance/revision/rejection rates.

The last two metrics matter most: infrastructure that executes more invalid or
unreviewed experiments faster is negative scale.
