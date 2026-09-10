# System Architect: digest refusal-semantics re-review

## Target

- Branch/worktree: `hygiene/digest-assertion-hardening` at `/private/tmp/eval-lab-synth-promotion-hardening`
- Prior blocked head: `f97620769fda04fcb256100c2e1caf4b012c775a`
- Repair head: `acc92343416d872ddf82f546b79248ed4f1c1f6a`
- Prior report: `/tmp/system-architect-digest-production-path-review-f9762076.md`

## Scope

Final-review only the four prior blockers:

1. declared scale bindings preserve the extractor's exact typed `scale_binding_unresolved_reason` through `v_scale_binding_status.scale_refusal_reason`;
2. non-mapping/raising resolvers produce distinct `artifact_resolver_invalid_return` / `artifact_resolver_error` reasons, all unresolved components, no arithmetic, and null gap;
3. `scores.visible` / `scores.sealed` summaries are no longer treated as authoritative outcome-binding artifacts, so absent exact sources are unresolved rather than mismatch;
4. exact synthetic default materialization and real CLI tests assert all five component statuses, overall status, compatibility, arithmetic, gap, refusal reason, and malformed/missing/partial/mismatch cases rather than merely `run_id`.

Confirm the complete exact path still emits all five verified statuses, compatible/arithmetic true, and gap 2.0. Confirm the checked-in BBO CLI row is unresolved with the exact unresolved-component reason and arithmetic false. Confirm F1–F3 and the clean declared-property cutover remain intact.

Run the same focused three test files, an independent minimal production-path probe, and touched-file Ruff. Do not edit or merge.

Return `APPROVE` or `BLOCK` with evidence. Write `/tmp/system-architect-digest-refusal-rereview-acc92343.md` and page `wH:p9`. No generated evidence rewrite, model run, main sync, or integration.
