---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
---

# Active vs Historical Asset Taxonomy & Lifecycle Rules

This guide defines the lifecycle boundaries, retention guarantees, and archival rules across active and historical assets in Eval Lab, anchored to [docs/content-inventory.md](content-inventory.md) and [docs/git-estate-inventory.md](git-estate-inventory.md).

---

## 1. Lifecycle Classification Taxonomy

```text
                               ┌────────────────────────┐
                               │   EVAL LAB ASSETS      │
                               └───────────┬────────────┘
                                           │
             ┌─────────────────────────────┴─────────────────────────────┐
             ▼                                                           ▼
┌──────────────────────────┐                               ┌──────────────────────────┐
│      ACTIVE ASSETS       │                               │    HISTORICAL ASSETS     │
├──────────────────────────┤                               ├──────────────────────────┤
│ • Runtime Code & CLI     │                               │ • Immutable Zone 1 Traces│
│ • Unit & Contract Tests  │                               │ • Milestone Analysis Logs│
│ • Active Experiment Specs│                               │ • Promoted Golden Runs   │
│ • Pinned Task Registries │                               │ • Closed PR Hand-offs    │
│ • Rebuildable Parquet    │                               │ • Archived Prompt Briefs │
└──────────────────────────┘                               └──────────────────────────┘
             │                                                           │
             ▼                                                           ▼
┌──────────────────────────┐                               ┌──────────────────────────┐
│   Continuous Evolution   │                               │     Append-Only / Read   │
│   & Invariant Protection │                               │     Protected from Purge │
└──────────────────────────┘                               └──────────────────────────┘
```

---

## 2. Active vs Historical Inventory Breakdown

| Asset Category | Active State Definition | Historical / Archival State Definition | Invariant & Retention Policy |
|---|---|---|---|
| **Python Code (`src/evallab/`)** | Authoritative active implementation across subpackages (`schemas`, `storage`, `evidence`, `interpretation`, `recovery`) and root runners. | Obsolete monolithic modules and superseded intermediate scripts. | Code evolves with backwards compatibility; obsolete legacy files are cleanly excised during approved cutovers. |
| **Test Suites (`tests/`)** | Active focused pytest suites defending observable contracts and golden rendering. | Deprecated temporary repro scripts. | Maintained in lockstep with contract changes; never execute project-wide suites locally during interactive development. |
| **Execution Runs (`runs/` vs `research/evidence/runs/`)** | Active scratch runs generated during development (`runs/trial_jobs/`). | Immutable golden baseline runs promoted to `research/evidence/runs/` and CAS blobs (`derived/evidence-cas/`). | Golden runs are permanently committed; local scratch runs are gitignored and subject to pruning via `evallab gc`. |
| **Columnar Data (`derived/parquet/`)** | Live queryable Parquet partitions used by DuckDB attach surfaces. | Historical compacted partition archives. | Rebuildable on demand from Zone 1 evidence via `evallab data backfill` or `evallab.storage.parquet_compaction`. |
| **Research & Analysis (`research/`)** | Active campaign proposals and working SQL queries (`research/analysis/queries.sql`). | Completed investigation reports, benchmark writeups, and historical discovery ledgers. | Historical analysis reports are append-only context; do not edit historical findings retroactively. |
| **Task Packages (`library/tasks/`)** | Active benchmark tasks registered in `library/tasks/` with verifiable oracles. | Deprecated candidate tasks or superseded benchmark versions. | Preserved with origin classifications and immutable reference solutions. |
| **Generated Docs (`docs/repo-map.md`, `INDEX.md`, `STATUS.md`)** | Live projections reflecting HEAD codebase state. | Historical documentation versions in git history. | Regenerated deterministically via CLI tools; never hand-edited. |

---

## 3. Storage Invariants & Eviction Moratorium

1. **Mixed-Authority Roots:**
   - `derived/evidence-cas/` is durable content-addressed storage (Zone 1) and must **never** be blindly purged with ephemeral caches.
   - `derived/parquet/` is derived analytics (Zone 3) and is freely rebuildable from raw evidence.
2. **Archival Integrity:**
   - Historical records in `research/analysis/` and `docs/prompts/` document the evolutionary history of the lab. Correct terminology only in active navigation docs without mass-rewriting historical files.
3. **Branch & Worktree Hygiene:**
   - Active work proceeds in isolated worktrees (`.worktrees/<name>`). Completed branches and merged worktrees are pruned according to [docs/git-estate-inventory.md](git-estate-inventory.md).
