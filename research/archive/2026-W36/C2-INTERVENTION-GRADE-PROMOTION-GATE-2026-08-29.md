---
type: decision-memo
topic: c2-intervention-grade-promotion-gate
date: 2026-08-29
status: distilled
owner: C2InterventionBuilder
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-29
license_note: Internal decision record; Eval Lab repository license applies.
feeds:
  - parked
---

# C2 Intervention Grade-Promotion Gate

## Decision

Add a fail-closed C2 (matched intervention contrast) grade-promotion contract and a
deterministic consumer seam that decides whether a C1 MATCHED analysis may be promoted
to C2. The gate never infers or defaults a prerequisite: any missing or tampered
requirement, and any mismatch between the C1 input and the recipe, yields an explicit
`REFUSAL` with an enumerated reason. A matched-only C1 input with no intervention recipe
is always refused (`C1_ONLY`). Relabeling C0 or C1 as C2 is grade inflation and is
mechanically barred.

Implemented in `src/evallab/interpretation/c2_intervention_gate.py` with a module runner
(`python -m evallab.interpretation.c2_intervention_gate`) mirroring the established
`trajectory_recipe_run.py` consumer-seam pattern. PR #303 owns the C1 token/cache rows in
`feature_registry.py`; this change does not touch them or any C0 projection file.

## The C2 contract

A C1 matched analysis is promoted to C2 **only when every** prerequisite is proven AND
the C1 input matches the recipe on every cross-checked dimension:

| Prerequisite | Fail-closed reason | Enforced by |
|---|---|---|
| Analysis-ready C1 matched input | `c1_not_analysis_ready`, `c1_not_matched` | `C1MatchedInput.analysis_ready`, `.matched` |
| Immutable intervention/recipe identity | `missing_recipe_identity` | non-empty `recipe_id`/`family`/`construct` |
| Recipe digest matches recomputation | `tampered_recipe_digest` | `expected_recipe_digest()` over identity payload |
| Declared treatment/control delta | `missing_declared_delta` | recipe `declared_delta`, arms differ |
| Recipe and C1 agree on family/construct/delta | `family_mismatch`, `construct_mismatch`, `delta_mismatch` | C1 values compared to recipe |
| Recipe verifier/source digests present + SHA-256 | (model enforced) | `Field(pattern=SHA256_PATTERN)` |
| C1 digests equal recipe digests | `verifier_digest_mismatch`, `source_digest_mismatch` | equality with recipe |
| Manipulation opportunity denominator | `missing_manipulation_opportunity`, `zero_manipulation_opportunity`, `opportunity_mismatch` | observed C1 denominator `> 0` and equal to recipe |
| Observed manipulation application | `missing_manipulation_application` | `C1MatchedInput.manipulation_applied` |
| Manipulation evidence identity | `missing_manipulation_evidence` | non-empty `manipulation_evidence_ref` |
| Observed effect | `missing_effect` | `C1MatchedInput.effect_observed` |
| Effect evidence identity + measured value/predicate | `missing_effect_evidence`, `missing_effect_identity` | non-empty `effect_evidence_ref`, `effect_identity` |
| No unintended delta | `extra_unintended_delta` | empty `unintended_delta` |
| Twin linkage present and declared | `missing_twin_linkage`, `twin_linkage_undeclared` | C1 twin key is one of the recipe's `twin_linkage_keys` |
| Distinct arms | `arms_not_distinct` | control/treatment trial IDs differ |

Every prerequisite is bound into the decision's canonical content identity via
`c1_input_digest` (the canonical digest over the full C1 input), so changing the effect,
digests, denominator, twin key, or any evidence ref changes the decision identity.

`InterventionRecipe` and `C1MatchedInput` are immutable `ContractModel` records (the construct is stored on the `construct_name` field, avoiding shadowing of `ContractModel.construct` and emitting no Pydantic warning);
`C2PromotionDecision` carries `decision` (`VALID`/`REFUSAL`), the enumerated `reasons`,
and `decision_id` / `decision_digest`. `decision_id` is the canonical digest over the
identity body (everything except `decision_id`/`decision_digest`); `decision_digest` is
the canonical digest over the identity body plus `decision_id`. Both are recomputed and
verified at validation, so tampering either independently is rejected.

## Bundled synthetic Action64 smoke

The Action64 mechanism (action-memory-v1 dose ladder) exposes context chunks under opaque
non-semantic handles (`ctx_<hex>`) retrieved one at a time; unscaffolded seed-1337 shows
opaque-handle transcription/set/order faults, and a one-turn-per-handle scaffold exhibits
O(n²)-like cumulative context (semantic stopped at 232/257, ~6.7M prompt tokens). The
high-value C2 intervention is **handle representation/issuance**: hold content/order/seed
fixed and change only opaque-ID vs indexed/range/batch retrieval.

`python -m evallab.interpretation.c2_intervention_gate --synthetic` builds a deterministic
synthetic twin proving that intervention: recipe identity, single declared delta
(`handle_representation`), a declared manipulation opportunity denominator (257 = the
64KiB read-opportunity count), manipulation applied with an evidence ref, effect observed
with an evidence ref and a measured effect identity, twin linkage
(`am-dose-ladder-v1-s1337-d65536` declared in the recipe), and pinned verifier/source
digests. It emits a reproducible `VALID` decision with a stable digest — a no-paid smoke.
This is a synthetic fixture/next-exact-recipe, not a real run; it does not claim an
empirical effect.

## Negative controls (tests)

- matched-only C1 with no recipe → `c1_matched_only_no_intervention`;
- missing recipe identity → `missing_recipe_identity`;
- tampered recipe digest → `tampered_recipe_digest`;
- family / construct / delta / verifier-digest / source-digest / opportunity / twin-key
  mismatch between C1 input and recipe → distinct mismatch refusals;
- arms not distinct → `arms_not_distinct`;
- no-op (declared but never applied, no effect) → refuses;
- fabricated/missing evidence: empty `manipulation_evidence_ref`, `effect_evidence_ref`,
  or `effect_identity` → refuses;
- extra unintended delta → `extra_unintended_delta`;
- zero/missing manipulation opportunity → refuses;
- not-analysis-ready / unmatched / missing twin linkage / missing verifier or source
  digest → refuse;
- a valid synthetic intervention reaches `VALID`;
- mutating any C1 field changes `c1_input_digest`, `decision_id`, and `decision_digest`;
- `decision_id` and `decision_digest` are split and each tamper is independently rejected.

## Verification

- `tests/test_c2_intervention_gate.py` — focused negative + positive + runner smoke tests.
- Touched files pass `ruff check` and `ty` (zero diagnostics).
- No changes to `feature_registry.py`, C0 projection files, or `cli.py`.
