from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from evallab.interpretation.trajectory_judgment import MachineJudgment, canonical_json_digest

D = {char: "sha256:" + char * 64 for char in "123456789abcdef"}
NOW = datetime(2026, 8, 26, tzinfo=UTC)


def bind_judgment_identity(payload: dict) -> dict:
    payload = dict(payload)
    payload["citation_ids"] = sorted(set(payload["citation_ids"]))
    payload["alternative_explanations"] = sorted(set(payload["alternative_explanations"]))
    payload["coverage_gaps"] = sorted(set(payload["coverage_gaps"]))
    id_body = {
        key: value
        for key, value in payload.items()
        if key not in {"produced_at", "judgment_id", "judgment_digest"}
    }
    payload["judgment_id"] = canonical_json_digest(id_body)
    payload["judgment_digest"] = canonical_json_digest(
        {**id_body, "judgment_id": payload["judgment_id"]}
    )
    return payload


def model_payload() -> dict:
    return bind_judgment_identity(
        {
            "schema_version": "machine-judgment/v1",
            "judgment_id": D["1"],
            "judgment_digest": D["2"],
            "producer_kind": "model",
            "pack_id": D["3"],
            "pack_digest": D["4"],
            "validity": "supported",
            "primary_label": {
                "namespace": "traj.judge.v1",
                "ontology_version": "traj.judge.ontology.v1",
                "class_id": "infrastructure_failure",
            },
            "finding_summary": "Observed infrastructure exception.",
            "earliest_supported_event_id": "event-1",
            "citation_ids": [D["5"]],
            "alternative_explanations": [],
            "coverage_gaps": [],
            "proposed_discriminator": None,
            "confidence": {
                "raw_label": "infrastructure_failure",
                "raw_score": 0.9,
                "calibrated_probability": None,
                "calibration_version": None,
            },
            "model_identity": {
                "provider": "google-antigravity",
                "model": "gemini-3.7-flash",
                "family": "gemini",
                "settings_digest": D["6"],
            },
            "prompt_digest": D["7"],
            "rubric_digest": D["8"],
            "output_schema_digest": D["9"],
            "raw_response_digest": D["a"],
            "produced_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    )


def deterministic_abstention_payload() -> dict:
    payload = model_payload()
    payload.update(
        {
            "producer_kind": "deterministic_abstention",
            "validity": "insufficient_evidence",
            "primary_label": None,
            "finding_summary": "Quality gate stopped judge invocation.",
            "citation_ids": [],
            "coverage_gaps": ["quality_quarantined"],
            "model_identity": None,
            "prompt_digest": None,
            "rubric_digest": None,
            "raw_response_digest": None,
        }
    )
    return bind_judgment_identity(payload)


def test_model_judgment_roundtrips_exact_contract() -> None:
    judgment = MachineJudgment.model_validate(model_payload())
    assert judgment.model_dump(mode="json") == model_payload()


def test_deterministic_abstention_has_no_model_or_label() -> None:
    judgment = MachineJudgment.model_validate(deterministic_abstention_payload())
    assert judgment.primary_label is None
    assert judgment.model_identity is None
    assert judgment.validity == "insufficient_evidence"


@pytest.mark.parametrize(
    "field,value",
    [
        ("primary_label", {"namespace": "x", "ontology_version": "v", "class_id": "c"}),
        ("model_identity", model_payload()["model_identity"]),
        ("prompt_digest", D["7"]),
        ("rubric_digest", D["8"]),
        ("raw_response_digest", D["a"]),
    ],
)
def test_deterministic_abstention_rejects_model_identity(
    field: str, value: object
) -> None:
    payload = deterministic_abstention_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        MachineJudgment.model_validate(payload)


def test_deterministic_abstention_rejects_non_insufficient_validity() -> None:
    payload = deterministic_abstention_payload()
    payload["validity"] = "supported"
    with pytest.raises(ValidationError, match="insufficient_evidence"):
        MachineJudgment.model_validate(payload)


def test_frozen_ontology_rejects_noncanonical_class_id() -> None:
    payload = model_payload()
    payload["primary_label"]["class_id"] = "wrong_target_action"
    with pytest.raises(ValidationError, match="frozen trajectory ontology"):
        MachineJudgment.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    ["model_identity", "prompt_digest", "rubric_digest", "raw_response_digest"],
)
def test_model_judgment_requires_complete_model_identity(field: str) -> None:
    payload = model_payload()
    payload[field] = None
    with pytest.raises(ValidationError, match="requires model"):
        MachineJudgment.model_validate(payload)


def test_citations_are_handle_ids_not_paths() -> None:
    payload = model_payload()
    payload["citation_ids"] = ["agent/trajectory.json#step=1"]
    with pytest.raises(ValidationError):
        MachineJudgment.model_validate(payload)


def test_duplicate_citation_ids_are_rejected() -> None:
    payload = model_payload()
    payload["citation_ids"] = [D["5"], D["5"]]
    with pytest.raises(ValidationError, match="unique"):
        MachineJudgment.model_validate(payload)


@pytest.mark.parametrize("field", ["alternative_explanations", "coverage_gaps"])
def test_duplicate_list_values_are_rejected(field: str) -> None:
    payload = model_payload()
    payload[field] = ["alpha", "alpha"]
    with pytest.raises(ValidationError, match="unique"):
        MachineJudgment.model_validate(payload)


def test_wrong_judgment_id_is_rejected() -> None:
    payload = model_payload()
    payload["judgment_id"] = D["1"]
    with pytest.raises(ValidationError, match="judgment_id"):
        MachineJudgment.model_validate(payload)


def test_wrong_judgment_digest_is_rejected() -> None:
    payload = model_payload()
    payload["judgment_digest"] = D["2"]
    with pytest.raises(ValidationError, match="judgment_digest"):
        MachineJudgment.model_validate(payload)


def test_publication_time_does_not_change_expected_content_digest() -> None:
    first = MachineJudgment.model_validate(model_payload())
    second_payload = model_payload()
    second_payload["produced_at"] = (NOW + timedelta(days=1)).isoformat()
    second = MachineJudgment.model_validate(second_payload)
    assert first.expected_judgment_digest() == second.expected_judgment_digest()


def test_identity_lists_are_canonicalized_before_digesting() -> None:
    first_payload = bind_judgment_identity(
        {
            **model_payload(),
            "citation_ids": [D["6"], D["5"]],
            "alternative_explanations": ["zeta", "alpha"],
            "coverage_gaps": ["state_missing", "linkage_missing"],
        }
    )
    second_payload = bind_judgment_identity(
        {
            **model_payload(),
            "citation_ids": [D["5"], D["6"]],
            "alternative_explanations": ["alpha", "zeta"],
            "coverage_gaps": ["linkage_missing", "state_missing"],
        }
    )
    first = MachineJudgment.model_validate(first_payload)
    second = MachineJudgment.model_validate(second_payload)
    assert first.citation_ids == second.citation_ids
    assert first.expected_judgment_digest() == second.expected_judgment_digest()


def test_json_schema_matches_frozen_machine_surface() -> None:
    schema = MachineJudgment.model_json_schema()
    expected = {
        "schema_version",
        "judgment_id",
        "judgment_digest",
        "producer_kind",
        "pack_id",
        "pack_digest",
        "validity",
        "primary_label",
        "finding_summary",
        "earliest_supported_event_id",
        "citation_ids",
        "alternative_explanations",
        "coverage_gaps",
        "proposed_discriminator",
        "confidence",
        "model_identity",
        "prompt_digest",
        "rubric_digest",
        "output_schema_digest",
        "raw_response_digest",
        "produced_at",
    }
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == expected
    assert set(schema["required"]) == expected
