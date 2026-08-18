# Audit Ledger

| date | subject | handoff | verdict | evidence path | risk note |
|---|---|---|---|---|---|
| 2026-08-18 | preflight | `agents/handoffs/preflight.md` | CONFIRMED | `research/audits/evidence/preflight/` | Core runtime and contract tests pass (31 tests); operator docs in `docs/operations.md` omit manual command reference |
| 2026-08-18 | storm | `agents/handoffs/storm-status.md` | CONFIRMED | `research/audits/evidence/storm/` | Storm engine, tests (11 tests), models, and banner utilities verified; engine lacks a direct CLI subcommand |
| 2026-08-18 | parquet-compaction | `agents/handoffs/parquet-compaction.md` | CONFIRMED | `research/audits/evidence/parquet-compaction/` | Compaction engine, tests (15 tests), and dry-run across 72 jobs on 9 tables pass; runs via module invocation `python -m evallab.parquet_compaction` |
