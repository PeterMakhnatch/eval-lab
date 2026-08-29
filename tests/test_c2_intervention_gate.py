"""Focused contract tests for the fail-closed C2 intervention grade-promotion gate.

Covers the negative controls (matched-only C1 cannot become C2, missing/tampered recipe
refuses, input-vs-recipe mismatch refuses, fabricated/missing evidence refuses, no-op
and extra-delta refuse, zero/missing opportunity refuses, missing prerequisites refuse)
and the positive control (a valid synthetic Action64 handle-representation intervention
reaches C2). Also verifies that the decision identity binds the C1 input digest and that
decision_id / decision_digest are split and independently tamper-proof.
"""

from __future__ import annotations

import json

import pytest

from evallab.interpretation.c2_intervention_gate import (
    CONTRACT_DIGEST,
    PRODUCER,
    C1MatchedInput,
    C2PromotionDecision,
    C2RefusalReason,
    InterventionRecipe,
    evaluate_c2_promotion,
    main,
    synthetic_action64_c1_input,
    synthetic_action64_recipe,
)


def _digest(hex_: str) -> str:
    return "sha256:" + hex_


def _valid_c1() -> C1MatchedInput:
    return synthetic_action64_c1_input()


def _valid_recipe() -> InterventionRecipe:
    return synthetic_action64_recipe()


def test_synthetic_recipe_identity_is_immutable_and_self_consistent():
    recipe = _valid_recipe()
    assert recipe.recipe_id == "am-handle-representation-v1"
    assert recipe.declared_delta == "handle_representation"
    assert recipe.control_arm != recipe.treatment_arm
    assert "am-dose-ladder-v1-s1337-d65536" in recipe.twin_linkage_keys
    # verifier/source digests are SHA-256 shaped
    assert recipe.verifier_truth_digest.startswith("sha256:")
    assert recipe.source_digest.startswith("sha256:")
    # digest must be the canonical digest over the identity payload (tamper-proof).
    assert recipe.recipe_digest == recipe.expected_recipe_digest()
    # any identity change invalidates the digest
    changed = recipe.model_copy(update={"treatment_arm": "batch_retrieval_v2"})
    assert changed.expected_recipe_digest() != recipe.recipe_digest


def test_valid_synthetic_intervention_reaches_c2():
    decision = evaluate_c2_promotion(_valid_recipe(), _valid_c1())
    assert decision.decision == "VALID"
    assert decision.reasons == ()
    assert decision.recipe_id == "am-handle-representation-v1"
    assert decision.producer == PRODUCER
    assert decision.contract_digest == CONTRACT_DIGEST
    assert decision.recipe_digest == _valid_recipe().recipe_digest
    # c1 input digest is bound into the decision and equals the C1 content digest
    assert decision.c1_input_digest == _valid_c1().expected_c1_digest()


def test_matched_only_c1_cannot_become_c2():
    """A C1 matched pair with no intervention recipe must refuse as grade inflation."""
    decision = evaluate_c2_promotion(None, _valid_c1())
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.C1_ONLY in decision.reasons


def test_missing_recipe_identity_refuses():
    decision = evaluate_c2_promotion(
        _valid_recipe().model_copy(update={"recipe_id": ""}), _valid_c1()
    )
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.MISSING_RECIPE_IDENTITY in decision.reasons


def test_tampered_recipe_digest_refuses():
    recipe = _valid_recipe().model_copy(update={"recipe_digest": _digest("c" * 64)})
    decision = evaluate_c2_promotion(recipe, _valid_c1())
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.TAMPERED_RECIPE_DIGEST in decision.reasons


def test_missing_declared_delta_refuses():
    recipe = _valid_recipe().model_copy(update={"declared_delta": ""})
    decision = evaluate_c2_promotion(recipe, _valid_c1())
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.MISSING_DECLARED_DELTA in decision.reasons


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("family", "other-family", C2RefusalReason.FAMILY_MISMATCH),
        ("construct_name", "other-construct", C2RefusalReason.CONSTRUCT_MISMATCH),
        ("declared_delta", "other_delta", C2RefusalReason.DELTA_MISMATCH),
        ("verifier_truth_digest", _digest("d" * 64), C2RefusalReason.VERIFIER_DIGEST_MISMATCH),
        ("source_digest", _digest("e" * 64), C2RefusalReason.SOURCE_DIGEST_MISMATCH),
        ("manipulation_opportunity_denominator", 100, C2RefusalReason.OPPORTUNITY_MISMATCH),
        ("twin_linkage_key", "some-other-twin", C2RefusalReason.TWIN_LINKAGE_UNDECLARED),
    ],
)
def test_c1_input_mismatch_with_recipe_refuses(field, value, expected):
    """Every C1-vs-recipe mismatch must refuse with its distinct reason."""
    c1 = _valid_c1().model_copy(update={field: value})
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert expected in decision.reasons


def test_arms_must_be_distinct():
    c1 = _valid_c1().model_copy(update={"treatment_trial_id": _valid_c1().control_trial_id})
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.ARMS_NOT_DISTINCT in decision.reasons


def test_zero_manipulation_opportunity_refuses():
    c1 = _valid_c1().model_copy(update={"manipulation_opportunity_denominator": 0})
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.ZERO_MANIPULATION_OPPORTUNITY in decision.reasons


def test_missing_manipulation_opportunity_refuses():
    c1 = _valid_c1().model_copy(update={"manipulation_opportunity_denominator": None})
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.MISSING_MANIPULATION_OPPORTUNITY in decision.reasons


def test_noop_intervention_refuses():
    """No-op: manipulation declared but never applied and no effect observed."""
    c1 = _valid_c1().model_copy(
        update={"manipulation_applied": False, "effect_observed": False}
    )
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.MISSING_MANIPULATION_APPLICATION in decision.reasons
    assert C2RefusalReason.MISSING_EFFECT in decision.reasons


@pytest.mark.parametrize(
    "field,expected",
    [
        ("manipulation_evidence_ref", C2RefusalReason.MISSING_MANIPULATION_EVIDENCE),
        ("effect_evidence_ref", C2RefusalReason.MISSING_EFFECT_EVIDENCE),
        ("effect_identity", C2RefusalReason.MISSING_EFFECT_IDENTITY),
    ],
)
def test_missing_evidence_identity_refuses(field, expected):
    """Effect/manipulation claims without a non-empty evidence identity must refuse."""
    c1 = _valid_c1().model_copy(update={field: ""})
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert expected in decision.reasons


def test_extra_unintended_delta_refuses():
    """Extra delta beyond the declared single factor must refuse."""
    c1 = _valid_c1().model_copy(update={"unintended_delta": ("scaffold_version",)})
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.EXTRA_DELTA in decision.reasons


def test_not_analysis_ready_c1_refuses():
    c1 = _valid_c1().model_copy(update={"analysis_ready": False})
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.C1_NOT_ANALYSIS_READY in decision.reasons


def test_unmatched_c1_refuses():
    c1 = _valid_c1().model_copy(update={"matched": False})
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.C1_NOT_MATCHED in decision.reasons


def test_missing_twin_linkage_refuses():
    c1 = _valid_c1().model_copy(
        update={"twin_linkage_key": None, "control_trial_id": None}
    )
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.MISSING_TWIN_LINKAGE in decision.reasons


def test_missing_verifier_and_source_digests_refuse():
    c1 = _valid_c1().model_copy(update={"verifier_truth_digest": None, "source_digest": None})
    decision = evaluate_c2_promotion(_valid_recipe(), c1)
    assert decision.decision == "REFUSAL"
    assert C2RefusalReason.MISSING_VERIFIER_DIGEST in decision.reasons
    assert C2RefusalReason.MISSING_SOURCE_DIGEST in decision.reasons


def test_refusal_lists_every_missing_prerequisite():
    """A maximally-broken input must enumerate every applicable reason, no subset hiding."""
    empty_recipe = _valid_recipe().model_copy(
        update={
            "recipe_id": "",
            "family": "",
            "construct_name": "",
            "declared_delta": "",
            "control_arm": "a",
            "treatment_arm": "a",
            "manipulation_opportunity_denominator": 0,
            "twin_linkage_keys": (),
            "verifier_truth_digest": _digest("0" * 64),
            "source_digest": _digest("0" * 64),
            "recipe_digest": _digest("0" * 64),
        }
    )
    broken_c1 = C1MatchedInput(
        trial_id="t",
        family="other-family",
        construct_name="other",
        analysis_ready=False,
        matched=False,
        control_trial_id=None,
        treatment_trial_id=None,
        declared_delta="different",
        manipulation_opportunity_denominator=None,
        manipulation_applied=False,
        manipulation_evidence_ref="",
        effect_observed=False,
        effect_evidence_ref="",
        effect_identity="",
        unintended_delta=("scaffold_version",),
        twin_linkage_key=None,
        verifier_truth_digest=None,
        source_digest=None,
    )
    decision = evaluate_c2_promotion(empty_recipe, broken_c1)
    assert decision.decision == "REFUSAL"
    reasons = set(decision.reasons)
    assert C2RefusalReason.C1_NOT_ANALYSIS_READY in reasons
    assert C2RefusalReason.C1_NOT_MATCHED in reasons
    assert C2RefusalReason.MISSING_RECIPE_IDENTITY in reasons
    assert C2RefusalReason.TAMPERED_RECIPE_DIGEST in reasons
    assert C2RefusalReason.MISSING_DECLARED_DELTA in reasons
    assert C2RefusalReason.FAMILY_MISMATCH in reasons
    assert C2RefusalReason.CONSTRUCT_MISMATCH in reasons
    assert C2RefusalReason.DELTA_MISMATCH in reasons
    assert C2RefusalReason.VERIFIER_DIGEST_MISMATCH in reasons
    assert C2RefusalReason.SOURCE_DIGEST_MISMATCH in reasons
    assert C2RefusalReason.OPPORTUNITY_MISMATCH in reasons
    assert C2RefusalReason.MISSING_MANIPULATION_OPPORTUNITY in reasons
    assert C2RefusalReason.MISSING_MANIPULATION_APPLICATION in reasons
    assert C2RefusalReason.MISSING_MANIPULATION_EVIDENCE in reasons
    assert C2RefusalReason.MISSING_EFFECT in reasons
    assert C2RefusalReason.MISSING_EFFECT_EVIDENCE in reasons
    assert C2RefusalReason.MISSING_EFFECT_IDENTITY in reasons
    assert C2RefusalReason.EXTRA_DELTA in reasons
    assert C2RefusalReason.MISSING_TWIN_LINKAGE in reasons
    assert C2RefusalReason.MISSING_VERIFIER_DIGEST in reasons
    assert C2RefusalReason.MISSING_SOURCE_DIGEST in reasons


def test_decision_is_deterministic_and_binds_c1_input_digest():
    d1 = evaluate_c2_promotion(_valid_recipe(), _valid_c1())
    d2 = evaluate_c2_promotion(_valid_recipe(), _valid_c1())
    assert d1.decision_digest == d2.decision_digest
    assert d1.model_dump(mode="json") == d2.model_dump(mode="json")
    assert d1.c1_input_digest == _valid_c1().expected_c1_digest()
    # round-trips through model_validate
    rebuilt = C2PromotionDecision.model_validate(d1.model_dump(mode="json"))
    assert rebuilt == d1


def test_mutated_c1_input_changes_decision_identity():
    """Any mutation of the C1 input must change the decision id and digest."""
    d1 = evaluate_c2_promotion(_valid_recipe(), _valid_c1())
    d2 = evaluate_c2_promotion(
        _valid_recipe(), _valid_c1().model_copy(update={"effect_identity": "mutated"})
    )
    assert d1.c1_input_digest != d2.c1_input_digest
    assert d1.decision_id != d2.decision_id
    assert d1.decision_digest != d2.decision_digest


def test_decision_id_and_digest_are_split_and_independently_tamperable():
    decision = evaluate_c2_promotion(_valid_recipe(), _valid_c1())
    # decision_id is the digest over the identity body (excludes decision_id/digest)
    assert decision.decision_id == decision.expected_decision_id()
    # decision_digest is the digest over the identity body plus decision_id
    assert decision.decision_digest == decision.expected_decision_digest()
    # tampering decision_id fails identity
    with pytest.raises(ValueError, match="decision_id does not match"):
        C2PromotionDecision.model_validate(
            {**decision.model_dump(mode="json"), "decision_id": _digest("f" * 64)}
        )
    # tampering decision_digest fails identity
    with pytest.raises(ValueError, match="decision_digest does not match"):
        C2PromotionDecision.model_validate(
            {**decision.model_dump(mode="json"), "decision_digest": _digest("e" * 64)}
        )
    # tampering a content field (reasons) fails identity
    with pytest.raises(ValueError, match="decision_id does not match"):
        C2PromotionDecision.model_validate(
            {
                **decision.model_dump(mode="json"),
                "reasons": (C2RefusalReason.C1_ONLY,),
                "decision": "REFUSAL",
            }
        )


def test_decision_contract_rejects_valid_with_reasons():
    with pytest.raises(ValueError, match="VALID decision cannot carry refusal reasons"):
        C2PromotionDecision.model_validate(
            {
                "schema_version": "c2-promotion-decision/v1",
                "recipe_id": "r",
                "trial_id": "t",
                "decision": "VALID",
                "reasons": ("c1_matched_only_no_intervention",),
                "recipe_digest": _digest("a" * 64),
                "c1_input_digest": _digest("b" * 64),
                "producer": PRODUCER,
                "contract_digest": CONTRACT_DIGEST,
                "decision_id": _digest("0" * 64),
                "decision_digest": _digest("0" * 64),
            }
        )


def test_module_runner_smoke_emits_reproducible_c2_result(capsys):
    """Consumer seam: python -m ... --synthetic emits a deterministic VALID C2 decision."""
    rc = main(["--synthetic", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["decision"] == "VALID"
    assert payload["recipe_id"] == "am-handle-representation-v1"
    # deterministic across invocations
    rc2 = main(["--synthetic", "--json"])
    captured2 = capsys.readouterr()
    assert rc2 == 0
    assert json.loads(captured2.out)["decision_digest"] == payload["decision_digest"]


def test_module_runner_rejects_missing_inputs(capsys):
    rc = main(["--recipe", "/nonexistent/recipe.json"])
    assert rc == 1
    assert "both required" in capsys.readouterr().err
