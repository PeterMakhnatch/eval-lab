# Repo Custodian: validate resolver artifact value schema

## Target

- Branch/worktree: `hygiene/digest-assertion-hardening` at `/private/tmp/eval-lab-synth-promotion-hardening`
- Current head: `acc92343416d872ddf82f546b79248ed4f1c1f6a`
- Review: `/tmp/system-architect-digest-refusal-rereview-acc92343.md`

Preserve the closed behavior at `acc92343`: typed refusal reasons through the view, distinct non-mapping/erroring resolver reasons, removed score-summary fallbacks, checked-in BBO unresolved truth, exact default path with all five verified and gap 2.0, and the declared-property cutover. Do not integrate yet.

## Narrow code repair

Inside the existing guarded external-resolver boundary, copy and validate the complete returned mapping before any `.get()` or artifact verifier use:

- `task_dir`: only `str | os.PathLike | None`;
- `metric_config`, `visible_outcome`, `hidden_outcome`: only `Mapping | None`;
- malformed known-key values return a stable typed `artifact_resolver_invalid_return: <component> ...` reason;
- exceptions while iterating/copying/accessing a custom `Mapping` return `artifact_resolver_error: ...`;
- every resolver contract failure leaves all five components unresolved, compatibility/arithmetic false, and gap null;
- unknown keys may be ignored, but no known malformed value may escape or be reclassified as digest mismatch;
- do not accept prebuilt verification results and do not add another resolver/registry abstraction.

## Required permanent public-path tests

Replace/extend the current control integration coverage so it proves the exact production contract rather than relying on an injected resolver:

1. Synthetic explicit authoritative evidence using the default locator, asserted through both `materialize_analysis_control_views()` and real `run_cli`, with all five component statuses verified, overall verified, compatible/arithmetic true, gap exactly 2.0, reason null.
2. View-level missing, partial exact artifacts, one-component mismatch, non-mapping resolver, raising resolver, and malformed known values inside a Mapping.
3. A custom Mapping whose access/copy operation raises, classified as `artifact_resolver_error`.
4. Full checked-in BBO CLI row: overall plus all five components unresolved, compatible/arithmetic false, gap null, and exact unresolved-components reason.

## Acceptance

- `uv run pytest -q tests/test_analysis_control.py tests/test_autonomous_research.py tests/test_digest_assertion_hardening.py`
- Independent minimal probe for malformed known values/custom Mapping access errors and exact default materialize+CLI.
- Ruff check/format touched files; `git diff --check`; clean worktree after commit.
- Return commit hash, exact diff surface, focused evidence, and BBO CLI row.
- No evidence rewrite, model run, integration, main sync, or unrelated formatting.
