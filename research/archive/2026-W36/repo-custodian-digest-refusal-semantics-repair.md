# Repo Custodian: repair production scale refusal semantics

## Target

- Branch: `hygiene/digest-assertion-hardening`
- Worktree: `/private/tmp/eval-lab-synth-promotion-hardening`
- Current head: `f97620769fda04fcb256100c2e1caf4b012c775a`
- Review report: `/tmp/system-architect-digest-production-path-review-f9762076.md`

Work only in the existing branch/worktree. Preserve F1–F3 and the verified extractor/binding behavior through `4d2bb8f8`, plus the `score_scale_binding_declared` cutover and public resolver plumbing from `f9762076`. Do not integrate yet.

## Required repair

1. **Preserve typed refusal reasons through the public view.**
   - When a scale binding is declared but cannot authorize arithmetic, `analysis_control._calibration_row()` must persist `AutonomousResearchFeatures.scale_binding_unresolved_reason` as `scale_refusal_reason`.
   - Do not replace `unresolved_components: [...]`, `mismatched_components: [...]`, or resolver-specific reasons with generic `validated scale binding absent`.
   - Evidence-level `scores.transfer_gap_null_reason` may apply only when no scale binding is declared, or as explicitly supplemental context that cannot hide verifier detail.

2. **Distinguish malformed/erroring external resolvers from missing artifacts.**
   - Validate the external resolver once at the extractor authority boundary.
   - Preserve stable typed reasons such as `artifact_resolver_invalid_return` and `artifact_resolver_error` for non-mapping returns and raised exceptions.
   - Keep all component statuses unresolved, arithmetic false, and transfer gap null.
   - Do not create or accept a precomputed verification result and do not swallow the failure into `{}` before the extractor can classify it.

3. **Remove non-authoritative outcome-summary fallbacks.**
   - The default locator must not treat generic `scores.visible` or `scores.sealed` summary objects as the authoritative visible/hidden outcome-binding payloads.
   - Resolve only explicit authoritative fields/paths such as `visible_outcome`, `hidden_outcome`, or exact policy-declared sources.
   - If no exact source exists, report unresolved, not mismatch.

4. **Replace the vacuous integration test.**
   - Build a synthetic exact task/verifier/metric/visible-outcome/hidden-outcome binding fixture.
   - Assert the full materialized row: all five component statuses, overall status, `score_scale_compatible`, `arithmetic_permitted`, gap, and refusal reason.
   - Exercise the actual `run_cli` default-locator path using explicit source fields.
   - Add malformed resolver, resolver exception, missing, partial, and mismatch materialized-view assertions.
   - Assert the checked-in BBO evidence is unresolved for absent exact sources rather than falsely mismatched.

## Acceptance

- Focused suite: `tests/test_analysis_control.py`, `tests/test_autonomous_research.py`, and `tests/test_digest_assertion_hardening.py`.
- Ruff check/format only touched files.
- Worktree clean; `git diff --check` clean.
- Return the new commit hash, exact diff surface, focused test/Ruff results, and observed real CLI BBO row.
- No generated evidence rewrite, model run, main sync, integration, or unrelated formatting.
