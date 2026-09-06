# Platform Builder — read-only audit of in-progress isolation integration

## Target

Read-only review; do not edit any worktree.

- Integration worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Branch: `analyst/synth-data-promotion-hardening`
- Canonical base: `adc8a65a`
- Approved isolation source: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/darwin-isolation-admission-final`
- Approved source range: `46794c166c39cb64c7c41350f8906b5ec3badab1..34efd8c15363c8bf671afe6ce313c022c3212025`
- Integration brief: `research/inbox/repo-custodian-integrate-isolation-34efd8c1.md`
- Active writer: Repo Custodian `wH:p0`; coordinate findings through `page wH:p0`, and page parent `wH:p9`.

## Audit question

The Custodian is preserving the newer canonical external-lineage/resolver authority while grafting isolation/admissibility authority. Focus on the currently difficult overlaps:

1. `src/evallab/analysis_control.py` and `sql/views.sql`: identify exact canonical resolver/scale-binding columns and views that must remain, plus exact isolation/admissibility fields/bindings that must be added. Point out any current mismatches causing `tests/test_analysis_control.py` failures.
2. `tests/test_analysis_control.py`: confirm all 16 canonical top-level tests remain and identify only the assertions that legitimately change under strict isolation/admissibility.
3. `tests/test_registry.py`: confirm all 49 canonical top-level tests remain plus 8 isolation tests; inspect `_make_control_job` / `_make_control_evidence` and give the exact current-schema `RunProvenance`, runtime identity, network evidence, result, and trial-admissibility sidecar shape required for those fixtures. Identify any current missing/lost fixtures.
4. Check the in-progress diff is exactly the approved 27 paths (20 tracked plus 7 new), with no out-of-scope formatting.
5. Compare top-level symbols and test names against canonical base; flag every canonical loss.

Do not run a broad suite. You may run only the failing focused test(s) needed to ground the diagnosis. Do not format, commit, reset, rebase, integrate, or run models.

## Output

Write `/tmp/platform-builder-isolation-integration-draft-audit.md` with concise exact findings, file/symbol references, and recommended edits. Page `wH:p0` with actionable findings and `wH:p9` with the report path. Confirm no files changed.
