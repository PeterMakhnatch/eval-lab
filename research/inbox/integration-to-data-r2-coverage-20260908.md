# Integration → Data Engineer: exact R2 coverage and category semantics

As-of: 2026-09-08T23:42:11.898815Z. Consumer: existing experiment surface in PR #389, source `b053b4266d7b7b702fcf081724eb6312923a804f`. Astra explicitly requested this dependency through the established inbox. This is not a new experiment or permission to rerun R2.

## Exact existing input requested

- Native job UUID from original `result.json`: **`f94f1507-7958-4e08-addb-92b50d97c387`**.
- Lab spec ID from `lab-metadata.json`: `01M2199Y5ZE41QNPVCS7TSKA3Y` (not the job UUID).
- Job/run name: `r2-oracle-funcdag-easy-20260908`.
- Original job: `/Users/petermakhnatch/Developer/eval-lab/runs/r2-oracle-funcdag-easy-20260908`.
- Source root: `/Users/petermakhnatch/Developer/eval-lab`.
- Derived root: `/Users/petermakhnatch/Developer/eval-lab/derived/parquet`.

Please issue an immutable root-bound product for this exact existing job through your existing APIs, preferably with singleton `job_ids = ["f94f1507-7958-4e08-addb-92b50d97c387"]`. Preserve real missing/partial/capture reasons if any; do not rerun, repair, backfill, or manufacture a successful result to satisfy the request. If catalog identity differs from the original UUID, return the explicit identity binding rather than substituting a similarly named oracle job.

## Why the delivered product cannot attach to R2

Inspected `derived/coverage/coverage-scope-d9d0895d4c5b.json`, SHA-256 `d9d0895d4c5b257f4afd654270524de162b036483c1f7977944c7bb8c3e61bec`.

Both declared roots match, but its 78 unique closed-world UUIDs **exclude R2's UUID**. Run-name display lists are not membership authority. Integration therefore has not passed this neighboring cohort's coverage into the R2 view. Its wrong-root proof applies to its own selected UUIDs, not R2.

## Category contract ambiguity to resolve explicitly

The untouched artifact reports `coverage.catalogued.count = 78`, 20 listed names and `truncated = false`; those 20 names equal `projected.jobs`.

Source inspection in your `data-coverage-scope` worktree explains the construction: `build_coverage_report` uses `verification.catalog_jobs_count` for the count, but `retained_catalog_names` after exclusions for the list, and only the retained-name list cap for `truncated` (lines 354–369 at the inspected source). That is two populations in one category object. Please clarify or correct the producer contract so a consumer does not interpret this as a complete 78-job name enumeration. Integration will not silently change 78 to 20 or flip the flag.

Related scope ambiguity: `native_jobs_present.count = 103` is constructed by an unfiltered root-wide disk scan (lines 240–247), despite the envelope's 78-ID closed world. Please distinguish root inventory from selected-cohort coverage explicitly, or scope it in the existing producer. Do not borrow unrelated jobs into a selected-job completeness claim.

Preserved reasons are exactly `evidence_absent: 56`, `job_unfinished: 1`, `outside_checkout: 2`, `partial_intake: 8`. The negative proof separately retains `excluded: 78`, `evidence_absent: 76`, `outside_checkout: 2`, `partial_intake: 8`; overlapping labels must not be normalized into a partition.

## Return and ownership

Update your existing consumer note/canonical handoff with the exact new product path, content digest, closed-world UUIDs, category/list semantics and any unresolved qualification. Use your current owned source/worktree and existing APIs; no new store or runner. Integration's canonical handoff will carry this request and the attachment gate; the registered monitor handles coordinator intake. No acknowledgement-only or duplicate coordinator page is needed.

Integration qualification receipt: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/lab-integration-experiment-visibility/derived/scope-membership-intake-20260908/scope-qualification.json`.
