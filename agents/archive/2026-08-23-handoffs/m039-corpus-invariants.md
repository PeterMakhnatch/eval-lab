Status: done
Last: merged as PR #131 (`bf1c931`)
Next: none
Blockers: none

# M039 — the suite pinned a photograph of live data

Status: complete — ready for review
Last: replaced frozen-snapshot assertions in
`tests/test_evidence_queries.py::test_full_corpus_derived_parquet_coverage_real`
with the invariants they were standing in for.
Next: nothing in this slice. INGEST (M029) should look at the projection gap noted
below.
Blockers: none.

## What broke, and why it mattered

Running the **free** oracle/nop battery to produce task-registration evidence took
the corpus from 92 to 94 trials. That turned the suite red:

```
FAILED tests/test_evidence_queries.py::test_full_corpus_derived_parquet_coverage_real
  - AssertionError: Expected 92 corpus trials, got 94
```

Nothing was wrong. The lab did exactly what a lab is for, and its own tests said no.

The test asserted a photograph of live data:

```python
assert total_trials == 92
assert summary["local-lab/event-summary"] == (67, 3, 64, 62, 2)
assert summary["petermakhnatch/transaction-reconciliation"] == (13, 7, 6, 6, 0)
assert taxonomy["ValueError"] == ("unknown", 9, 3)
assert total_exceptions == 16
```

Worse, the counts depend on **which checkout runs the test**: the derived root is
per-checkout, so `attach()` resolved **94** rows from the primary tree and **6** from
a git worktree. A test whose verdict changes with `cwd` is not protecting a contract.

This is the same defect family the lab has now hit three times: the argparse `--help`
golden that pinned CPython's formatter, `status_generator`'s fallback that substituted
a different dataset, and now a corpus-size constant. Each pinned something incidental
instead of something invariant.

## What it asserts now

| Kept as invariant | Why |
|---|---|
| `n == never_measured + measured` per task | the split is a partition; violation is a real view bug |
| `sum(v_task_summary.n) == count(trial_facts)` | the summary must account for every row |
| historical tasks still present (when the resolved root holds the full corpus) | evidence disappearing is the regression worth catching |
| every taxonomy row has a class, a phase, `n > 0`, `tasks_affected > 0` | exceptions must be classified, whatever the counts |

`HISTORICAL_CORPUS_FLOOR = 92` is kept as a **floor, never an equality**, with a
comment saying so. Growth is expected; shrinkage is a bug.

## Mutation evidence

```
MUT 1 — v_task_summary miscounts n (count(*) + 1)
FAILED test_full_corpus_derived_parquet_coverage_real
  - AssertionError: v_task_summary must account for every trial_facts row

MUT 2 — v_task_summary silently drops a task (WHERE task_name <> 'local-lab/event-summary')
FAILED test_full_corpus_derived_parquet_coverage_real
  - AssertionError: historical task vanished: local-lab/event-summary

restored -> 7 passed
```

Verified green from both roots: the worktree root (6 projected trials) and the
primary derived root (94 trials).

## Observation for INGEST (M029)

`evallab doctor` reports `FAIL catalog-parquet catalog=80 projected=6 missing=74`
from a worktree. The 8 battery job dirs landed on disk and only some projected. That
is exactly INGEST's remit — completeness as an invariant — and is not touched here.
