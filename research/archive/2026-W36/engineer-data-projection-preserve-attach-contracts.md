# Engineer Data — preserve existing attach contracts during CAS rebase

## Checkpoint

- Held checkpoint: `48be689fd4fa8bf4cc96e92d6988379842436d17` on `feat/projection-settlement-ledger`.
- Do not resume until parent supplies the final approved Ops CAS head/API.
- This brief is additive to `research/inbox/engineer-data-projection-settlement.md` and the mandatory immutable CAS locator migration.

## Parent regression finding

The checkpoint changes `tests/test_attach.py` by roughly `-1181/+109` lines and deletes `tests/test_attach_properties.py`; the replacements exercise mostly private `_attach_z3`. This drops established public-surface coverage for Z2, Z4, CLI behavior, DSN redaction, SQL determinism, semantic/mechanical views, multi-layout precedence, and full `attach()` behavior. A contract cutover may change Z3 expectations, but deleting unrelated or still-valid coverage is not acceptable.

## Required repair after CAS rebase

- Restore the existing attach and attach-property tests from base `93d2e7c` as the starting point.
- Preserve all still-valid public behavior and coverage: Z2 unavailable/real-catalog behavior, Z4 front matter, CLI zones/query/print-SQL exit behavior, DSN/password redaction, deterministic SQL, cross-zone queries, public `attach()` result/status reporting, semantic-vs-mechanical view identity joins, and table registration.
- Adapt only the Z3 assertions intentionally changed by the new contract:
  - unmanifested legacy Parquet must not be admitted as ready;
  - ready/partial/unavailable and per-table ready/NA/missing/failed/stale must come from verified manifests;
  - no fake empty relation for merely missing required data;
  - explicit schema-defined NA may expose the typed empty relation required by the contract.
- Migrate legacy hot/cold/standalone/overlap tests by writing verified settlement manifests and exact table bindings, or change their expectation to typed unavailable/partial where the new contract intentionally refuses them. Do not simply delete the behaviors.
- Keep new settlement property tests, but make the core acceptance exercise public `attach()` in addition to private helpers.
- Restore property coverage or move it transparently with equivalent test names/observable assertions; no broad coverage loss.
- Run the restored attach/CLI/public-surface suite together with the new settlement/reconciliation/trajectory suite and report exact counts.

Architect review will treat unexplained deletion of established tests/contracts as a blocker even when the new focused 110-test matrix passes.
