"""Platform-owned AcceptanceDecision v1 contract and pure fail-closed gate.

Normative source: PR #189 ``automated-trajectory-interpretation-v1.schema.json``.
Both frozen Track B CalibrationReport versions disable every class, so this runtime cannot accept.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from evallab.schemas import ContractModel
from evallab.trajectory_judgment import SHA256_PATTERN, Digest, canonical_json_digest

AUTO_ACCEPTANCE_ENABLED = False
DETERMINISTIC_GATE_ORDER = (
    "C1_resolve",
    "C2_digest",
    "C3_source",
    "C4_window",
    "C5_entail",
    "C6_no_future",
    "C7_actor_cone",
    "C8_search_before_absence",
    "C9_verifier_priority",
    "C10_omitted",
    "schema_valid",
    "pack_complete",
    "not_quarantined",
    "not_hold_gold",
)
_GATE_ORDER_INDEX = {
    gate_id: index for index, gate_id in enumerate(DETERMINISTIC_GATE_ORDER)
}


def _gate_sort_key(gate: GateResult) -> tuple[int, str]:
    return (_GATE_ORDER_INDEX.get(gate.gate_id, len(DETERMINISTIC_GATE_ORDER)), gate.gate_id)

_FATAL_REASON_CODES = frozenset(
    {
        "digest_mismatch",
        "source_digest_mismatch",
        "quarantined_input",
        "contradicts_verifier_or_state",
        "schema_invalid",
        "citation_unresolved",
    }
)


class GateResult(ContractModel):
    gate_id: str
    status: Literal["pass", "fail", "unknown"]
    reason_code: str | None
    citation_ids: list[Digest]

    @field_validator("citation_ids")
    @classmethod
    def validate_citation_ids(cls, values: list[str]) -> list[str]:
        if values != sorted(set(values)):
            raise ValueError("citation IDs must be unique and deterministically ordered")
        for value in values:
            if re.fullmatch(SHA256_PATTERN, value) is None:
                raise ValueError("citation IDs must be canonical sha256 digests")
        return values


class CrossJudgeRecord(ContractModel):
    required: bool
    judge_families: list[str]
    class_ids: list[str | None]
    agreement: Literal["exact", "disagree", "not_required", "unavailable"]


class CalibrationClassGate(ContractModel):
    class_id: str
    calibration_version: str = Field(pattern=SHA256_PATTERN)
    report_digest: str = Field(pattern=SHA256_PATTERN)
    report_schema: Literal["calibration-report-v1", "calibration-report-v1.1"]
    thresholds_digest: str = Field(pattern=SHA256_PATTERN)
    acceptance_enabling_allowed: Literal[False]
    acceptance_enabled: Literal[False]
    hold_reasons: list[str] = Field(min_length=1)
    reliability_snapshot: dict[str, Any]


class AcceptanceDecision(ContractModel):
    """Immutable outcome of the Platform pure acceptance gate."""

    schema_version: Literal["acceptance-decision/v1"]
    decision_id: str = Field(pattern=SHA256_PATTERN)
    decision_digest: str = Field(pattern=SHA256_PATTERN)
    decision: Literal["accepted", "rejected", "abstained"]
    judgment_ids: list[Digest] = Field(min_length=1)
    pack_digest: str = Field(pattern=SHA256_PATTERN)
    deterministic_gates: list[GateResult] = Field(min_length=1)
    cross_judge: CrossJudgeRecord
    calibration_version: str | None
    calibration_class_gate: CalibrationClassGate
    reason_codes: list[str] = Field(min_length=1)
    proposed_next_check: str | None
    policy_digest: str = Field(pattern=SHA256_PATTERN)
    supersedes_decision_id: str | None = Field(pattern=SHA256_PATTERN)
    produced_at: datetime

    @field_validator("judgment_ids")
    @classmethod
    def validate_judgment_ids(cls, values: list[str]) -> list[str]:
        if values != sorted(set(values)):
            raise ValueError("judgment IDs must be unique and deterministically ordered")
        for value in values:
            if re.fullmatch(SHA256_PATTERN, value) is None:
                raise ValueError("judgment IDs must be canonical sha256 digests")
        return values

    @field_validator("deterministic_gates")
    @classmethod
    def validate_gate_order(cls, values: list[GateResult]) -> list[GateResult]:
        gate_ids = [value.gate_id for value in values]
        expected = [value.gate_id for value in sorted(values, key=_gate_sort_key)]
        if gate_ids != expected or len(set(gate_ids)) != len(gate_ids):
            raise ValueError("gate IDs must be unique and deterministically ordered")
        return values


    @model_validator(mode="after")
    def validate_decision_invariants(self) -> AcceptanceDecision:
        if self.decision == "accepted":
            if not AUTO_ACCEPTANCE_ENABLED:
                raise ValueError("automatic acceptance is disabled for v1")
            if any(gate.status != "pass" for gate in self.deterministic_gates):
                raise ValueError("accepted decision requires every deterministic gate")
            if self.cross_judge.required and self.cross_judge.agreement != "exact":
                raise ValueError("accepted decision requires exact judge agreement")
            if not (
                self.calibration_class_gate.acceptance_enabling_allowed
                and self.calibration_class_gate.acceptance_enabled
            ):
                raise ValueError("accepted decision requires an enabled class gate")
        return self

    def identity_payload(self) -> dict[str, Any]:
        """Canonical identity body, excluding publication time and its own digest."""
        payload = self.model_dump(mode="json")
        payload.pop("produced_at")
        payload.pop("decision_digest")
        return payload

    def expected_decision_digest(self) -> str:
        return canonical_json_digest(self.identity_payload())


def _ordered_gates(gates: list[GateResult]) -> list[GateResult]:
    """Require every frozen D-gate and apply its normative evaluation order."""
    gate_ids = [gate.gate_id for gate in gates]
    if len(set(gate_ids)) != len(gate_ids):
        raise ValueError("deterministic gate IDs must be unique")
    missing = set(DETERMINISTIC_GATE_ORDER) - set(gate_ids)
    unexpected = set(gate_ids) - set(DETERMINISTIC_GATE_ORDER)
    if missing or unexpected:
        raise ValueError(
            f"deterministic gates must match frozen set; missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )
    return sorted(gates, key=_gate_sort_key)


def evaluate_acceptance(
    *,
    judgment_ids: list[str],
    pack_digest: str,
    deterministic_gates: list[GateResult],
    cross_judge: CrossJudgeRecord,
    calibration_class_gate: CalibrationClassGate,
    policy_digest: str,
    proposed_next_check: str | None,
    supersedes_decision_id: str | None = None,
    produced_at: datetime | None = None,
) -> AcceptanceDecision:
    """Evaluate immutable gate inputs; current v1 can only reject or abstain."""
    gates = _ordered_gates(deterministic_gates)
    failed_reasons = {
        gate.reason_code
        for gate in gates
        if gate.status == "fail" and gate.reason_code is not None
    }
    fatal = bool(failed_reasons & _FATAL_REASON_CODES)
    decision: Literal["rejected", "abstained"] = (
        "rejected" if fatal else "abstained"
    )

    reason_codes: list[str] = []
    for gate in gates:
        if (
            gate.status != "pass"
            and gate.reason_code
            and gate.reason_code not in reason_codes
        ):
            reason_codes.append(gate.reason_code)
    if cross_judge.required and cross_judge.agreement != "exact":
        reason_codes.append("cross_judge_disagree")
    if not calibration_class_gate.acceptance_enabling_allowed:
        reason_codes.append("acceptance_enabling_disabled")
    if not calibration_class_gate.acceptance_enabled:
        reason_codes.append("class_not_enabled")
    for hold_reason in calibration_class_gate.hold_reasons:
        if hold_reason not in reason_codes:
            reason_codes.append(hold_reason)
    if not reason_codes:
        reason_codes.append("auto_accept_ineligible")

    ordered_judgment_ids = sorted(set(judgment_ids))
    if len(ordered_judgment_ids) != len(judgment_ids):
        raise ValueError("judgment IDs must be unique")
    body: dict[str, Any] = {
        "schema_version": "acceptance-decision/v1",
        "decision": decision,
        "judgment_ids": ordered_judgment_ids,
        "pack_digest": pack_digest,
        "deterministic_gates": [gate.model_dump(mode="json") for gate in gates],
        "cross_judge": cross_judge.model_dump(mode="json"),
        "calibration_version": calibration_class_gate.calibration_version,
        "calibration_class_gate": calibration_class_gate.model_dump(mode="json"),
        "reason_codes": reason_codes,
        "proposed_next_check": proposed_next_check,
        "policy_digest": policy_digest,
        "supersedes_decision_id": supersedes_decision_id,
    }
    decision_id = canonical_json_digest(body)
    decision_digest = canonical_json_digest({"decision_id": decision_id, **body})
    return AcceptanceDecision(
        decision_id=decision_id,
        decision_digest=decision_digest,
        produced_at=produced_at or datetime.now(UTC),
        **body,
    )
