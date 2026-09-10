# Ops Eval Runner — Digest Resolver Inner-Normalization Repair

## Objective

Close the one remaining resolver-boundary escape and complete the public-path assertion matrix on top of the reviewed digest hardening stack.

## Base and workspace

- Create a new isolated worktree and branch from exact commit `ca1662a46d22abb15d51599ea413ff9f02262708`.
- Do not edit the dirty primary checkout.
- Do not rewrite or move `hygiene/digest-assertion-hardening` or the staged integration spine.
- Return a single focused commit for final System Architect review.

## Authoritative review

Read and implement the exact closure in:

- `/tmp/system-architect-digest-resolver-schema-final-review-ca1662a4.md`

## Required production repair

Keep the existing resolver boundary and result schema. Inside its existing guarded block:

1. For a non-null `task_dir` accepted as `str | os.PathLike`, call `os.fspath`, require the normalized result to be `str`, and store the normalized string. A failing or non-string `__fspath__` implementation is `artifact_resolver_error`.
2. For each non-null known metric/outcome value accepted as `Mapping`, materialize it into a plain dictionary and exercise the repository's canonical JSON encoding inside the guard before storing it. Custom access/copy/canonicalization failures and nested non-JSON values are `artifact_resolver_error`.
3. Continue to classify an outer runtime-type violation as `artifact_resolver_invalid_return`.
4. Preserve the existing fail-closed result exactly: overall plus task/verifier/metric/visible/hidden unresolved, compatibility false, arithmetic false, gap null, typed reason.
5. Add no second resolver abstraction, registry/evidence changes, waiver, fallback, or model execution.

## Required tests

Extend the existing focused tests, without duplicating production logic, to lock:

- wholly non-`Mapping` resolver return -> exact `artifact_resolver_invalid_return: expected Mapping, got ...` contract;
- valid known artifact value implemented as a failing custom `Mapping` -> typed `artifact_resolver_error`, never escaped exception;
- failing and non-string custom `PathLike` -> typed `artifact_resolver_error`;
- non-JSON content inside a plain known-value mapping -> typed `artifact_resolver_error`;
- every malformed/raising/custom failure asserts overall plus all five component statuses, false/false/null, and exact typed reason;
- default locator through direct materialization and real `run_cli` asserts all five verified, compatibility/arithmetic true, gap `2.0`, null reason;
- checked-in BBO direct materialization and real CLI assert the exact all-five unresolved matrix, compatibility/arithmetic false, gap null, and full exact unresolved-components reason;
- retain partial and one-component mismatch coverage.

## Verification

Run only focused validation in the isolated worktree:

- `uv run pytest -q tests/test_analysis_control.py tests/test_autonomous_research.py tests/test_digest_assertion_hardening.py`
- `uv run ruff check src/evallab/autonomous_research.py tests/test_analysis_control.py`
- `uv run ruff format --check src/evallab/autonomous_research.py tests/test_analysis_control.py`
- an independent custom inner-`Mapping` probe confirming no exception escapes;
- the real default-locator CLI and checked-in BBO CLI paths, with the exact public row values reported.

Do not run project-wide suites. Return branch, worktree, commit, changed-file list, focused command results, probe observations, and confirmation that the dirty root and staged spine were untouched.
