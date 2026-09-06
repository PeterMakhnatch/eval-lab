# Ops Eval Runner — Resolver Final Assertion Closure

Create a fresh isolated repair branch/worktree from exact `22600b4277b0f26f1a643df4468019e53d509174`, or advance your existing isolated branch only if it is clean and still exactly at that head. Do not use the dirty old synth worktree, dirty root, or staged spine.

Authoritative review: `/tmp/system-architect-normalized-resolver-final-review-22600b42.md`.

Make **test-only** changes in `tests/test_analysis_control.py`:

1. Route `test_materialize_views_with_raising_custom_mapping_records_typed_error` through the existing `_scale_status_row_for_resolver` and `_assert_fail_closed_scale_row` helpers so it asserts overall plus task/verifier/metric/visible/hidden all unresolved, compatibility/arithmetic false, gap null, and exact reason `artifact_resolver_error: custom mapping access failed`.
2. In direct checked-in BBO materialization test `test_headline_scale_and_selection_refusals`, add `score_scale_compatible` to the query/result and assert it is exactly `False`, retaining all existing arithmetic/gap/reason/status assertions.
3. Do not touch production, registry, evidence, policies, model code, or unrelated formatting.

Run:

- `uv run pytest -q tests/test_analysis_control.py tests/test_autonomous_research.py tests/test_digest_assertion_hardening.py`
- `uv run ruff check tests/test_analysis_control.py`
- `uv run ruff format --check tests/test_analysis_control.py`

Return branch/worktree/new commit, exact diff, counts, and confirmation the commit is test-only and all other worktrees were untouched.
