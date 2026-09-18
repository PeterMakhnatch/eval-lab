# System Architect — exact-head rereview after integrated data P1 repairs

## Exact target

Read-only review in `/private/tmp/eval-lab-staged-spine-integration`, branch `analyst/synth-data-promotion-hardening`, exact clean head `6843da00`.

Commit chain from prior blocked review:

- blocked head `54b8155b5bea3942d9826a6d75b36c7bb2abb8da`, report `/tmp/system-architect-integrated-cas-projection-review-54b8155b.md`
- `51519edb` — quality settlement cutover + initial bootstrap migration
- `79349c26` — generated docs
- `3ddfb199` — terminal-locator materialization and canonical source-path binding
- `6843da00` — generated docs, current head

Existing CAS/projection sources, approvals, map, and canonical parent `09b83977` remain authoritative. No edits/rebase/backfill/mutation/model/control/subagents.

## Required rereview

Return APPROVE only if both prior P1s are causally closed and the repair adds no authority escape.

### Quality cutover

1. Confirm old `persist_quality_ledger`, `load_quality_report_for_trial`, `register_quality_tables_in_duckdb`, root-level quality files, `read_parquet`/`parquet_scan`, and typed-empty public quality views have zero live definitions/callers.
2. Trace report/finding rows end to end: exact caller-held `EvidenceLocator` -> authenticated materialization with filesystem-dependent evaluation inside live lifetime -> deterministic per-trial producer -> exact job projection contract -> atomic schema writer -> settlement ledger/final event -> validated captured-byte `storage.attach` session table/readiness. No raw-path continuation after missing/wrong/tampered locator.
3. Inspect `_export_quality_tables`: private-only helper, no live/direct unmanifested caller or standalone compatibility authority. `project_jobs`/`ingest_and_project` must preserve exact produced/declared table set/order and fail atomically.
4. Verify CLI `_ingest_command` explicitly establishes CAS authority with an explicit store and passes exact locator mapping; no implicit store/record discovery. Remove raw input after archive during an adversary and prove projection still succeeds from CAS. Missing/wrong locator must not catalog/project quality.
5. Verify AnalysisWorker pre-model gate remains fail-closed and bound to frozen/current exact source identity, with no mutable Parquet read and no broadened model permission. Check whether the report/check/status identity is adequately frozen/recomputed; BLOCK if mutable source can pass or quality status can drift without stale/tamper refusal.
6. Re-run/extend quality replacement adversary before first attach and between queries; admitted rows must not change and subsequent mismatch is typed blocked. Check missing/quarantined/failed/not-applicable readiness, no fake table.

### Bootstrap locator/source binding

7. Confirm `_analyze_dispatched_jobs` selects the matching terminal queue event by exact request/job identity, validates all locator fields/digests, constructs `EvidenceLocator`, and runs every filesystem-dependent load/analysis while authenticated materialization is live. No raw run directory or unauthenticated durable path is transport.
8. Review `Queue._promote_control_bootstrap_job` CAS-to-durable publication for exact locator binding, destination conflict/refusal, atomicity, symlink/path safety, content verification, and event/provenance continuity. Durable publication is a canonical derived artifact, never a substitute locator.
9. Review new `run_trial_analysis(canonical_trial_path=...)`/admissibility composition. The override must not permit arbitrary relabeling: actual materialized bytes, canonical durable bytes, stored source path and source digests must be one exact authority. Check absent/wrong/different-but-same-named path, byte mutation, symlink swap, replacement during analyzer call, and post-sidecar/pre-promotion tampering. Preserve all existing caller behavior and TrialAdmissibilityError semantics.
10. Confirm the tamper test fails for the right contract, not only an incidental later error.

### Schema and canonical preservation

11. Independently validate the `FACT_SCHEMAS["state_events"]` list-child change: demonstrate PyArrow/Parquet roundtrip requires explicit `element`, exact writer + unchanged approved verifier accept it, wrong/nullability/child type/name still refuse. Ensure this is producer correctness, not verifier normalization or unrelated schema drift.
12. Confirm `storage/settlement.py`, `parquet_io.py`, `attach.py`, `reconciliation.py`, `trajectory_data_quality.py` remain exact approved authority where claimed; `data_backfill.py`, historical generator/evidence remain exact parent. Confirm evaluator semantics are exact prior semantics (no schema-version/verifier weakening).
13. Re-run prior CAS/projection/canonical/historical matrices plus repaired quality/bootstrap tests, negative searches, mutable-file adversaries, pinned `f414` historical dry-run/shared verifier, CLI help/golden, statics/governance/docs. Check clean head/tree.

Custodian reports 861 passed/8 skipped across 32 suites, canonical 294/4 skipped, CAS 242, projection 122/2 skipped, quality 10, type/lint/governance/docs clean. Independently substantiate; passing counts alone are insufficient.

Write `/tmp/system-architect-integrated-data-repair-rereview-6843da00.md` with exact head/tree, findings by severity, causal P1 disposition, contract/adversary/test matrix, and final **APPROVE** or **BLOCK**. If BLOCK, name exact file/symbol/root cause/minimal repair/test. Page `wH:p9` with verdict.
