---
source_url: https://github.com/PeterMakhnatch/eval-lab/tree/archive/pr346-parent-stack-20260902
source_type: repo
retrieved: 2026-09-02
license_note: Internal repository branch; repository license applies.
status: distilled
feeds:
  - parked
---

# PR 346 parent stack parked

The former 27-commit parent stack is preserved at `archive/pr346-parent-stack-20260902` (head `8d0125de30429c9edcb92871a836cac6256f8016`). It contains the unlanded trial-admissibility and network-isolation authority integration, registry and task-workbench changes, analysis-control and cohort enforcement, evidence-fact and Parquet schema changes, and their supporting tests. It was parked because retargeting the full stack to `main` exposed 80 exact-head Python 3.12 failures across authority migration, analysis surfaces, schema compaction, and stale feature-governance expectations; folding those repairs into the ATIF memory mapper would create an unreviewable mega-merge. Reintroduce only pieces with a landed consumer, as separate small pull requests with independently green checks.
