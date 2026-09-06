# System Architect — Final Review of Normalized Resolver Artifacts

## Target

- Branch: `eval-runner/digest-resolver-inner-normalization`
- Commit: `22600b4277b0f26f1a643df4468019e53d509174`
- Base: exact blocked head `ca1662a46d22abb15d51599ea413ff9f02262708`
- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/eval-runner-digest-resolver-inner-normalization`
- Repair brief: `/Users/petermakhnatch/Developer/eval-lab/research/inbox/ops-eval-runner-digest-resolver-inner-normalization-repair.md`
- Prior review: `/tmp/system-architect-digest-resolver-schema-final-review-ca1662a4.md`

Return strict **APPROVE** or **BLOCK** at `/tmp/system-architect-normalized-resolver-final-review-22600b42.md` and page the parent. Do not edit or merge.

## Reported closure to verify

- Diff from `ca1662a4` changes only `src/evallab/autonomous_research.py` and `tests/test_analysis_control.py`.
- Accepted `task_dir` is normalized with `os.fspath` inside the existing guard; failing or non-string normalized values become `artifact_resolver_error`.
- Accepted known metric/outcome `Mapping` values are copied to plain dicts and canonical-JSON exercised inside the guard; access/copy/serialization failures become `artifact_resolver_error`.
- Outer runtime-type violations remain exact `artifact_resolver_invalid_return`.
- Every handled failure returns overall plus task/verifier/metric/visible/hidden unresolved, compatibility and arithmetic false, gap null, exact typed reason.
- Default locator direct + real CLI reports all five verified, true/true, gap `2.0`, null reason.
- Checked-in BBO direct + real CLI reports all five unresolved, false/false, gap null, exact five-component unresolved reason.
- Reported focused tests passed; touched Ruff and format check passed; independent broken inner-`Mapping` probe returned typed resolver error without escape.

## Review requirements

1. Verify exact base/head/diff and absence of registry/evidence/model/staged-spine changes.
2. Independently probe:
   - wholly non-`Mapping` resolver output;
   - outer custom `Mapping.get` failure;
   - known inner custom `Mapping` iteration/access failure;
   - plain mapping with nested non-JSON value;
   - failing and non-string custom `PathLike`;
   - partial map, one-component mismatch, unknown key, and resolver exception.
3. For every malformed/raising/custom case, assert all six statuses, false/false/null, and exact typed reason through the production materialization boundary.
4. Exercise default-locator and checked-in BBO real CLI rows and assert every component plus exact reason/gap/booleans.
5. Reconfirm all prior F1–F3 digest/property closures and no score-summary/prebuilt-verification fallback regression.
6. Run the same three focused test files and touched-file Ruff/format checks.

APPROVE only if no external resolver value can raise after the guarded boundary and the complete public direct/CLI contract is locked. Otherwise BLOCK with the smallest exact closure. Approval authorizes only semantic replay of the reviewed production deltas onto the current staged spine.
