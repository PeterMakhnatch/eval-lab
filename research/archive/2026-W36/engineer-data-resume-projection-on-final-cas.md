# Engineer Data — resume projection settlement on final CAS authority

## Exact inputs

Resume only now that generic run CAS is final-approved.

Projection writer worktree:

- `/Users/petermakhnatch/Developer/eval-lab/.worktrees/projection-settlement-ledger`
- branch `feat/projection-settlement-ledger`
- clean checkpoint `48be689fd4fa8bf4cc96e92d6988379842436d17`
- checkpoint is not review-ready.

Final CAS authority:

- worktree `/private/tmp/eval-lab-run-cas-content-identity-final`
- branch `fix/run-cas-content-identity-final`
- exact approved head `078cf287b05d80bb88bd20d87b112b33b250f688`
- Architect approval `/tmp/system-architect-run-cas-analyst-final-rereview-078cf287.md`

Existing mandatory attach-preservation brief:

- `research/inbox/engineer-data-projection-preserve-attach-contracts.md`

Do not edit the CAS worktree. Do not run models. Do not integrate to the canonical spine.

## Required migration

1. Rebase/migrate the projection branch onto exact final CAS head `078cf287...`, preserving projection semantics while accepting the final CAS implementation as authority for every overlapping CAS/runner/queue/campaign/schema file.
2. Remove the checkpoint's copied/path-authoritative f406 API. Projection settlement must consume `EvidenceLocator` (`store_root`, `kind`, `record_id`, expected record digest, expected content digest) and `materialize_evidence`; no `manifest_path`, `blob_path`, raw `job_dir`, or URI-only discovery may become authority.
3. The settlement ledger must bind the exact CAS record/content/archive identities and producer/schema/source identities, and reopen/materialize through the independent locator before projection.
4. Preserve the implemented append-only PG/filesystem settlement state machine, atomic Parquet publication, source/table/partition/row/file digest bindings, supersession validation, deterministic reconciliation, manifest-only Z3 attach readiness, canonical interpretation root, and per-decision partitions—repair them where final CAS API changes require it.
5. Treat the earlier broad attach-test deletion as a blocking regression. Start from the full pre-checkpoint public attach/property suites and preserve every observable contract named in `engineer-data-projection-preserve-attach-contracts.md`: Z2/Z4, CLI/query/print-SQL, redaction, determinism, cross-zone, semantic/layout/table registration, and public `attach()` coverage. Adapt only intentional manifest-gated Z3 ready/partial/unavailable expectations. No broad test deletion or private-helper-only substitution.
6. Inventory remains deterministic and non-mutating. Unknown/unbound extras are quarantined/unavailable; do not backfill or publish guessed settlement state.
7. No live database/model run unless already covered by deterministic local fixture infrastructure; report live-PG absence explicitly rather than simulating success.

## Verification and handoff

Run focused changed-contract suites covering:

- CAS locator reopen/materialization into settlement;
- settlement ledger transitions and idempotent/conflict replay;
- atomic publication and reconciliation inventory;
- interpretation canonical root;
- complete restored attach/public property surface, including typed per-table ready/partial/unavailable.

Also run touched Ruff/format and `git diff --check`. Inspect the exact diff against the rebased final-CAS base for broad test deletions and obsolete path authority. Commit cleanly, then page `wH:p9` with:

- exact old checkpoint, final CAS base, new head;
- rebase/conflict catalog;
- changed paths/stat;
- per-suite counts;
- explicit public attach test inventory preservation evidence;
- no-backfill/no-model/no-integration confirmation.
