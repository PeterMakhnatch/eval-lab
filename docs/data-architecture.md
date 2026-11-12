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
derived/synthetic/                   Zone 03 generated projections
derived/curated/                     Zone 04 exports
```

`derived/` is rebuildable and ignored by Git. Raw snapshots and raw jobs are
never edited to make a parser succeed. A parser fix creates a new projection;
a transform creates a new item with a new digest and a parent link.

External and local Parquet roots stay physically separate. Cross-zone analysis
must name both roots and select a `zone`, preventing a broad glob from silently
mixing published external runs with locally reproduced evidence.

For daily partition consolidation under `derived/parquet/compact/`, see [compaction](#parquet-compaction).
For instruction and trajectory search in `derived/lance/`, see [lexical vector search](#lexical-vector-search).

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
| z1 | Immutable raw jobs and evidence CAS | Direct evidence authority; not a DuckDB replacement for raw bytes. |
| z2 | PostgreSQL catalog through `postgres_scanner` | Expose canonical entities and the examined database identity. |
| z3 | Parquet projections | Read hot and compact layouts with `union_by_name=true`. |
| z4 | Markdown metadata | `path`, `title`, `status`, `audience`, `generated_by` from all `docs/**/*.md`. |
| z5 | Coordination records | Derived records, not execution evidence. |

```sh
```

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
`--zones` fails only when no zone attaches.
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

```sh
uv run evallab trials
uv run evallab trials --runs-dir /absolute/path/to/job
uv run evallab trials --sql 'SELECT campaign, legit, count(*) FROM trials GROUP BY campaign, legit'
```

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
`uv run python -m evallab.spine check` reports each orphan edge and fails on orphans.
Do not repair a broken join by silently removing its row from the denominator.

## Lineage

```sh
```

Each generated artifact declares direct `inputs` as `path` or `id` plus a `sha256:` digest.
Markdown uses YAML front-matter. JSON and Parquet use their metadata or companion sidecar.
The walker stops at immutable z1 evidence.
Every other zone must name its inputs.
It never infers an edge from a filename, timestamp, or nearby directory.

Missing inputs report `unrecorded`. A changed source reports `digest_mismatch`.
An ancestor reference reports `cycle`. The default traversal depth is 32.
Exit 0 requires every branch to resolve to matching z1 evidence.
Missing, mismatched, cyclic, or unrecorded lineage exits 1.

For up to 100 resolved partition files, record each sorted path and content digest.
For larger sets, use the canonical glob with the composite digest of sorted member digests.
Do not record an unexpanded glob as if it were a regular file.
`research/lessons.md` declares its SQL, task/CRAFT, observation, analysis, and trial-fact inputs.

## Parquet compaction

`storage.parquet_compaction` owns planning, primary keys, and validated publication.
The source uses job-level `jobs.parquet` and trial-level fact/ATIF tables.
Daily compaction produces the same tables under `compact/dt=YYYY-MM-DD/`.
Readers accept supported historical compact layouts through the storage path helpers.

```sh
uv run python -m evallab.storage.parquet_compaction compact --dry-run
uv run python -m evallab.storage.parquet_compaction compact --target-date YYYY-MM-DD --no-prune
```

Compaction covers jobs, trajectories, steps, calls, observations, trial facts,
reward facts, artifact facts, and tool usage.
Deduplication uses each table's declared primary key and deterministic ordering.
UTC partition dates use step timestamps, raw job dates, configured catalog dates, then filesystem time.
The last source is a fallback, not observed execution time.

The writer validates exact Arrow schema and expected row count before atomic replacement.
A failed validation never prunes granular partitions.
Only closed UTC days qualify for normal compaction.
The default granular retention is 7 days; `--no-prune` keeps those projections.
Projection cleanup never removes raw jobs or CAS evidence.
Use the [retention policy](GENERATED-CACHE-POLICY.md) before any cleanup.

## Lexical vector search

LanceDB is an opt-in `lance` dependency group beside DuckDB.
Its `tasks`, `trials`, and `steps` tables live under the configured Lance root.
Task instructions come from the library. Trial metadata comes through the attach surface.
Raw step text comes from retained ATIF under explicitly resolved run roots.
Missing trajectories are reported by path, not invented.

```sh
uv run python -m evallab.lance build --table all
uv run python -m evallab.lance search 'quick brown' --table tasks --k 5
```

The default `HashingEmbedder` uses 256 L2-normalized lexical buckets.
Its distance is lexical overlap, not semantic similarity.
Trial text uses bounded first/last messages; step text retains its ATIF source.
An ANN index requires at least 1,000 rows. Smaller tables use exact search.
`--runs-root` or `EVALLAB_RUNS_ROOT` selects explicit raw evidence roots.
Repeated builds preserve identity rather than duplicate rows.

## Historical field-to-evidence matrix

The following matrix preserves the PR #187 mapping and its original scientific values.
“Current” in this retained matrix refers to that validation, not a new corpus census.

Validation basis: current origin/main after PR #187, `research/experiments/manifests/terminal-bench-v3-k1-gemini-low-{campaign-manifest,machine-analysis-inventory}.json`, central `derived/evidence-cas`, merged Quality Ledger, current raw-shape/storage audits, and research-context calibration freeze.

## CampaignAnalysisManifest mapping

| Contract field | Real source | Current evidence / normalization |
|---|---|---|
| campaign/source commit/actor | PR #185 campaign manifest | `terminal-bench-v3-k1-gemini-low-screen`, execution commit `a1897ee…`, Peter |
| source campaign manifest digest | bytes of merged PR #185 JSON | manifest builder computes SHA-256; not copied from prose |
| attempted entries | PR #187 inventory | 7 entries: 5 cohort trials, 1 quarantined auth attempt, 1 free control |
| source role | inventory `role` | preserve `infrastructure_retry_1`, `spec_2`…; never overload as cohort membership |
| cohort inclusion | inventory cohort array membership | five true; control/quarantine false |
| attempt role | normalized source role | successful bun retry=`retry`; spec_2…5=`primary`; free control=`control`; auth attempt=`quarantined_attempt` |
| task/verifier digest | inventory | nullable for free control where source says `n/a`; unknown reason recorded |
| CAS URI | inventory | all seven use `cas://sha256/<64hex>` |
| quality | inventory + Quality Ledger | cohort: four `warn`, one `pass`; quarantine/control preserve `quarantine|no_atif` |
| accounting | inventory `accounting` | planned 5, executed 7, cohort 5, retry 1, control 1, quarantine 1, unresolved 0 |

Schema correction from this mapping: trial role is split into `source_role`, `cohort_included`, and `attempt_role`; quality accepts the complete ledger enum; nullable control digests are explicit unknowns.

## TrajectoryIR mapping

| IR group | Authoritative source | Required handling |
|---|---|---|
| experiment/spec/job/trial | manifest + raw result/lab metadata | exact strings; no names parsed into IDs |
| document/session/trajectory/steps | raw ATIF restored from CAS | preserve ATIF `step_id`, `tool_call_id`, `source_call_id`; multiple calls per step retained |
| source bytes and CAS | CAS record/blob | every result/lock/ATIF/verifier/state source carries relative path, bytes, SHA-256, CAS URI |
| task/verifier/environment/scaffold/model | lock/result/config/lab metadata | null + unknown reason when absent; never infer from filename |
| outcome/reward/exception | trial `result.json` and verifier outputs | null reward is unknown/verifier unavailable, not zero |
| quality | merged Quality Ledger loader | missing report=`quality_not_evaluated`; fail/quarantine not model-callable |
| unpaired calls | quality findings + ATIF link audit | current four warning TB3 trials retain exact unlinked count; linkage-dependent labels unavailable |
| action/program/skeleton | deterministic parser with version/config digest | profile-dependent exit meaning is unknown without pinned semantics profile |
| state/verifier edges | explicit state journal/check refs only | no temporal-adjacency causality |
| opportunity | benchmark/profile contract citations | absent authority => unavailable; adjacency after error is not recovery opportunity |

Observed current TB3 cohort boundary: four trials contain repeated `ATIF_UNPAIRED_TOOL_CALL` warnings; `ico-path-patch` is quality pass with only two steps and reward zero. The IR must represent both without inventing observations, recovery, or capability conclusions.

## EvidencePack mapping

| Pack group | IR/raw source | Gate |
|---|---|---|
| header/contracts/outcome/coverage | deterministic IR + cited task/verifier bytes | mandatory budget reserve |
| selected evidence | exact CAS-hydrated CitationHandles | whole windows only; redacted content hash separate from raw source hash |
| episode index | deterministic IR boundaries | summaries `advisory_only=true` |
| omitted ranges | every unselected IR event | bounds/counts/type counts/ordered digest/reopen citations mandatory |
| pair evidence | valid AlignmentRecord | no pair window when counterpart confounded/unavailable |
| token accounting | frozen tokenizer + complete serialized pack | mandatory overflow => tier/reopen/abstain; pack cannot reach model |

Current campaign production smoke must start from the five PR #187 CAS URIs. `.worktrees/tbench3-screen` trial directories are stale development evidence and cannot satisfy campaign acceptance.

## Judgment / decision / calibration mapping

| Contract | Current source | Status |
|---|---|---|
| MachineJudgment v1 | merged Platform runtime PR #193 plus frozen Track B ontology/prompt | main emits deterministic abstentions; model output remains closed to the frozen ontology |
| AcceptanceDecision v1 | merged Platform runtime PR #193 | pure Platform gate; automatic acceptance disabled |
| CalibrationReport v1 | research-context `306b720`: genuine Gemini 3.7 Flash High, 14/14 valid rows, no fallback | `experimental_hold`; accepted 0, coverage 0, no class enabled |
| human baseline | source audit `01ee5d6` plus frozen v1.1 protocol | no eligible existing corpus; require author raw-label release or new offline three-qualified-rater calibration |
| citation entailment | deterministic resolution plus Track B strict-support scoring | resolution/completeness 1.0 but strict support 0.379; semantic pass cannot open acceptance |

## Benchmark-specific evidence checks

| Family | Durable evidence observed | Required system disposition now |
|---|---|---|
| Gemini TB3 | five CAS-backed cohort trials + retry/control/quarantine accounting | build IR/pack; preserve warning coverage; judgments remain calibration-gated |
| AgentAbstain | readiness contract `e090a05`: 0 admitted, 1 verified HOLD, 130 pending single-delta audit, 132 informational exclusions; free control has no ATIF | admit no production pair; pending candidates require pinned-byte `SingleDeltaAdmissionGate` plus nine controls; existing control abstains `source_missing|pair_unavailable` |
| DeepPlanning | no reopenable local Harbor/CAS identity found | unavailable; do not fabricate six trials from an opinion note |
| LOCA | task/materializer pins but no durable Harbor trial/ATIF/quality/CAS in audited corpus | unavailable; task folders are not trajectories |

## Contract validation cases

1. The five Eval Lab runtime/input definitions compile under the combined JSON Schema; the sixth interface, CalibrationReport v1, is validated by its Synthetic Research-owned external schema and pinned SHA-256 rather than duplicated.
2. PR #187 records map without dropping any attempted entry or converting `n/a` to a digest.
3. Wrong/missing CAS, source, content, redaction, task, verifier, or quality digest fails closed.
4. A warning TB3 item with unpaired calls can build IR/pack but cannot auto-accept a linkage-dependent class.
5. Missing ATIF/reward/pair produces explicit unknowns and deterministic abstention.
6. Mandatory-window overflow produces no model-callable pack.
7. A class gate is copied from one exact frozen CalibrationReport row; v1 report/class enable flags are false and cannot be overridden by AcceptanceDecision.
8. Rebuilding PostgreSQL/Parquet from CAS sidecars preserves artifact digests and decisions.
