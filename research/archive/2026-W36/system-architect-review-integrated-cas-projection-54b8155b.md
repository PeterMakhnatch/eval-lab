# System Architect — exact-head review of integrated CAS + projection canonical spine

## Exact integration

- Worktree/branch: `/private/tmp/eval-lab-staged-spine-integration`, `analyst/synth-data-promotion-hardening`.
- Parent canonical: `09b839770e53137dd09a7131776120d462a67761`.
- CAS semantic landing: `4a6a3093` (13 files, +1997/-533).
- Projection semantic landing: `9684b586` (18 files, +4751/-576).
- Final official docs: `54b8155b` (2 files, +41/-32), current exact head.
- Approved CAS source: `078cf287b05d80bb88bd20d87b112b33b250f688`.
- Approved projection source: `26509bef00ac0bc19666e3436c1bdc04c357f878`, semantic base `078cf287`.
- Binding integration map: `/tmp/ops-data-settlement-integration-map-09b83977.md`.
- Source approvals: `/tmp/system-architect-run-cas-analyst-final-rereview-078cf287.md`, `/tmp/system-architect-projection-settlement-rereview-26509bef.md`.

Read-only exact-head review. Do not edit/rebase/integrate/backfill/mutate legacy projections/run model/control/spawn subagents.

## Review requirements

Review actual code/diff and independently run targeted adversaries. Return APPROVE only if manual integration preserves both source contracts and every newer canonical authority.

### CAS landing

1. `EvidenceArchive`/`EvidenceLocator` remain content-addressed and caller-selected: exact store/kind/record plus independently expected record/content/archive digests; authenticated reopen/materialization; no mutable path authority.
2. `SettledRun.cas_locator` is the only completed-run transport across runner, queue, campaigns, analyst, CLI, and smoke. No raw job-dir/path, record glob/discovery, `cas_uri`, implicit store-root, `manifest_path`/`blob_path` fallback.
3. Runner preserves canonical isolation/admissibility, credential/proxy/Z.ai/DeepSeek, context and runtime-lock authority while freezing/settling bytes. Failed settlement is typed terminal/unsettled, not a completed run.
4. Queue/campaign composition preserves leases, admission/outcome/isolation transitions, action-memory and external-lineage/digest bindings.
5. Inspect caller migration in `smoke.py`: authenticated locator materialization, stable lifetime, no return to raw-path authority.
6. Evaluate intentional test replacements/removals: archive failure now fail-closed; old unauthenticated analyst hydration tests replaced by stronger locator tests. No coverage gap or hidden compatibility shim.

### Projection landing

7. `settlement.py`, `reconciliation.py`, `attach.py`, `parquet_io.py`, `trajectory_data_quality.py`, and approved projection tests match exact source authority where claimed; no schema/nullability verifier weakening.
8. Durable per-table monotonic ledger/final-event agreement, canonical single root, CAS-only source, captured immutable Parquet bytes, exact Arrow digest/schema/row-count, session-temp DuckDB tables, and typed all-state Z1/Z3 readiness remain exact.
9. No `read_parquet`/`parquet_scan` or path reopen from public views; no placeholder ready state; no split/compatibility root; no live projection mutation/backfill.
10. `trajectory_runtime.py`, `atif.py`, `queue.py`, `cli.py`, and `sql/schema.sql` are true semantic compositions: projection semantics plus canonical context step/payload authority, memory continuity, MemGym source-only provenance, isolation/admission, CAS locator, historical CLI, staged controls.
11. Canonical `storage/data_backfill.py`, `historical_git_snapshot.py`, and 131 historical artifacts remain byte-identical to parent. Confirm updated legacy readiness tests correctly require typed `STORE_JOIN_UNAVAILABLE` under partitioned settlement rather than masking a regression.
12. Final CLI parser/help/golden is the union of staged controls + historical contracts + CAS + projection; no source stale golden.

### Evidence and boundaries

13. Run negative searches from the map and explain diagnostic-only matches.
14. Re-run exact focused CAS and projection/property/Z3 tests plus canonical isolation/admission/memory/MemGym/historical CLI/verifier regressions. Run a pinned `f414512a` historical dry-run/shared verifier to prove identities/artifacts unchanged.
15. Exercise actual offline CAS archive/reopen/materialize, settlement state/refusal, captured-byte DuckDB Z1/Z3 attach/query/non-ready behavior, and final CLI help. If covered by an exact behavioral test, name the test and observed path.
16. Verify governance/repo-map/doc-index/statics and clean head. PostgreSQL/Harbor live-service absence must remain explicitly typed unavailable; no fallback or claim.

Custodian reported 587 passed/8 skipped across 25 files, statics/docs/governance clean, CAS symbol preservation, byte-exact core projection authority, and no backfill/model/control. Independently substantiate; passing count alone is not approval.

Write `/tmp/system-architect-integrated-cas-projection-review-54b8155b.md` with exact head/tree, findings ordered by severity, contract/negative/smoke/test matrix, and final **APPROVE** or **BLOCK**. If BLOCK, give exact file/symbol/root cause/minimal repair/test. Page `wH:p9` with verdict.
