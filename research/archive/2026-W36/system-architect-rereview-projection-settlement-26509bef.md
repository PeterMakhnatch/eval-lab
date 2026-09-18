# System Architect — final projection settlement blocker re-review

After the currently assigned strict historical generator review, re-review the repaired projection head.

## Exact candidate

- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/projection-settlement-ledger`
- Branch/head: `feat/projection-settlement-ledger @ 26509bef00ac0bc19666e3436c1bdc04c357f878`
- Parent blocked head: `bdb33e5810e789a1aceb2d4a27c67e4f368ee205`
- Approved CAS base: `078cf287b05d80bb88bd20d87b112b33b250f688`
- Prior report: `/tmp/system-architect-projection-settlement-review-bdb33e58.md`
- Repair commit: 7 files, +507/-72; clean.

Read-only. Do not edit/rebase/integrate/backfill, run model/live PostgreSQL, or spawn subagents.

## Required closure

### P1 non-ready manifests

Verify public attach returns deterministic per-table partial/unavailable for every active non-ready state (`discovered`, `source_validated`, `cas_committed`, `cataloged`, `projecting`) and terminal failure (`projection_failed`, `quarantined`). Reasons must derive only from state, final canonical event reason code, and that table’s own failure reason. No nonexistent manifest attribute, other-table borrowing, exception, or false readiness.

### P2 immutable session authority

Verify every admitted Parquet path is captured once into bytes; digest, schema, and row count are checked against those exact bytes; the same bytes are parsed via Arrow; and DuckDB copies authenticated tables into session-owned temporary tables. Public views/CLI/query paths must reference only those session tables, never reopen mutable Parquet paths.

Adversarially replace the file:

1. between readiness/capture and binding;
2. immediately after attach before first query;
3. between repeated queries.

Only exact admitted rows may be returned. Confirm common Hive partition columns are reconstructed from manifest authority without filename inference, and deduplication cannot mix identities across tables/partitions.

### Static/regression closure

- All six prior `ty` diagnostics closed through real narrowing/types.
- Ruff format drift closed.
- All pre-checkpoint attach/property tests retained.
- New public state and race tests bind actual attach/query behavior.
- Prior PASS conclusions remain: CAS-only locator authority, monotonic ledger, canonical root, atomic publication, read-only reconciliation, per-table typed readiness, explicit not-applicable vs zero-row-ready.

## Output

Run the projection/pipeline, full attach/property/settlement, trajectory runtime/data-quality matrices plus targeted P1/P2 probes, touched `ty`, Ruff/format, compile, diff check, clean status.

Write `/tmp/system-architect-projection-settlement-rereview-26509bef.md` beginning `APPROVE` or `BLOCK`; page `wH:p9` with exact verdict. Confirm no edit/backfill/model/live-DB/subagent.
