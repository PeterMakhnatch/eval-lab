from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from evallab.trajectory_acceptance import (
    DETERMINISTIC_GATE_ORDER,
    AcceptanceDecision,
    CalibrationClassGate,
    CrossJudgeRecord,
    GateResult,
    evaluate_acceptance,
)

D = {char: "sha256:" + char * 64 for char in "123456789abcdef"}
NOW = datetime(2026, 8, 26, tzinfo=UTC)


def class_gate() -> CalibrationClassGate:
    return CalibrationClassGate(
        class_id="infrastructure_failure",
        calibration_version=D["1"],
        report_digest=D["2"],
        report_schema="calibration-report-v1",
        thresholds_digest=D["3"],
        acceptance_enabling_allowed=False,
        acceptance_enabled=False,
        hold_reasons=["acceptance_enabling_disabled"],
        reliability_snapshot={},
    )


def cross_judge(agreement: str = "exact") -> CrossJudgeRecord:
    return CrossJudgeRecord.model_validate(
        {
            "required": True,
            "judge_families": ["gemini", "grok"],
            "class_ids": ["infrastructure_failure", "infrastructure_failure"],
            "agreement": agreement,
        }
    )


def gate(
    gate_id: str = "C1_resolve",
    status: str = "pass",
    reason_code: str | None = None,
) -> GateResult:
    return GateResult.model_validate(
        {
            "gate_id": gate_id,
            "status": status,
            "reason_code": reason_code,
            "citation_ids": [D["4"]],
        }
    )

def passing_gates() -> list[GateResult]:
    return [gate(gate_id) for gate_id in DETERMINISTIC_GATE_ORDER]


def gates_with(status: str, reason_code: str) -> list[GateResult]:
    gates = passing_gates()
    gates[0] = gate(DETERMINISTIC_GATE_ORDER[0], status, reason_code)
    return gates



def evaluate(
    gates: list[GateResult] | None = None,
    cross: CrossJudgeRecord | None = None,
    produced_at: datetime = NOW,
) -> AcceptanceDecision:
    return evaluate_acceptance(
        judgment_ids=[D["5"], D["6"]],
        pack_digest=D["7"],
        deterministic_gates=passing_gates() if gates is None else gates,
        cross_judge=cross or cross_judge(),
        calibration_class_gate=class_gate(),
        policy_digest=D["8"],
        proposed_next_check="collect held-out calibration evidence",
        produced_at=produced_at,
    )


def test_all_gates_pass_but_disabled_class_abstains() -> None:
    decision = evaluate()
    assert decision.decision == "abstained"
    assert "acceptance_enabling_disabled" in decision.reason_codes
    assert "class_not_enabled" in decision.reason_codes


def test_calibration_gate_is_fail_closed_for_both_report_versions() -> None:
    payload = class_gate().model_dump(mode="json")
    payload["report_schema"] = "calibration-report-v1.1"
    assert CalibrationClassGate.model_validate(payload).acceptance_enabled is False
    payload["acceptance_enabled"] = True
    with pytest.raises(ValidationError):
        CalibrationClassGate.model_validate(payload)


@pytest.mark.parametrize(
    "reason",
    [
        "digest_mismatch",
        "source_digest_mismatch",
        "quarantined_input",
        "contradicts_verifier_or_state",
        "schema_invalid",
        "citation_unresolved",
    ],
)
def test_integrity_or_contradiction_failure_rejects(reason: str) -> None:
    decision = evaluate(gates_with("fail", reason))
    assert decision.decision == "rejected"
    assert reason in decision.reason_codes


def test_unknown_or_nonfatal_gate_abstains() -> None:
    unknown = evaluate(gates_with("unknown", "source_missing"))
    underpowered = evaluate(gates_with("fail", "calibration_underpowered"))
    assert unknown.decision == underpowered.decision == "abstained"


def test_cross_judge_disagreement_abstains() -> None:
    decision = evaluate(cross=cross_judge("disagree"))
    assert decision.decision == "abstained"
    assert "cross_judge_disagree" in decision.reason_codes


def test_accepted_is_runtime_invalid_while_v1_disabled() -> None:
    payload = evaluate().model_dump(mode="json")
    payload["decision"] = "accepted"
    with pytest.raises(ValidationError, match="acceptance is disabled"):
        AcceptanceDecision.model_validate(payload)


def test_gate_order_and_publication_time_do_not_change_identity() -> None:
    reversed_gates = list(reversed(passing_gates()))
    first = evaluate(reversed_gates, produced_at=NOW)
    second = evaluate(
        passing_gates(),
        produced_at=NOW + timedelta(days=1),
    )
    assert first.decision_id == second.decision_id
    assert first.decision_digest == second.decision_digest


def test_duplicate_gate_ids_are_rejected() -> None:
    gates = passing_gates()
    with pytest.raises(ValueError, match="must be unique"):
        evaluate([*gates, gates[0]])


def test_missing_frozen_gate_is_rejected() -> None:
    with pytest.raises(ValueError, match="must match frozen set"):
        evaluate(passing_gates()[:-1])


def test_json_schema_matches_frozen_acceptance_surface() -> None:
    schema = AcceptanceDecision.model_json_schema()
    expected = {
        "schema_version",
        "decision_id",
        "decision_digest",
        "decision",
        "judgment_ids",
        "pack_digest",
        "deterministic_gates",
        "cross_judge",
        "calibration_version",
        "calibration_class_gate",
        "reason_codes",
        "proposed_next_check",
        "policy_digest",
        "supersedes_decision_id",
        "produced_at",
    }
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == expected
    assert set(schema["required"]) == expected
    assert schema["properties"]["decision"]["enum"] == [
        "accepted",
        "rejected",
        "abstained",
    ]
