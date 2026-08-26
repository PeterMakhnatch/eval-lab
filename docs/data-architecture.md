---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
---

# Data architecture: four provenance zones

Status: normative design. Owner: DATA-STRATEGY. Date: 2026-08-15.

This architecture keeps evidence, imported corpora, generated tasks, and
training-ready distillations distinguishable even when they share a Parquet
schema. A row's shape never establishes its evidential status; its zone and
`ProvenanceMetadata` sidecar do.

## The four zones

| Zone | Purpose | Canonical examples | Authority |
|---|---|---|---|
| `01-external` | Public material acquired from a third party | Pinned Hugging Face ATIF corpus, Harbor benchmark export | Useful comparative data; never evidence that this lab reproduced a result |
| `02-local-evidence` | Immutable outputs from this lab's queue | `runs/<job>/`, ATIF, verifier output, catalog rows | Primary evidence for lab claims |
| `03-synthetic` | Machine-produced tasks or data derived from declared inputs | Generated Harbor tasks, perturbations, normalized external formats | Candidate material until independently certified |
| `04-curated` | Reviewed, policy-filtered products assembled from prior zones | Distillation sets, analysis-ready cohorts, training exports | Derived product; authority is bounded by its parents and selection contract |

Zone numbers describe lineage, not a mandatory processing order. A local job
may consume a Zone 01 task and produce Zone 02 evidence. A Zone 04 cohort may
select from Zones 01 and 02, but it must retain every selected parent's digest.

## Storage boundaries

```text
library/benchmarks/_trajectories/    Zone 01 immutable snapshots
runs/ and research/evidence/runs/    Zone 02 immutable raw jobs
library/synthetic/                   Zone 03 generated task sources
derived/parquet/external/            Zone 01 query projection
derived/parquet/job_id=*/            Zone 02 query projection
derived/synthetic/                   Zone 03 generated projections
derived/curated/                     Zone 04 exports
```

`derived/` is rebuildable and ignored by Git. Raw snapshots and raw jobs are
never edited to make a parser succeed. A parser fix creates a new projection;
a transform creates a new item with a new digest and a parent link.

External and local Parquet roots stay physically separate. Cross-zone analysis
must name both roots and select a `zone`, preventing a broad glob from silently
mixing published external runs with locally reproduced evidence.

## Provenance contract

Every dataset-sized item has a JSON sidecar validated by
`evallab.schemas.ProvenanceMetadata`:

- `item_id`: stable, human-readable identifier;
- `zone`: one of the four exact zone labels above;
- `source_uri`: public URI or repository-relative local source;
- `revision`: immutable upstream pin, required for Zone 01;
- `material_digest`: SHA-256 of the acquired or produced bytes;
- `license`: upstream or output license when known;
- `created_at` and `created_by`: UTC timestamp and producer identity;
- `transform`: `name@version` for machine-produced Zones 03 and 04;
- `parent_digests`: content-addressed lineage, required for Zone 04;
- `notes`: bounded human context, never a substitute for a structured field.

The schema rejects unknown fields. That makes migrations explicit: extend the
model and its tests, or increment `schema_version`; do not hide new semantics in
an ad hoc sidecar key.

## Zone admission gates

### Zone 01: external

1. Resolve a public source to an immutable revision (commit SHA, not a branch).
2. Fetch anonymously; gated or credential-dependent datasets are unsupported.
3. Hash the material before parsing and record the declared license.
4. Preserve invalid records and report parser failures by count and reason.
5. Project only into `derived/parquet/external/`.

An external leaderboard score remains a reported upstream result. It can test
our parser or motivate a hypothesis, but cannot be presented as reproduced.

### Zone 02: local evidence

1. Enter only through the queue/executor ingest path.
2. Keep the completed raw job immutable.
3. Catalog and Parquet projection must agree, except for a recorded projection
   failure with its own reason code.
4. Preserve task, verifier, environment, scaffold, prompt, and model identity.

This is the only zone from which the lab may make an unqualified statement such
as "we ran" or "we observed".

### Zone 03: synthetic

1. Record the generator or converter as `name@version` and hash every parent.
2. Run schema validation and static policy checks.
3. For Harbor tasks, require an oracle pass and nop score of zero before use.
4. Keep generation prompts, source diffs, and rejection reasons as lineage.
5. Never place an agent-authored task directly in `registered/`.

Certification promotes confidence in an item; it does not rewrite its zone or
erase its synthetic origin.

### Zone 04: curated

1. Declare selection, exclusion, deduplication, and redaction rules.
2. Cite at least one parent digest per output item or shard.
3. Record the versioned export transform and exact schema.
4. Carry licenses and usage constraints forward; incompatible parents stay out.
5. Make the export reproducible from retained parents and configuration.

Curated means reviewed for a stated purpose, not ground truth. A training set,
benchmark cohort, and publication table may need different curation contracts.

## Allowed transitions

| From | To | Required operation |
|---|---|---|
| Zone 01 | Zone 01 Parquet | Deterministic parse/projection; raw digest retained |
| Zone 01 | Zone 03 | Versioned conversion or task synthesis |
| Zone 01 or 03 | Zone 02 | A fresh local queue run; imported results never become local evidence |
| Zones 01–03 | Zone 04 | Declared selection/transform with parent digests |
| Zone 04 | Zone 02 | A fresh local evaluation of the curated artifact |

There is no metadata-only promotion. In particular, copying an external row
under the local Parquet root does not make it Zone 02.

## Query and publication rules

- Every analysis declares the zones it reads.
- Nullable token and cost fields remain nullable; missing is not zero.
- Model, scaffold, task, verifier, and environment versions are grouping keys,
  not descriptive footnotes.
- Published aggregates link to the curated manifest and ultimately to parent
  digests.
- Raw prompts and observations may contain proprietary or personal data. Zone
  04 exporters must apply a declared redaction policy before wider release.

## Rebuild and audit invariants

For each sidecar, the material digest must match the current bytes. For each
derived item, every parent digest must resolve in the retained provenance
index. For local jobs, cataloged job IDs must equal projected job IDs plus
reasoned projection exceptions. For an external ingest, the audit reports:

```text
dataset revision digest license bytes files parsed invalid projected
```

Deleting `derived/` and rerunning pinned fetch/projection commands must recreate
the same schemas, row counts, and content digests. Rebuildability is tested from
fixtures in CI and from one public sample during mission acceptance.

## Retention

Zone 02 raw evidence and Zone 04 release manifests are durable. Zone 01 may be
refetched only while its immutable public revision remains available, so a
publication dependency should be retained locally. Zone 03 rejected candidates
may be compressed after their rejection metadata and generator lineage are
preserved. All Parquet projections are disposable caches.

`derived/analyses/` is a rebuildable Zone 03 working tree, not the immutable
artifact authority. An IR, pack, judgment, decision, or campaign report is
eligible for working-tree cleanup only after its exact bytes have been archived
to CAS, the archive restores with the recorded content digest, and the current
report or decision records that CAS URI and digest. Missing, corrupt,
unrestorable, unlinked, or ambiguous-generation sidecars remain on HOLD; cleanup
must not turn them into zero rows or select a replacement generation.

Superseded or duplicate working copies may be moved under
`derived/analyses/_quarantine/<reason>/` during reconciliation. Quarantine
preserves bytes for investigation but is excluded from current-generation
discovery and capability denominators. A quarantined working copy may be pruned
only when all of these checks pass:

1. its authoritative CAS object restores byte-for-byte and matches the recorded
   digest;
2. the canonical current report or decision identifies the retained generation;
3. no unresolved worker invocation, citation, or provenance record points only
   to the working copy.

Cleanup never deletes CAS evidence, rewrites append-only reports, or repairs
ambiguity by modification time. If any check is unavailable, retain the
quarantine and report `unknown` or a reason-coded HOLD.
