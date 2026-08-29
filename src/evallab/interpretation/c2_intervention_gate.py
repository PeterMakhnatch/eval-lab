"""Fail-closed C2 (matched intervention contrast) grade-promotion gate.

C2 is the matched *intervention* contrast grade: a single declared treatment/control
delta applied to otherwise identical counterfactual twins, with the manipulation
provably applied, an observed effect, a proven manipulation-opportunity denominator,
no unintended delta, and pinned verifier/source digests.

This module implements the immutable grade-promotion contract that decides whether a
C1 MATCHED analysis may be promoted to C2. It is fail-closed: any missing or tampered
prerequisite, and any mismatch between the C1 input and the recipe, yields an explicit
REFUSAL with an enumerated reason. Nothing is inferred or defaulted. A C1 matched pair
that carries no intervention recipe can never become C2 (``C1_ONLY``), and relabeling a
matched-only analysis as C2 is grade inflation.

Prerequisites (all required):
- ``analysis_ready`` C1 matched input (control + treatment, both admitted).
- Immutable intervention/recipe identity plus a digest that matches a recomputation
  over the identity payload.
- A declared treatment/control delta (the single factor the intervention changes).
- Recipe and C1 agree on family, construct, declared delta, verifier/source digests,
  manipulation-opportunity denominator, and the twin-linkage key (a declared key).
- A proven manipulation-opportunity denominator (> 0) on the C1 input.
- Observed manipulation application and an observed effect, each with a non-empty
  evidence reference, plus a measured effect identity/value/predicate.
- No unintended delta beyond the declared one.
- Distinct control and treatment arm trial IDs.
- Counterfactual/twin linkage (the C1 twin key is one of the recipe's declared keys).
- Verifier and source digests (SHA-256 shaped on the recipe; equal on the C1 input).

Consumer seam: ``python -m evallab.interpretation.c2_intervention_gate`` reads a
``--recipe`` JSON and ``--c1-input`` JSON (or ``--synthetic`` for the bundled
reproducible synthetic Action64 handle-representation intervention) and emits a single
deterministic ``C2PromotionDecision`` on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from evallab.interpretation.trajectory_judgment import SHA256_PATTERN, canonical_json_digest
from evallab.schemas import ContractModel

PRODUCER = "c2-intervention-gate/v1"
CONTRACT_ID = "c2-intervention-grade-promotion-contracts-v1"
CONTRACT_DIGEST = canonical_json_digest({"contract": CONTRACT_ID, "producer": PRODUCER})

Decision = Literal["VALID", "REFUSAL"]


class C2RefusalReason:
    """Enumerated, machine-readable C2 grade-promotion refusal reasons."""

    C1_NOT_ANALYSIS_READY = "c1_not_analysis_ready"
    C1_NOT_MATCHED = "c1_not_matched"
    C1_ONLY = "c1_matched_only_no_intervention"
    MISSING_RECIPE_IDENTITY = "missing_recipe_identity"
    TAMPERED_RECIPE_DIGEST = "tampered_recipe_digest"
    MISSING_DECLARED_DELTA = "missing_declared_delta"
    DELTA_MISMATCH = "delta_mismatch"
    FAMILY_MISMATCH = "family_mismatch"
    CONSTRUCT_MISMATCH = "construct_mismatch"
    MISSING_MANIPULATION_OPPORTUNITY = "missing_manipulation_opportunity"
    ZERO_MANIPULATION_OPPORTUNITY = "zero_manipulation_opportunity"
    OPPORTUNITY_MISMATCH = "opportunity_mismatch"
    MISSING_MANIPULATION_APPLICATION = "missing_manipulation_application"
    MISSING_MANIPULATION_EVIDENCE = "missing_manipulation_evidence"
    MISSING_EFFECT = "missing_effect"
    MISSING_EFFECT_EVIDENCE = "missing_effect_evidence"
    MISSING_EFFECT_IDENTITY = "missing_effect_identity"
    EXTRA_DELTA = "extra_unintended_delta"
    MISSING_TWIN_LINKAGE = "missing_twin_linkage"
    TWIN_LINKAGE_UNDECLARED = "twin_linkage_undeclared"
    ARMS_NOT_DISTINCT = "arms_not_distinct"
    MISSING_VERIFIER_DIGEST = "missing_verifier_digest"
    VERIFIER_DIGEST_MISMATCH = "verifier_digest_mismatch"
    MISSING_SOURCE_DIGEST = "missing_source_digest"
    SOURCE_DIGEST_MISMATCH = "source_digest_mismatch"


C2_REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        getattr(C2RefusalReason, name)
        for name in dir(C2RefusalReason)
        if name.isupper() and isinstance(getattr(C2RefusalReason, name), str)
    }
)


class InterventionRecipe(ContractModel):
    """Immutable recipe identity for a C2 intervention.

    The recipe names the single declared delta, both arms, the manipulation-opportunity
    denominator, the counterfactual twin-linkage keys, and the pinned verifier/source
    digests (SHA-256 shaped). ``recipe_digest`` is a canonical digest over every other
    identity field and must match a recomputation (tamper detection).
    """

    schema_version: Literal["c2-intervention-recipe/v1"] = "c2-intervention-recipe/v1"
    recipe_id: str = Field(min_length=1)
    family: str = Field(min_length=1)
    construct_name: str = Field(min_length=1)
    declared_delta: str = Field(min_length=1)
    control_arm: str = Field(min_length=1)
    treatment_arm: str = Field(min_length=1)
    manipulation_opportunity_denominator: int = Field(ge=0)
    twin_linkage_keys: tuple[str, ...]
    verifier_truth_digest: str = Field(pattern=SHA256_PATTERN)
    source_digest: str = Field(pattern=SHA256_PATTERN)
    recipe_digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("twin_linkage_keys")
    @classmethod
    def canonicalize_twin_linkage_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(values)))

    @field_validator("recipe_digest")
    @classmethod
    def _digest_shape(cls, value: str) -> str:
        if not value.startswith("sha256:"):
            raise ValueError("recipe_digest must be a canonical sha256:<hex> digest")
        return value

    def identity_payload(self) -> dict[str, Any]:
        """Canonical identity body: every field except the digest itself."""
        payload = self.model_dump(mode="json")
        payload.pop("recipe_digest")
        return payload

    def expected_recipe_digest(self) -> str:
        return canonical_json_digest(self.identity_payload())


class C1MatchedInput(ContractModel):
    """Analysis-ready C1 matched input that is a candidate for C2 promotion.

    Carries the twin-linkage evidence, the declared delta, the observed
    manipulation-opportunity denominator, whether the manipulation was actually applied
    (with a non-empty evidence reference), whether an effect was observed (with a
    non-empty evidence reference and a measured effect identity/value/predicate), any
    unintended extra deltas, and the pinned verifier/source digests. ``analysis_ready``
    and ``matched`` must both be true.
    """

    trial_id: str = Field(min_length=1)
    family: str = Field(min_length=1)
    construct_name: str = Field(min_length=1)
    analysis_ready: bool
    matched: bool
    control_trial_id: str | None = None
    treatment_trial_id: str | None = None
    declared_delta: str | None = None
    manipulation_opportunity_denominator: int | None = None
    manipulation_applied: bool = False
    manipulation_evidence_ref: str = ""
    effect_observed: bool = False
    effect_evidence_ref: str = ""
    effect_identity: str = ""
    unintended_delta: tuple[str, ...] = ()
    twin_linkage_key: str | None = None
    verifier_truth_digest: str | None = None
    source_digest: str | None = None

    @field_validator("unintended_delta")
    @classmethod
    def canonicalize_unintended_delta(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(values)))

    def expected_c1_digest(self) -> str:
        """Canonical digest over the full C1 input content (evidence-identity bound)."""
        return canonical_json_digest(self.model_dump(mode="json"))


class C2PromotionDecision(ContractModel):
    """Immutable outcome of the C2 grade-promotion gate.

    ``decision`` is ``VALID`` only when every prerequisite passes; otherwise it is
    ``REFUSAL`` with the enumerated ``reasons``. ``c1_input_digest`` binds the full C1
    input (including evidence refs, measured effect, digests, denominator, twin) so any
    change to the input changes the decision identity. ``decision_id`` is the canonical
    digest over the identity body (everything except ``decision_id`` and
    ``decision_digest``); ``decision_digest`` is the canonical digest over the identity
    body plus ``decision_id``.
    """

    schema_version: Literal["c2-promotion-decision/v1"] = "c2-promotion-decision/v1"
    recipe_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    decision: Decision
    reasons: tuple[str, ...]
    recipe_digest: str = Field(pattern=SHA256_PATTERN)
    c1_input_digest: str = Field(pattern=SHA256_PATTERN)
    producer: Literal["c2-intervention-gate/v1"] = PRODUCER
    contract_digest: str = CONTRACT_DIGEST
    decision_id: str = Field(pattern=SHA256_PATTERN)
    decision_digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("reasons")
    @classmethod
    def _canonicalize_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(values) - C2_REFUSAL_REASONS)
        if unknown:
            raise ValueError(f"unknown C2 refusal reason(s): {unknown}")
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def _validate_decision_contract(self) -> C2PromotionDecision:
        if self.decision == "VALID" and self.reasons:
            raise ValueError("VALID decision cannot carry refusal reasons")
        if self.decision == "REFUSAL" and not self.reasons:
            raise ValueError("REFUSAL decision must carry at least one reason")
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload.pop("decision_id")
        payload.pop("decision_digest")
        return payload

    def expected_decision_id(self) -> str:
        """Canonical digest over the identity body (excludes decision_id/digest)."""
        return canonical_json_digest(self.identity_payload())

    def expected_decision_digest(self) -> str:
        """Canonical digest over identity body plus decision_id."""
        return canonical_json_digest(
            {**self.identity_payload(), "decision_id": self.decision_id}
        )

    @model_validator(mode="after")
    def _validate_content_identity(self) -> C2PromotionDecision:
        if self.decision_id != self.expected_decision_id():
            raise ValueError("decision_id does not match canonical content identity")
        if self.decision_digest != self.expected_decision_digest():
            raise ValueError("decision_digest does not match canonical content identity")
        return self


def _decision_payload(
    *,
    recipe_id: str,
    trial_id: str,
    decision: Decision,
    reasons: tuple[str, ...],
    recipe_digest: str,
    c1_input_digest: str,
) -> dict[str, Any]:
    """Build the full immutable decision payload with canonical content identity."""
    body = {
        "schema_version": "c2-promotion-decision/v1",
        "recipe_id": recipe_id,
        "trial_id": trial_id,
        "decision": decision,
        "reasons": reasons,
        "recipe_digest": recipe_digest,
        "c1_input_digest": c1_input_digest,
        "producer": PRODUCER,
        "contract_digest": CONTRACT_DIGEST,
    }
    decision_id = canonical_json_digest(body)
    decision_digest = canonical_json_digest({**body, "decision_id": decision_id})
    return {**body, "decision_id": decision_id, "decision_digest": decision_digest}


def _valid_decision(
    recipe: InterventionRecipe, c1_input: C1MatchedInput
) -> C2PromotionDecision:
    return C2PromotionDecision(
        **_decision_payload(
            recipe_id=recipe.recipe_id,
            trial_id=c1_input.trial_id,
            decision="VALID",
            reasons=(),
            recipe_digest=recipe.recipe_digest,
            c1_input_digest=c1_input.expected_c1_digest(),
        )
    )


def _refusal_decision(
    recipe: InterventionRecipe | None,
    c1_input: C1MatchedInput,
    reasons: list[str],
) -> C2PromotionDecision:
    recipe_id = recipe.recipe_id if recipe is not None else "unknown-recipe"
    if not recipe_id.strip():
        recipe_id = "unknown-recipe"
    return C2PromotionDecision(
        **_decision_payload(
            recipe_id=recipe_id,
            trial_id=c1_input.trial_id,
            decision="REFUSAL",
            reasons=tuple(sorted(set(reasons))),
            recipe_digest=(
                recipe.recipe_digest if recipe is not None else "sha256:" + "0" * 64
            ),
            c1_input_digest=c1_input.expected_c1_digest(),
        )
    )


def _matches_recipe(c1_input: C1MatchedInput, recipe: InterventionRecipe) -> list[str]:
    """Cross-check C1 input against the recipe; every mismatch is a distinct reason."""
    reasons: list[str] = []
    if c1_input.family != recipe.family:
        reasons.append(C2RefusalReason.FAMILY_MISMATCH)
    if c1_input.construct_name != recipe.construct_name:
        reasons.append(C2RefusalReason.CONSTRUCT_MISMATCH)
    if c1_input.declared_delta != recipe.declared_delta:
        reasons.append(C2RefusalReason.DELTA_MISMATCH)
    if c1_input.verifier_truth_digest != recipe.verifier_truth_digest:
        reasons.append(C2RefusalReason.VERIFIER_DIGEST_MISMATCH)
    if c1_input.source_digest != recipe.source_digest:
        reasons.append(C2RefusalReason.SOURCE_DIGEST_MISMATCH)
    if c1_input.manipulation_opportunity_denominator != recipe.manipulation_opportunity_denominator:
        reasons.append(C2RefusalReason.OPPORTUNITY_MISMATCH)
    if (
        c1_input.twin_linkage_key is not None
        and c1_input.twin_linkage_key not in recipe.twin_linkage_keys
    ):
        reasons.append(C2RefusalReason.TWIN_LINKAGE_UNDECLARED)
    return reasons


def evaluate_c2_promotion(
    recipe: InterventionRecipe | None,
    c1_input: C1MatchedInput,
) -> C2PromotionDecision:
    """Evaluate immutable C2 grade-promotion inputs; fail-closed.

    Returns VALID only when every prerequisite is proven and the C1 input matches the
    recipe, otherwise REFUSAL with the enumerated reasons. Matched-only C1 input (no
    intervention recipe) always refuses with ``C1_ONLY``; no analysis is ever relabeled
    C2 on inference.
    """

    reasons: list[str] = []

    # 1. Analysis-ready C1 matched input.
    if not c1_input.analysis_ready:
        reasons.append(C2RefusalReason.C1_NOT_ANALYSIS_READY)
    if not c1_input.matched:
        reasons.append(C2RefusalReason.C1_NOT_MATCHED)

    # 2. Immutable intervention/recipe identity and digest.
    if recipe is None:
        reasons.append(C2RefusalReason.C1_ONLY)
    else:
        if not recipe.recipe_id or not recipe.family or not recipe.construct_name:
            reasons.append(C2RefusalReason.MISSING_RECIPE_IDENTITY)
        if recipe.expected_recipe_digest() != recipe.recipe_digest:
            reasons.append(C2RefusalReason.TAMPERED_RECIPE_DIGEST)
        if not recipe.declared_delta or recipe.control_arm == recipe.treatment_arm:
            reasons.append(C2RefusalReason.MISSING_DECLARED_DELTA)

    # 3. C1 input must match the recipe on every cross-checked dimension.
    if recipe is not None:
        reasons.extend(_matches_recipe(c1_input, recipe))

    # 4. Manipulation-opportunity denominator (observed on the C1 input; never inferred
    #    from the recipe declaration).
    denom = c1_input.manipulation_opportunity_denominator
    if denom is None:
        reasons.append(C2RefusalReason.MISSING_MANIPULATION_OPPORTUNITY)
    elif denom <= 0:
        reasons.append(C2RefusalReason.ZERO_MANIPULATION_OPPORTUNITY)

    # 5. Observed manipulation application and effect, each with evidence identity.
    if not c1_input.manipulation_applied:
        reasons.append(C2RefusalReason.MISSING_MANIPULATION_APPLICATION)
    if not c1_input.manipulation_evidence_ref:
        reasons.append(C2RefusalReason.MISSING_MANIPULATION_EVIDENCE)
    if not c1_input.effect_observed:
        reasons.append(C2RefusalReason.MISSING_EFFECT)
    if not c1_input.effect_evidence_ref:
        reasons.append(C2RefusalReason.MISSING_EFFECT_EVIDENCE)
    if not c1_input.effect_identity:
        reasons.append(C2RefusalReason.MISSING_EFFECT_IDENTITY)

    # 6. No unintended delta beyond the declared one.
    if c1_input.unintended_delta:
        reasons.append(C2RefusalReason.EXTRA_DELTA)

    # 7. Counterfactual/twin linkage and distinct arms.
    if not c1_input.twin_linkage_key:
        reasons.append(C2RefusalReason.MISSING_TWIN_LINKAGE)
    elif recipe is not None and c1_input.twin_linkage_key not in recipe.twin_linkage_keys:
        reasons.append(C2RefusalReason.TWIN_LINKAGE_UNDECLARED)
    if not c1_input.control_trial_id or not c1_input.treatment_trial_id:
        reasons.append(C2RefusalReason.MISSING_TWIN_LINKAGE)
    elif c1_input.control_trial_id == c1_input.treatment_trial_id:
        reasons.append(C2RefusalReason.ARMS_NOT_DISTINCT)

    # 8. Verifier and source digests present and SHA-256 shaped.
    if not c1_input.verifier_truth_digest:
        reasons.append(C2RefusalReason.MISSING_VERIFIER_DIGEST)
    if not c1_input.source_digest:
        reasons.append(C2RefusalReason.MISSING_SOURCE_DIGEST)

    if reasons:
        return _refusal_decision(recipe, c1_input, reasons)
    # No reasons implies the recipe was present (absence appends C1_ONLY above).
    assert recipe is not None
    return _valid_decision(recipe, c1_input)


# --------------------------------------------------------------------------- #
# Bundled reproducible synthetic Action64 handle-representation intervention.
# --------------------------------------------------------------------------- #
# The Action64 mechanism (action-memory-v1 dose ladder) exposes context chunks under
# opaque non-semantic handles (``ctx_<hex>``) retrieved one at a time. The C2
# intervention holds content/order/seed fixed and changes ONLY the handle
# representation/issuance: opaque-ID retrieval (control) vs indexed/range/batch
# retrieval (treatment). The synthetic twin below proves recipe identity, a single
# declared delta, a proven manipulation opportunity denominator (the declared read
# opportunity count), observed manipulation application/effect with evidence refs, twin
# linkage, and verifier/source digests — all without a paid run.
# --------------------------------------------------------------------------- #

SYNTHETIC_ACTION64_RECIPE_ID = "am-handle-representation-v1"
SYNTHETIC_ACTION64_FAMILY = "action-memory-v1"
SYNTHETIC_ACTION64_CONSTRUCT = "actionable_entity_memory_and_value_binding"
SYNTHETIC_ACTION64_DELTA = "handle_representation"
SYNTHETIC_ACTION64_CONTROL_ARM = "opaque_id_retrieval"
SYNTHETIC_ACTION64_TREATMENT_ARM = "indexed_range_batch_retrieval"
SYNTHETIC_ACTION64_OPPORTUNITY = 257
SYNTHETIC_ACTION64_TWIN_KEYS = ("content", "order", "seed")
SYNTHETIC_ACTION64_TWIN_KEY = "am-dose-ladder-v1-s1337-d65536"
SYNTHETIC_ACTION64_VERIFIER_DIGEST = "sha256:" + "a" * 64
SYNTHETIC_ACTION64_SOURCE_DIGEST = "sha256:" + "b" * 64


def synthetic_action64_recipe() -> InterventionRecipe:
    """Return the immutable synthetic Action64 handle-representation recipe."""
    provisional = InterventionRecipe(
        recipe_id=SYNTHETIC_ACTION64_RECIPE_ID,
        family=SYNTHETIC_ACTION64_FAMILY,
        construct_name=SYNTHETIC_ACTION64_CONSTRUCT,
        declared_delta=SYNTHETIC_ACTION64_DELTA,
        control_arm=SYNTHETIC_ACTION64_CONTROL_ARM,
        treatment_arm=SYNTHETIC_ACTION64_TREATMENT_ARM,
        manipulation_opportunity_denominator=SYNTHETIC_ACTION64_OPPORTUNITY,
        twin_linkage_keys=SYNTHETIC_ACTION64_TWIN_KEYS + (SYNTHETIC_ACTION64_TWIN_KEY,),
        verifier_truth_digest=SYNTHETIC_ACTION64_VERIFIER_DIGEST,
        source_digest=SYNTHETIC_ACTION64_SOURCE_DIGEST,
        recipe_digest="sha256:" + "0" * 64,
    )
    computed = provisional.expected_recipe_digest()
    return InterventionRecipe(
        recipe_id=SYNTHETIC_ACTION64_RECIPE_ID,
        family=SYNTHETIC_ACTION64_FAMILY,
        construct_name=SYNTHETIC_ACTION64_CONSTRUCT,
        declared_delta=SYNTHETIC_ACTION64_DELTA,
        control_arm=SYNTHETIC_ACTION64_CONTROL_ARM,
        treatment_arm=SYNTHETIC_ACTION64_TREATMENT_ARM,
        manipulation_opportunity_denominator=SYNTHETIC_ACTION64_OPPORTUNITY,
        twin_linkage_keys=SYNTHETIC_ACTION64_TWIN_KEYS + (SYNTHETIC_ACTION64_TWIN_KEY,),
        verifier_truth_digest=SYNTHETIC_ACTION64_VERIFIER_DIGEST,
        source_digest=SYNTHETIC_ACTION64_SOURCE_DIGEST,
        recipe_digest=computed,
    )


def synthetic_action64_c1_input() -> C1MatchedInput:
    """Return a proven analysis-ready C1 matched input for the synthetic intervention."""
    return C1MatchedInput(
        trial_id="am-dl-semantic-distractor-65536-s1337",
        family="action-memory-v1",
        construct_name="actionable_entity_memory_and_value_binding",
        analysis_ready=True,
        matched=True,
        control_trial_id="am-dl-opaque-id-65536-s1337-control",
        treatment_trial_id="am-dl-indexed-range-65536-s1337-treatment",
        declared_delta="handle_representation",
        manipulation_opportunity_denominator=257,
        manipulation_applied=True,
        manipulation_evidence_ref="evidence/manipulation/indexed-batch-applied.json",
        effect_observed=True,
        effect_evidence_ref="evidence/effect/indexed-batch-task-success.json",
        effect_identity="treatment_task_success=true;control_task_success=false",
        unintended_delta=(),
        twin_linkage_key=SYNTHETIC_ACTION64_TWIN_KEY,
        verifier_truth_digest="sha256:" + "a" * 64,
        source_digest="sha256:" + "b" * 64,
    )


# --------------------------------------------------------------------------- #
# Consumer seam: deterministic module runner (mirrors trajectory_recipe_run).
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.interpretation.c2_intervention_gate",
        description="Evaluate C1 -> C2 grade promotion against an intervention recipe.",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--synthetic", action="store_true", help="Use bundled synthetic Action64 intervention")
    src.add_argument("--recipe", type=Path, help="Path to InterventionRecipe JSON")
    src.add_argument("--c1-input", type=Path, help="Path to C1MatchedInput JSON")
    parser.add_argument("--json", action="store_true", help="Emit decision as JSON")
    return parser


def _load_model[ModelT: ContractModel](
    path: Path, model: type[ModelT], label: str
) -> ModelT:
    if not path.is_file():
        raise ValueError(f"{label} file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {label} file {path}: {exc}") from exc
    return model.model_validate(payload)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.synthetic:
            recipe = synthetic_action64_recipe()
            c1_input = synthetic_action64_c1_input()
        else:
            if args.recipe is None or args.c1_input is None:
                print(
                    "error: --recipe and --c1-input are both required unless --synthetic",
                    file=sys.stderr,
                )
                return 1
            recipe = _load_model(args.recipe, InterventionRecipe, "recipe")
            c1_input = _load_model(args.c1_input, C1MatchedInput, "C1 input")
        decision = evaluate_c2_promotion(recipe, c1_input)
        if args.json:
            print(json.dumps(decision.model_dump(mode="json"), indent=2, sort_keys=True))
        else:
            print(
                f"{decision.decision} recipe={decision.recipe_id} trial={decision.trial_id} "
                f"reasons={','.join(decision.reasons) or '-'}"
            )
        return 0
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
