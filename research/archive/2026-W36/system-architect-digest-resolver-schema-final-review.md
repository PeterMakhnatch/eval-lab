# System Architect: final resolver artifact-schema review

## Target

- Branch/worktree: `hygiene/digest-assertion-hardening` at `/private/tmp/eval-lab-synth-promotion-hardening`
- Prior blocked head: `acc92343416d872ddf82f546b79248ed4f1c1f6a`
- Repair head: `ca1662a46d22abb15d51599ea413ff9f02262708`
- Prior report: `/tmp/system-architect-digest-refusal-rereview-acc92343.md`

## Scope

Final-review only the two remaining blockers:

1. The guarded external-resolver boundary copies/accesses the returned Mapping under exception handling and validates known values: `task_dir` only `str | PathLike | None`; metric/visible/hidden only `Mapping | None`. Malformed known values become typed `artifact_resolver_invalid_return`; custom Mapping access/copy errors become typed `artifact_resolver_error`; all five statuses unresolved, arithmetic false, gap null. Unknown keys cannot bypass the known-value contract. No prebuilt verification result/parallel abstraction exists.
2. Permanent public-path tests exercise default explicit source location and real CLI with all five verified, compatible/arithmetic true, gap exactly 2.0, reason null; missing, partial, one-component mismatch, non-mapping, raising, malformed-known-value, custom Mapping access error; and the full checked-in BBO unresolved row with exact reason.

Confirm all prior acc92343 closures and F1–F3/declared-property behavior remain unchanged. Confirm the final commit diff is only `autonomous_research.py` and `tests/test_analysis_control.py`; no registry formatting churn survives.

Run the same focused three-file suite, independent malformed Mapping/default locator+CLI probes, and touched-file Ruff. Do not edit or merge.

Return `APPROVE` or `BLOCK` with evidence. Write `/tmp/system-architect-digest-resolver-schema-final-review-ca1662a4.md` and page `wH:p9`. No evidence rewrite, model run, main sync, or integration.
