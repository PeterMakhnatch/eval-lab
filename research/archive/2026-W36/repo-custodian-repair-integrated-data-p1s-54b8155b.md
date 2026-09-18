# Repo Custodian — repair exact-head CAS/projection P1 blockers

## Starting identity and authority

Work only in clean canonical integration worktree `/private/tmp/eval-lab-staged-spine-integration`, branch `analyst/synth-data-promotion-hardening`, exact blocked head `54b8155b5bea3942d9826a6d75b36c7bb2abb8da` (tree `56f4d475`). Do not touch dirty root checkout. Do not rewrite the three existing landing commits; add focused repair commit(s), then an official generated-doc commit only if required.

Blocking review: `/tmp/system-architect-integrated-cas-projection-review-54b8155b.md`. Approved CAS/projection sources and prior integration contracts remain authoritative. This is a clean cutover: no compatibility path/root/view, raw-path downstream authority, placeholder ready view, verifier weakening, inference, projection/backfill of legacy data, or model/control run.

## Repair P1 — eliminate live mutable trajectory-quality projection island

Retain deterministic trajectory quality evaluation, but retire every live mutable publication/read path:

- `interpretation.trajectory_quality.persist_quality_ledger`
- `interpretation.trajectory_quality.load_quality_report_for_trial`
- `interpretation.trajectory_quality.register_quality_tables_in_duckdb`
- CLI/worker callers and old tests that assume root-level mutable Parquet or public `read_parquet` views.

The final tree must contain zero public/direct `read_parquet`/`parquet_scan` use for these tables, zero typed-empty compatibility views, zero root-level quality-table writer/merger, and zero live caller that projects from raw job paths after a `missing_cas_locator` quarantine.

Preserve the quality report/finding observable as a first-class settled output. Integrate its exact Arrow schemas and deterministic row production into the existing per-job authenticated CAS-locator -> `ProjectionSettlementManifest` -> monotonic ledger -> atomic verified Parquet flow. Requirements:

1. Only bytes reopened/materialized from an exact caller-held `EvidenceLocator` may produce report/finding rows. The materialization lifetime must cover every filesystem-dependent quality evaluation; never retain a dead temporary `JobRecord.path`.
2. Add quality report/finding tables to the exact per-job projection contract/partition (including deterministic zero-row findings where applicable), producer-code identity, schema/nullability, relative path, row count and digest settlement. Publication becomes ready only through existing verifier/ledger transitions.
3. Use the approved atomic writer/schema equality and existing `_settle_projection_result`/settlement recorder. No second derived root and no special readiness mechanism.
4. DuckDB exposure must use only canonical `storage.attach.attach`: captured validated bytes copied into session-temp tables plus the all-state `z3.table_readiness` relation. There must be no path-backed public view and no missing-table placeholder that looks ready.
5. Migrate `_ingest_command` to establish authentic CAS authority before ingestion. A user-selected raw job path MAY be an explicit archive input, but project only from the resulting independently authenticated locator; require an explicit store argument/config rather than implicit record discovery. Do not silently accept missing locators or continue writing analytics after quarantine.
6. Remove the `analysis_worker.py` direct derived writer/reader dependency. Preserve pre-model quality gating in a fail-closed form bound to exact source bytes and frozen request identity, or feed it the same settled authoritative output through canonical validated attachment; never reopen mutable Parquet. Do not broaden any model-call permission.
7. Remove obsolete functions/imports/exports and migrate every caller/test. Do not leave deprecated aliases/shims.

Required adversaries/tests:

- swap quality Parquet before first query and between queries: admitted query rows cannot change because attach captures bytes; mutated/mismatched bytes are blocked, not trusted.
- missing/quarantined/failed/not-applicable quality tables appear with correct typed all-state readiness and no fake public table.
- missing/wrong/tampered locator cannot produce a ready quality table or model-admissible quality gate.
- repeat identical authenticated source is idempotent; divergent settlement root/table is rejected.
- CLI ingest proves it archives/reopens/materializes the explicit source then settles quality tables; raw source removal after archive cannot break projection, and no locator means typed refusal.
- worker staging still has zero model calls and quality fail/quarantine behavior remains exact without mutable-ledger reads.

## Repair P1 — migrate stale canonical bootstrap helper

`tests/test_trial_admission_bootstrap.py::_analyze_dispatched_jobs` must not load `research/evidence/runs/<job-name>`. Reconstruct the exact `EvidenceLocator` from the relevant terminal queue event for each dispatched request, authenticate/materialize it in a context manager, and complete load/analysis while the context is live. Assert the raw completed-job directory is absent/not required. Re-run the exact seven-file canonical matrix from the Architect report; expected zero failures.

## Preservation and verification

- Preserve exact CAS locator/settled-run contract; isolation/admission/proxy/runtime-lock; queue/campaign transitions; context step/payload; memory continuity; MemGym source-only; historical generator/artifacts; CLI staged controls/historical contracts.
- Keep `storage/data_backfill.py`, `historical_git_snapshot.py`, and checked-in historical evidence byte-identical to `09b83977`.
- Preserve exact approved settlement/attach/reconciliation/parquet verifier semantics unless the quality-table contract requires a narrowly additive composition; never weaken validation/nullability/final-event/single-root rules.
- Re-run focused quality, evidence ingest/projection, settlement/attach/property/Z3, analysis-worker, CAS transport, exact canonical authority, and historical dry-run/shared verifier matrices.
- Run Architect's mutable-file adversary, offline CAS archive/reopen/materialize, typed unavailable non-ready attach, final CLI help/golden, Ruff/format, compile, governance, repo-map/doc-index. Run project-supported type checker exactly; if unavailable, report it, do not fabricate.
- Inspect AST/symbol/test set differences and report zero unexplained canonical removal. Negative-search the exact blockers and prior integration map.
- Commit code/tests first. Regenerate official repo-map/doc-index and commit only those docs separately if changed. Leave clean.

Page `wH:p9` with full exact head/commit chain, changed paths/symbols, behavioral evidence/counts, negative-search results, and any unavailable service/tool. Do not claim completion if either P1 or any canonical matrix remains blocked.
