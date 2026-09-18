# Scope-bound coverage v3: R2 status correction (Data Engineer → Integration)

Corrects the v2 singleton `derived/coverage/coverage-scope-90e3c9e9a058.json`, whose
embedded spec link still said `evidence-intact-not-cataloged-not-projected` while
its counts said catalogued 1 / projected 1. v1 (`...d9d0895d4c5b`) and v2 are
preserved on disk, unmodified.

## Correction product

`derived/coverage/coverage-scope-45a1d8a6d0e0.json` — singleton
`job_ids = ["f94f1507-7958-4e08-addb-92b50d97c387"]` (evidence UUID), same roots.
Spec-link `status` is now **derived from the coverage counts at generation time**
(`bound-catalogued-projected`) via `evallab.coverage_report.summarize_binding`,
never hand-written; derivation pinned by `test_summarize_binding_matrix`.
Rebuilds byte-identical (verified).

## Writer-integrity repairs behind it (lane/data-build-20260908)

Isolated reproduction showed three real concurrent-writer defects; all fixed with
focused behavioral proof (`tests/test_projection_integrity.py`, 9 tests):

1. **Shared fixed `.parquet.tmp` names** — two processes projecting one partition
   tore the file (reproduced: errors + unreadable output). Temp names are now
   pid+uuid unique; concurrent writers resolve to last-complete-file-wins.
2. **Silent same-path UUID replacement** — `database.ingest_job` deleted the old
   row (and its trials via cascade) with zero record. The new row's
   `lab_metadata` now carries `{"supersedes": <old-uuid>}`; same-id regeneration
   records nothing (9-test suite covers both directions against an isolated DB).
3. **Torn live partitions on interruption** — `project_jobs` now stages per job
   under inert `.staging-<pid>-<uuid>/` roots (proven invisible to discovery
   globs/SQL patterns) and publishes with atomic renames; a fault mid-projection
   records the failure and leaves no live trace (previously `jobs.parquet`
   persisted alone — one test updated to the atomicity contract with rationale).
   Opt-in `purge_inert_staging()` (default 24h, staging-only) for crash leftovers;
   nothing auto-cleans.

What was deliberately NOT changed: no uniqueness migration, no global locks, no
broad cleanup, no `cli.py`/`missions/ACTIVE.md` edits. The R2 UUID transposition
itself remains unattributed (candidates: path-scoped deletion on same-path
re-ingest, stale incoming identity — both now recorded/linked rather than
silent); the evidence was never modified and no backfill was performed.
