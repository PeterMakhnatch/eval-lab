Status: review-wanted
Last: M023 CRAFT-BATCH — classify batching with tested idempotence
Next: Integrator review and merge into main
Blockers: none

# M023 CRAFT-BATCH Handoff

## Summary of Changes
1. **Pinned Idempotence Contract in Tests**:
   - Added tests covering directory entries, symlink creation, symlink retargeting, and broken symlinks in `task_digest` computation (`craft.py:350`).
   - Added CLI and API tests verifying that two consecutive scans over an unchanged corpus result in zero churn (`churn.is_empty` is True) and byte-identical output with skipped disk write.
2. **Classify Batching**:
   - Refactored `craft.scan` to process task directories in batches via `scan_tasks_batch(task_dirs, source)`.
   - Added `DEFAULT_BATCH_SIZE: int = 10` constant bounded to balance grouping efficiency against memory overhead and LLM context window limits (`docs/platform-architecture.md §6`).
   - Added `--batch-size` CLI argument to `craft scan`.
3. **Idempotence & Isolation Under Batching**:
   - Proved batched and unbatched outputs are strictly identical across arbitrary batch sizes (`test_batched_output_equals_unbatched_output_across_batch_sizes`).
   - Verified that mutating a single task inside a batch causes only that specific task to be rewritten (`churn.digest_changed == (mutated_ref,)`), preserving isolation across batch boundaries.
4. **Property-Based Hardening (Hypothesis)**:
   - Added `test_batch_invariance_property`: `scan(..., batch_size=N) == scan(..., batch_size=M)` for all corpus sizes and batch sizes.
   - Added `test_idempotence_zero_churn_property`: Re-scan over unchanged corpus always produces `churn.is_empty` and skips write.
   - Added `test_partial_mutation_isolation_property`: Single-item mutation within a batch isolates churn to that item only.

## Verification
- `bash scripts/premerge.sh` green: Python 3.12, 1331 passed, ty 28 <= 28.
- Test suite in `tests/test_craft.py` grew from 51 to 59 tests (+ 3 property tests with dozens of fuzzed examples each).
