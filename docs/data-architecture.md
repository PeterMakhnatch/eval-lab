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
`provenance.json` sidecar do.

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
runs/ and research/evidence/runs/    Zone 02 immutable raw jobs
derived/parquet/external/            Zone 01 query projection
derived/parquet/external/task_catalog/  Zone 01 pinned task catalog (task_sources,
                                      task_versions, task_findings, task_lineage,
                                      task_stability, task_exploits,
                                      task_qualification;
                                      see docs/task-quality.md)
derived/task-store/hf/                Zone 01 pinned dataset snapshots, read-only
                                      (<org>__<repo>@<rev12>/ + provenance.json)
derived/parquet/job_id=*/            Zone 02 query projection
library/task-variants/                Zone 03 lineage records for derived task variants
derived/task-store/variants/          Zone 03 materialized task variants (rebuildable from lineage records)
derived/captures/<name>/             Zone 02 independent model-call record (calls.jsonl + manifest + digest)
```

`derived/` is rebuildable and ignored by Git. Raw snapshots and raw jobs are
never edited to make a parser succeed. A parser fix creates a new projection;
a transform creates a new item with a new digest and a parent link.

External and local Parquet roots stay physically separate. Cross-zone analysis
must name both roots and select a `zone`, preventing a broad glob from silently
mixing published external runs with locally reproduced evidence.

For daily partition consolidation under `derived/parquet/compact/`, see [compaction](#parquet-compaction).
For task origin taxonomy and classification rules, see [task quality](task-quality.md#task-provenance).

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
5. Never place an agent-authored task directly in `library/registry/`.

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

For unified catalog, Parquet, and front-matter queries, see [the attach contract](#unified-attach-surface).

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

Evidence-pipeline reliability owns **engineering data quality**: complete
capture, consistent projections, traceable provenance, usable consumer
interfaces, and safe replay from retained raw evidence. It does not decide
whether a task or verifier is scientifically valid, or whether a reward
counts as a pass. For the paired treatment/capture publication and recovery
contract, see [execution tiers](execution-tiers.md#trial-treatment-keys-and-capture-records).

For entity join invariants and canonical `v_spine`, see [catalog entities and joins](#catalog-entities-and-joins).

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

## Unified attach surface

`evallab.storage.attach.attach` owns cross-zone query attachment.
These storage zones differ from the 4 provenance categories above.

| Storage zone | Source | Contract |
|---|---|---|
| z2 | PostgreSQL catalog through `postgres_scanner` | Expose canonical entities and the examined database identity. |
| z3 | Parquet projections | Read hot and compact layouts with `union_by_name=true`. |
| z4 | Markdown metadata | `path`, `title`, `status`, `audience`, `generated_by` from all `docs/**/*.md`. |


```python
from evallab.storage.attach import attach

result = attach()
try:
    rows = result.connection.execute("SELECT * FROM z3.trial_facts LIMIT 5").fetchall()
finally:
    result.connection.close()
```

An unavailable zone reports its reason and examined source.
It does not become a successful empty query.
An unavailable PostgreSQL connection does not prevent local z3/z4 reads.
Exported SQL and an open connection retain the same published treatment/capture generation.

### Markdown metadata parser

`storage.attach` owns `DocMetadata`, `parse_front_matter`, `extract_title`, and `parse_doc`.
The builder context compiler is retired; metadata parsing is not.
The parser reads UTF-8 and hashes the complete original content as `sha256:<64-hex>`.
It retains both raw content and the stripped body.
It uses the first H1 as the title, with a filename-derived fallback.

Only a leading YAML block bounded by `---` is metadata.
Malformed or non-mapping YAML falls back to the stripped complete document.
Recognized statuses are `living` and `historical`; other statuses remain `unclassified`.
A string audience becomes 1 normalized value. Lists and tuples become normalized tuples.
Parsing does not filter documents by audience or compile a context bundle.
The z4 reader keeps its recursive Markdown glob, including historical artifacts.
Lineage uses the same front-matter parser.

## Local trial census and trace queries

The trial census has no CLI frontend. Python readers use `evallab.storage.trials.connect_trials`.

The census is a transient DuckDB view, not another stored database.
Discovery includes primary/worktree run roots, reviewed evidence, and an explicit `EVALLAB_RUNS_ROOT`.
Staging directories and physical copies do not multiply native `(job_id, trial_id)` identities.
Unfinished and infrastructure trials remain visible. Missing native IDs remain null.
The read-only census writes no projections, reports, queue events, or provider calls.
Missing projections can use existing in-memory producers. Failures remain in `projection_error`.

The `contamination` relation retains separate audit labels and never changes a counts verdict.
`legit` requires the exact native model/harness, applied lock, reference-profile match,
no canonical infrastructure exclusion, and no HAR-156 `our_limit` stop.
Reward, integrity, and copy verdict do not enter that predicate.
An approved deviation is not an exact reference-profile match.
Unknown evidence never establishes legitimacy.

Trace mode reads actual ATIF steps and `sql/trace_queries.sql` views without a catalog or network.
Missing or invalid counts remain unknown. Unscored attempts do not become failures.
Coverage separates file existence, observed steps, processed counts, and frozen labels.
First-edit metrics require hash-matched stored features or an explicit read-only reconstruction.
Stale/conflicting values remain unknown.
Post-edit shares use complete ordinal-matched native metrics independently for input and output.
Recorded actions, shape classification, prediction opinions, and countability remain different evidence.
Frozen-loop accuracy requires valid agreeing raters and an unambiguous cohort.
See `sql/README.md` for the detailed query contract.

### Trial reader edge contracts

`traj.outline_trajectory` reads and extracts `result.json` metadata before hashing the result citation.
It checks file existence again before hashing. A removed result has no citation.
Invalid JSON uses empty metadata; a hash read error still propagates.
These separate reads do not provide an atomic snapshot if the source changes.

`probe03` preserves each CTRF caller's original malformed-field contract.
The passage and test-record readers validate list shape and can use stdout recovery.
The grader, progress, and source-assertion readers use unvalidated fields.
Non-object roots and truthy non-object results raise `AttributeError`; truthy integer test fields raise `TypeError`.
Sized non-list test fields retain their length-based counts without stdout fallback.
Outcome rejection attribution evaluates normalizer counts before selecting a rule.
Mixed malformed step IDs can therefore raise `TypeError`, including for unrelated rejection causes.


## Reward-report contradictions in processed trials

`process-job` writes 1 `reward_report` record per trial.
The value is `null` when the trial contradicts nothing.
Otherwise, the object contains `schema` (`evallab.reward_report/v1`), `classification`, `reason`,
`mismatch`, `reward`, `reward_source`, `reward_txt`, and `counts`.
`counts` contains `tests`, `passed`, `failed`, and `skipped`.

The rules mirror the HAR-437 scan in
`research/experiments/reward-report-contradictions/build.py`.
The module `src/evallab/reward_report.py` never imports research code.
`forged` means `passed == 0`, `failed > 0`, and `reward > 0`.
`partial-credit` means `reward >= 1`, `passed > 0`, and `failed > 0`.
`unknown` covers the remaining contradiction shapes.
A mismatch means the effective reward differs from `verifier/reward.txt` by more than `1e-9`.
A missing report or a report with no test list is not comparable and raises no classification flag.
A reward-file mismatch still produces a record with `classification: null` and `reason: ""`.
A trial with no usable reward produces no record at all.
The classification flag is `reward_report:<classification>:<passed>/<tests>-passed`.
For example, the flag is `reward_report:forged:0/19-passed`.
The mismatch flag is `reward_report:mismatch:<reward>-vs-<reward_txt>`.
For example, the flag is `reward_report:mismatch:0-vs-1`.
The record and flags are evidence only.
This check never rewrites `reward`, `scored`, `reward_source`, `counts`, or `decision`.

## Catalog entities and joins

`sql/schema.sql` owns catalog DDL. Do not duplicate its schema in another writer.
Suites key on `(name, version)` and members bind exact `(task_ref, task_version)` pairs.
A draft suite permits membership changes. A frozen suite permits none.
PostgreSQL triggers protect both suite membership and frozen suite identity.

`v_quota_today` groups completed trial runs and tokens by provider and UTC date.
It is not subscription headroom. Provider allowance comes from quota observations.
An observed empty catalog is different from an unreachable database.
Neither a failed read nor a missing table establishes zero consumption.

The join spine is `spec_id → job_id → trial_id → {trajectory, analysis, observation}`.
Task/version binds CRAFT and suite membership. Agent identity binds its profile.
`v_spine` keeps trials without analysis through LEFT JOINs.
Do not repair a broken join by silently removing its row from the denominator.

## Lineage


Each generated artifact declares direct `inputs` as `path` or `id` plus a `sha256:` digest.
Markdown uses YAML front-matter. JSON and Parquet use their metadata or companion sidecar.
The walker stops at immutable z1 evidence.
Every other zone must name its inputs.
It never infers an edge from a filename, timestamp, or nearby directory.

Missing inputs report `unrecorded`. A changed source reports `digest_mismatch`.
An ancestor reference reports `cycle`. The default traversal depth is 32.
`evallab.lineage.resolve_lineage` checks provenance in Python without mutating source files.

For up to 100 resolved partition files, record each sorted path and content digest.
For larger sets, use the canonical glob with the composite digest of sorted member digests.
Do not record an unexpanded glob as if it were a regular file.
`research/lessons.md` declares its SQL, task/CRAFT, observation, analysis, and trial-fact inputs.

## Parquet compaction

`storage.parquet_compaction` owns `PRIMARY_KEYS` for compact layouts.
The source uses job-level `jobs.parquet` and trial-level fact/ATIF tables.
Daily compaction produces the same tables under `compact/dt=YYYY-MM-DD/`.
Readers accept supported historical compact layouts through the storage path helpers.

Use the [retention policy](GENERATED-CACHE-POLICY.md) before any cleanup.

