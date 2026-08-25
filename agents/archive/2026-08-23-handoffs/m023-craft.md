Status: done
Last: merged as PR #123 (`d9dee45`)
Next: none
Blockers: none

# M023 CRAFT-BATCH Handoff

## Contract Compliance Verdict
`craft.py` **honoured its documented idempotence contract** (comments at `:272`, `:350`, `:1028`, `:1038`, `:1123`) completely and required zero bug fixes to its underlying contract logic. Specifically:
- **`:272` (Timestamp omission)**: Records omit scan-time timestamps, ensuring row digests remain deterministic across scans.
- **`:350` (Directory & symlink digest coverage)**: `task_digest` strictly hashes directory entries, file contents, and symlink targets without dereferencing loops.
- **`:1028` (Record digest witness)**: `records_digest` provides an encoding-independent witness that matches identically across consecutive unchanged scans.
- **`:1038` (Churn facet separation)**: `compute_churn` cleanly partitions `digest_changed` from `facets_changed`, `added`, and `removed`.
- **`:1123` (Skipped write preservation)**: `write_records` compares sha256 hashes of temporary vs existing Parquet files, leaving both bytes and `mtime` intact when no facts changed.

## Non-Vacuity Test Audit
All new tests in `tests/test_craft.py` were audited to confirm they operate on non-empty corpora:
- `test_directory_entries_and_symlinks_affect_task_digest`: Operates on real directory entries and symlinks.
- `test_cli_scan_is_idempotent_and_skips_rewrite`: Scans a non-empty corpus of 2 tasks.
- `test_batched_output_equals_unbatched_output_across_batch_sizes`: Scans 12 tasks across batch sizes 1, 2, 3, 4, 5, 7, 10, 11, 12, 13, 50.
- `test_scan_batch_handles_decode_and_os_errors_gracefully`: Scans a mix of valid tasks and corrupted manifests.
- `test_batched_scan_is_idempotent_on_rescan`: Scans 8 tasks with `batch_size=3`.
- `test_partial_change_under_batching_rewrites_only_changed_row`: Scans 9 tasks with `batch_size=3` and mutates a single task in batch 2.
- Property tests (`test_batch_invariance_property`, `test_idempotence_zero_churn_property`, `test_partial_mutation_isolation_property`): Enforce `num_tasks >= 1` (or `>= 2` for mutation isolation).

## Batch Size Parameter & Bounds
- Defined as `DEFAULT_BATCH_SIZE: int = 10` in `src/evallab/craft.py:72`.
- Explained in comment: bounds batch size to balance grouping throughput against memory overhead and LLM prompt context window limits (`docs/platform-architecture.md §6`).
- Exposed via CLI `--batch-size` with validation (`batch_size > 0`).

---

## Mutation Evidence

### Mutation 1: Breaking Digest Skip (Re-scan Rewrites File)
**Mutation applied to `src/evallab/craft.py` (`write_records`)**:
```python
# Replaced sha256 equality check and unlink with unconditional replacement:
temporary.replace(path)
rewritten = True
```

**Real Pytest Output**:
```
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.4.2, pluggy-1.6.0
rootdir: /Users/petermakhnatch/Developer/eval-lab/.worktrees/m023-craft
configfile: pyproject.toml
plugins: hypothesis-6.165.10
collected 62 items / 59 deselected / 3 selected

tests/test_craft.py FFF                                                  [100%]

=================================== FAILURES ===================================
______________ test_rescan_of_an_unchanged_corpus_churns_nothing _______________
    ...
        second = craft.write_records(craft.scan([source]).records, out)
    
        assert first.rows == second.rows == 2
        assert first.digest == second.digest
        assert second.churn.is_empty
>       assert second.rewritten is False
E       AssertionError: assert True is False
E        +  where True = WriteResult(... rewritten=True).rewritten

tests/test_craft.py:641: AssertionError
________________ test_cli_scan_is_idempotent_and_skips_rewrite _________________
    ...
>       assert mtime1 == mtime2
E       assert 1787083659587415741 == 1787083659590862220

tests/test_craft.py:867: AssertionError
__________________ test_batched_scan_is_idempotent_on_rescan ___________________
    ...
>       assert second.rewritten is False
E       AssertionError: assert True is False
E        +  where True = WriteResult(... rewritten=True).rewritten

tests/test_craft.py:955: AssertionError
=========================== short test summary info ============================
FAILED tests/test_craft.py::test_rescan_of_an_unchanged_corpus_churns_nothing - AssertionError: assert True is False
FAILED tests/test_craft.py::test_cli_scan_is_idempotent_and_skips_rewrite - assert 1787083659587415741 == 1787083659590862220
FAILED tests/test_craft.py::test_batched_scan_is_idempotent_on_rescan - AssertionError: assert True is False
======================= 3 failed, 59 deselected in 0.26s =======================
```

---

### Mutation 2: Breaking Batch Boundary Isolation (Polluting Whole Batch on Single-Item Change)
**Mutation applied to `src/evallab/craft.py` (`scan_tasks_batch`)**:
```python
# If any task in a batch was modified, invalidate all task digests in the batch:
if any("Modified" in (t / "instruction.md").read_text() for t in task_dirs if (t / "instruction.md").is_file()):
    records = [r.model_copy(update={"task_digest": r.task_digest + "_polluted"}) for r in records]
```

**Real Pytest Output**:
```
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.4.2, pluggy-1.6.0
rootdir: /Users/petermakhnatch/Developer/eval-lab/.worktrees/m023-craft
configfile: pyproject.toml
plugins: hypothesis-6.165.10
collected 62 items / 61 deselected / 1 selected

tests/test_craft.py F                                                    [100%]

=================================== FAILURES ===================================
_________ test_partial_change_under_batching_rewrites_only_changed_row _________
    ...
        # Mutate task_04 (middle item of batch 2: task_03, task_04, task_05)
        (tasks[4] / "instruction.md").write_text("Modified instruction for task 04\n")
    
        # Second scan with batch_size=3
        second_res = craft.scan([source], batch_size=3)
        second_write = craft.write_records(second_res.records, out)
    
        assert second_write.rows == 9
        assert second_write.rewritten is True
        # ONLY task_04 changed its digest!
>       assert second_write.churn.digest_changed == ("test/corpus\ttask_04",)
E       AssertionError: assert ('test/corpus...pus\ttask_05') == ('test/corpus\ttask_04',)
E         
E         At index 0 diff: 'test/corpus\ttask_03' != 'test/corpus\ttask_04'
E         Left contains 2 more items, first extra item: 'test/corpus\ttask_04'
E         
E         Full diff:
E           (
E         +     'test/corpus\ttask_03',
E               'test/corpus\ttask_04',
E         +     'test/corpus\ttask_05',
E           )

tests/test_craft.py:987: AssertionError
=========================== short test summary info ============================
FAILED tests/test_craft.py::test_partial_change_under_batching_rewrites_only_changed_row - AssertionError: assert ('test/corpus...pus\ttask_05') == ('test/corpus\ttask_04',)
======================= 1 failed, 61 deselected in 0.27s =======================
```

---

### Restoration & Verification
After reverting both mutations (`git checkout src/evallab/craft.py`):
```
$ bash scripts/premerge.sh
Resolved 75 packages in 3ms
Audited 51 packages in 1ms
All checks passed!
........................................................................ [  5%]
........................................................................ [ 10%]
........................................................................ [ 16%]
........................................................................ [ 21%]
........................................................................ [ 26%]
............................................................x........... [ 32%]
........................................................................ [ 37%]
......................................s................................. [ 43%]
........................................................................ [ 48%]
........................................................................ [ 53%]
........................................................................ [ 59%]
........................................................................ [ 64%]
........................................................................ [ 70%]
........................................................................ [ 75%]
........................................................................ [ 80%]
........................................................................ [ 86%]
........................................................................ [ 91%]
........................................................................ [ 97%]
......................................                                   [100%]
1332 passed, 1 skipped, 1 xfailed in 106.77s (0:01:46)
...
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 28
```
