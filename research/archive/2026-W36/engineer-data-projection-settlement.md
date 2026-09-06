# Engineer Data: projection settlement ledger, canonical root, and attach readiness

## Base and isolation

- Create a fresh isolated worktree and branch `feat/projection-settlement-ledger` from exact commit `93d2e7c184ce607ea57c86f732bd493bfba2489d` (`feat/next-buildout-report`).
- Never switch, reset, clean, merge, or edit the dirty primary checkout.
- One writer owns this worktree.
- Commit the finished focused change and report branch, full commit, worktree, changed files, migration/API details, focused commands/results, and remaining limitations.

## Shared authority contract

- Evidence CAS is immutable source truth.
- PostgreSQL is the transactional settlement/catalog/lineage authority.
- Parquet files are rebuildable, versioned projections admitted only when bound to an exact CAS/source/producer/schema manifest.
- DuckDB is query federation only. It must never infer readiness from filesystem presence.
- The separate Ops lane is hardening generic-run CAS settlement and Harbor runtime compatibility. Do not edit `runner.py` or depend on an uncommitted Ops symbol. Consume the existing canonical evidence archive record shape and keep the settlement input boundary narrow.

## Production ownership

Own the smallest existing modules/migrations needed for:

- `evidence/atif.py` ingestion/projection settlement;
- PostgreSQL settlement/projection registry persistence;
- canonical projection-root resolution in `storage/paths.py` and interpretation runtime defaults;
- `storage/attach.py` Z3 readiness/admission;
- a deterministic, non-mutating reconciliation inventory surface;
- focused tests.

Avoid unrelated analysis/admissibility schema changes. Prefer a dedicated settlement module/migration over expanding heavily shared catch-all schemas when repository conventions permit.

## Required state machine and binding

Implement one append-only/transactional settlement state machine for run and interpretation sources:

`discovered -> source_validated -> cas_committed -> cataloged -> projecting -> ready | projection_failed | quarantined`

Each settlement must bind, without inference:

- stable source/run/trial identity;
- CAS URI, CAS content digest, and immutable source-manifest digest;
- runtime identity/compatibility result when present;
- exact required/optional projection table set;
- producer name/version/code digest;
- schema version/digest per table;
- canonical Parquet path/URI, final file digest, row count, and partition identity;
- exact state transitions/timestamps and typed failure reason;
- supersession/rebuild lineage without deleting historical evidence.

Use repository canonical JSON/digest/timestamp helpers. Fail closed on malformed, missing, duplicate, reordered, mismatched, stale, or ambiguous authority fields. PostgreSQL may commit `cataloged` before projection, but no consumer may treat it as `ready`.

## Projection publication

For every required table:

1. reopen and verify the immutable CAS source manifest;
2. derive with pinned producer/schema identity;
3. write a temporary file under the canonical projection root;
4. reopen and verify schema, row count, source digest, and computed file digest;
5. atomically publish;
6. transactionally mark that exact table ready.

A source is analytically ready only when all required tables are ready. Explicit optional tables may become `not_applicable`; missing/failed/stale required tables may not.

## Canonical roots

- Keep human-readable immutable interpretation sidecars at `derived/interpretation` as CAS source material.
- Make interpretation Parquet writers use the same canonical root resolved by `derived_root_from_environment`, normally `derived/parquet`, as attach readers.
- Expected interpretation layout is under `derived/parquet/{interpretation_artifacts,machine_judgments,acceptance_decisions}/...`.
- Remove the split default where the writer uses `derived/` and attach reads `derived/parquet/`.

## DuckDB attach admission

- Consume durable per-table settlement/projection manifests, not raw filesystem presence.
- Expose machine-readable table states: `ready`, `not_applicable`, and `missing/failed/stale` with exact reason.
- Zone status must be `ready`, `partial`, or `unavailable` with counts; 27/39 can only be `partial`, never fully attached.
- Create normal views only for exact ready entries.
- A registered `not_applicable` relation may receive a typed empty view constructed from its registered schema.
- Never synthesize `SELECT * FROM (VALUES (NULL)) t LIMIT 0` or any anonymous/inferred null-column placeholder for a missing required table.
- A caller that requires a missing/failed/stale relation must receive a typed refusal.

## Reconciliation inventory

Add a deterministic read-only inventory that joins CAS records, PostgreSQL settlement/catalog rows, expected per-table manifests, physical Parquet files, and interpretation sidecars. Classify exact objects as matched, missing source, missing projection, extra projection, stale producer, digest mismatch, or unverifiable. Do not delete/adopt/backfill the observed 106 missing or 10 extra objects in this branch. Extras must remain quarantined from attach until exact ownership is proven.

## Focused acceptance

- PostgreSQL-first ingestion records `cataloged`, then exact per-table projecting/ready/failure states; retry is idempotent and resumes without duplicate catalog rows or immutable evidence rewrites.
- File publication is temporary + verified + atomic; injected write/reopen/schema/row-count/digest failures never mark ready.
- Manifest mismatch in CAS source, producer, schema, file digest, partition, or required set fails closed.
- Interpretation default writer and attach reader resolve the same canonical root.
- 39/39 exact ready tables yields zone `ready`; 27/39 yields `partial` with 12 exact reasons and no fake normal relation; absent root yields `unavailable`.
- `not_applicable` typed empties preserve exact declared column names/types.
- The non-mutating inventory classifies missing/extra/stale/digest-drift examples deterministically and never deletes or adopts files.
- Run focused tests for changed contracts plus touched-file Ruff/format checks. Skip project-wide suites and do not mutate the operator's real derived data.

## Review source

Architect review: `/tmp/system-architect-e2e-data-architecture-review.md`. Treat the operator census `156/50/106/10`, `27/39`, and `195/39/39` as observed inputs for later reconciliation, not as values to hard-code into tests or production logic.
