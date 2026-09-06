# System Architect — final projection settlement review

## Exact candidate

- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/projection-settlement-ledger`
- Branch: `feat/projection-settlement-ledger`
- Exact clean head: `bdb33e5810e789a1aceb2d4a27c67e4f368ee205`
- Commits: `86bcd66c` + `bdb33e58`
- Exact approved generic-run CAS base: `078cf287b05d80bb88bd20d87b112b33b250f688`
- CAS approval report: `/tmp/system-architect-run-cas-analyst-final-rereview-078cf287.md`

Read-only review. Do not edit/rebase/integrate/backfill/run a model or touch dirty root.

## Claimed candidate surface

18 paths, +4292/-531:

- SQL settlement schema;
- ATIF and Parquet authenticated materialization;
- trajectory interpretation writer/root behavior;
- queue locator propagation;
- attach readiness gating;
- reconciliation and settlement modules;
- projection/attach/pipeline/trajectory tests plus Z3 settlement helpers;
- officially regenerated repo map/index.

The builder reports that all pre-checkpoint attach/property tests were restored; only behavior expectations were adapted for manifest-gated ready semantics and typed not-applicable outcomes.

## Required verdict questions

### 1. Authority split and evidence identity

- Durable generic run truth remains CAS-only through `EvidenceLocator`; no record/content/archive path is accepted as authority.
- Projection producers rematerialize and authenticate exact CAS bytes against locator record/content digests before parsing/projecting.
- Mutable record discovery, caller-provided digest equality, record-path authority, and post-settlement fallback are absent.
- `SettledRun`/queue/ingest/campaign contracts from exact CAS base are preserved.

### 2. Durable settlement ledger

- Ledger is append-only/monotonic and binds source run identity, exact evidence locator, archive/record/content identity, projection/table/schema identity, partition identity, output content digest, row count, and status/reason.
- Partial attempts cannot masquerade as ready; invalid status transitions, duplicate/conflicting identities, or cross-run/table overwrites fail closed.
- Transaction boundaries do not declare ready before verified atomic Parquet publication; crash/interruption produces a recoverable non-ready state, not a false-ready manifest.
- PostgreSQL schema/migration behavior matches repository conventions without creating a competing metadata authority.

### 3. Canonical projection root and atomic publication

- Interpretation writers and attach readers agree on one canonical derived/parquet root and deterministic table/partition paths.
- Publication uses atomic temp-write/fsync-or-project-equivalent/rename semantics appropriate to the existing project contract; no partially written table is admitted.
- Inventory/reconciliation is deterministic and read-only; unexpected/unbound extras are typed unverifiable/quarantined, never auto-adopted, relabeled, or deleted.

### 4. Per-table readiness and attach semantics

- Every attached table is independently typed `ready`, `partial`, or `unavailable` from exact admitted settlement records; one table’s readiness does not lift another.
- DuckDB attaches only exact manifest-admitted ready files and refuses missing/digest-mismatched/unbound/stale files.
- Typed empty/not-applicable behavior is explicit and cannot be confused with a successfully projected zero-row table.
- Z3/property tests exercise the public `attach()` contract, relevant state transitions, and plausible crash/conflict/order bugs rather than only helper internals.

### 5. Regression and scope discipline

- Compare pre-checkpoint tests at CAS base against candidate: no broad deletion, weakened assertion, or removed public contract is hidden in the -531 lines.
- `tests/test_attach.py`, `tests/test_attach_properties.py`, and settlement properties preserve previous coverage and add the new contract.
- No live backfill, legacy inference, bespoke run settlement path, model invocation, or PostgreSQL requirement was smuggled into ordinary fixture tests.
- Generated docs were produced by the official generator and accurately reflect the candidate.

## Verification

Run focused tests sufficient to independently validate:

- projection settlement + pipeline;
- full attach + prior properties + settlement properties;
- trajectory runtime + data-quality;
- targeted adversarial probes for locator tampering, unbound extras, interrupted publication, status conflicts, per-table partial/unavailable, and attach digest mismatch;
- touched `ty`/Ruff/format/compile as applicable and `git diff --check`.

## Output

Write `/tmp/system-architect-projection-settlement-review-bdb33e58.md` beginning `APPROVE` or `BLOCK`, with:

- exact head and path/stat audit;
- authority/transaction/root/readiness conclusions;
- test-preservation comparison;
- commands/counts;
- exact blockers if any;
- no-edit/no-backfill/no-model confirmation.

Page `wH:p9` with the verdict and report path.
