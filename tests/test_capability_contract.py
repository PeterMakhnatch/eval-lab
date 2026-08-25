from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.capability_contract import (
    BoundArtifactRef,
    CapabilityClaimSpec,
    CapabilityContractSpec,
    ClaimKind,
    FreezeRecord,
    HarnessPolicySnapshot,
    IntegrationCostLedger,
    NoveltyCertificate,
    evaluate_capability_contract,
)
from evallab.schemas import TaskContamination
from evallab.screen import validate_capability_admission


def _artifact(root: Path, name: str, kind: str) -> BoundArtifactRef:
    path = f"evidence/{name}.json"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"name": name}) + "\n")
    return BoundArtifactRef.bind(repo_root=root, path=path, kind=kind)  # type: ignore[arg-type]


def _json_artifact(
    root: Path,
    name: str,
    kind: str,
    payload: dict[str, object],
) -> BoundArtifactRef:
    path = f"evidence/{name}.json"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload) + "\n")
    return BoundArtifactRef.bind(  # type: ignore[arg-type]
        repo_root=root, path=path, kind=kind
    )


def _policy(root: Path, label: str, **overrides: object) -> HarnessPolicySnapshot:
    values: dict[str, object] = {
        "label": label,
        "protocol_identity": "protocol-a",
        "harness_identity": "harness-a",
        "retries": 1,
        "schema_guard": True,
        "tool_shortlisting": ["shell"],
        "termination": "final-or-budget",
        "step_budget": 20,
        "token_budget": 10_000,
        "wall_budget_seconds": 600,
        "compaction_model": "none",
        "compaction_settings": {},
        "compaction_seed": 7,
        "model_identity": "model@digest",
        "preamble_identity": "preamble@digest",
        "adapter_identity": "adapter@digest",
        "truncation": "refuse",
    }
    values.update(overrides)
    path = f"evidence/policy-{label}.json"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({key: value for key, value in values.items() if key != "label"}) + "\n"
    )
    values["artifact"] = BoundArtifactRef.bind(
        repo_root=root, path=path, kind="harness_policy"
    )
    return HarnessPolicySnapshot.model_validate(values)


def _freeze(root: Path, artifacts: list[BoundArtifactRef], *, late: bool = False) -> FreezeRecord:
    first = datetime(2026, 8, 24, 12, tzinfo=UTC)
    frozen = first + timedelta(seconds=1) if late else first - timedelta(seconds=1)
    path = "evidence/freeze.json"
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(json.dumps({
        "frozen_at": frozen.isoformat().replace("+00:00", "Z"),
        "first_trace_at": first.isoformat().replace("+00:00", "Z"),
        "frozen_artifacts": [item.model_dump(mode="json") for item in artifacts],
        "post_trace_revisions": [],
        "identity": "test-freeze-before-trace",
    }) + "\n")
    record = BoundArtifactRef.bind(repo_root=root, path=path, kind="freeze_record")
    return FreezeRecord(
        artifact=record,
        frozen_at=frozen,
        first_trace_at=first,
        frozen_artifacts=artifacts,
    )


def _result(root: Path, claim: CapabilityClaimSpec):
    return evaluate_capability_contract(
        CapabilityContractSpec(experiment_id="contract-test", claims=[claim]),
        repo_root=root,
    )


def _claim(report, kind: ClaimKind):
    return next(item for item in report.claims if item.kind == kind)


def test_missing_claims_are_explicit_valid_insufficiency(tmp_path: Path) -> None:
    report = evaluate_capability_contract(
        CapabilityContractSpec(experiment_id="empty"), repo_root=tmp_path
    )
    assert report.status == "valid_insufficient"
    assert report.refuse_substantive_generality is True
    assert [item.kind for item in report.claims] == list(ClaimKind)
    assert {item.status for item in report.claims} == {"unavailable"}
    validate_capability_admission(report)


def test_hidden_policy_and_budget_difference_fails_closed(tmp_path: Path) -> None:
    evidence = [
        _artifact(tmp_path, "curve", "curve_report"),
        _artifact(tmp_path, "certificate", "workbench_certificate"),
        _artifact(tmp_path, "oracle", "surface_oracle"),
        _artifact(tmp_path, "nop", "nop_control"),
        _artifact(tmp_path, "margin", "equivalence_preregistration"),
    ]
    left = _policy(tmp_path, "left", protocol_identity="protocol-left")
    right = _policy(tmp_path, "right", protocol_identity="protocol-right", retries=2)
    frozen = [*evidence, left.artifact, right.artifact]
    report = _result(tmp_path, CapabilityClaimSpec(
        kind=ClaimKind.P,
        availability="available",
        statement="protocol portability",
        evidence=evidence,
        harness_policies=[left, right],
        freeze=_freeze(tmp_path, frozen),
        declared_factor="protocol",
        preregistered_equivalence_margin=0.1,
        equivalence_interval_95=(-0.05, 0.05),
    ))
    result = _claim(report, ClaimKind.P)
    assert result.status == "invalid"
    assert any("hidden retry" in reason for reason in result.reasons)


def test_late_freeze_and_post_trace_revision_are_invalid(tmp_path: Path) -> None:
    production = _artifact(tmp_path, "production", "production_report")
    policy = _policy(tmp_path, "production")
    freeze = _freeze(tmp_path, [production, policy.artifact], late=True)
    freeze = freeze.model_copy(update={"post_trace_revisions": ["changed policy"]})
    report = _result(tmp_path, CapabilityClaimSpec(
        kind=ClaimKind.Y,
        availability="available",
        statement="production reliability",
        evidence=[production],
        harness_policies=[policy],
        freeze=freeze,
    ))
    assert report.status == "invalid"
    reasons = _claim(report, ClaimKind.Y).reasons
    assert any("freeze must precede" in reason for reason in reasons)
    assert any("post-trace revision" in reason for reason in reasons)


def test_novelty_contamination_and_prompt_borrowing_fail_closed(tmp_path: Path) -> None:
    registry = _artifact(tmp_path, "registry", "task_registry_record")
    novelty_artifact = _artifact(tmp_path, "novelty", "novelty_certificate")
    policy = _policy(tmp_path, "novelty")
    first = datetime(2026, 8, 24, 12, tzinfo=UTC)
    novelty = NoveltyCertificate(
        artifact=novelty_artifact,
        issued_at=first - timedelta(minutes=1),
        first_trace_at=first,
        task_identity="heldout@1",
        registry_record=registry,
        contamination=TaskContamination(in_pretrain="y", basis="public benchmark"),
        heldout_allowed_use=True,
        reference_prompt_borrowing=True,
        recoverable_in_world_knowledge=False,
    )
    evidence = [_artifact(tmp_path, "adapt", "adaptation_report")]
    report = _result(tmp_path, CapabilityClaimSpec(
        kind=ClaimKind.U,
        availability="available",
        statement="unfamiliar adaptation",
        evidence=evidence,
        harness_policies=[policy],
        novelty=novelty,
        freeze=_freeze(tmp_path, [*evidence, policy.artifact, novelty_artifact, registry]),
    ))
    reasons = _claim(report, ClaimKind.U).reasons
    assert any("in_pretrain='n'" in reason for reason in reasons)
    assert any("reference-prompt borrowing" in reason for reason in reasons)
    assert any("recoverable in-world" in reason for reason in reasons)


def test_missing_production_ledger_is_insufficient_not_invalid(tmp_path: Path) -> None:
    report = _result(tmp_path, CapabilityClaimSpec(
        kind=ClaimKind.Y,
        availability="available",
        statement="production reliability",
    ))
    result = _claim(report, ClaimKind.Y)
    assert report.status == "valid_insufficient"
    assert result.status == "insufficient"
    assert any("IntegrationCostLedger" in reason for reason in result.reasons)


def test_ledger_rejects_scalar_score(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, "ledger", "integration_ledger")
    with pytest.raises(ValidationError):
        IntegrationCostLedger.model_validate({
            "artifact": artifact.model_dump(mode="json"),
            "raw_dependencies": [],
            "added_loc": 0,
            "modified_loc": 0,
            "environment_specific_symbols": [],
            "prompt_tokens": 0,
            "revisions": 0,
            "post_trace_fixes": 0,
            "score": 1.0,
        })


def test_tamper_is_reread_and_replay_identity_is_rejected(tmp_path: Path) -> None:
    production = _artifact(tmp_path, "production", "production_report")
    policy = _policy(tmp_path, "tamper")
    freeze = _freeze(tmp_path, [production, policy.artifact])
    (tmp_path / production.path).write_text('{"tampered":true}\n')
    report = _result(tmp_path, CapabilityClaimSpec(
        kind=ClaimKind.Y,
        availability="available",
        statement="production reliability",
        evidence=[production],
        harness_policies=[policy],
        freeze=freeze,
    ))
    assert report.status == "invalid"
    assert any("bytes changed" in reason for reason in _claim(report, ClaimKind.Y).reasons)
    with pytest.raises(ValidationError):
        BoundArtifactRef(
            path="evidence/replayed.json",
            sha256=production.sha256,
            kind=production.kind,
            identity=production.identity,
        )


def test_one_claim_kind_never_implies_another(tmp_path: Path) -> None:
    report = _result(tmp_path, CapabilityClaimSpec(
        kind=ClaimKind.P,
        availability="unavailable",
        statement="P unavailable",
        limitations=["component bounded"],
    ))
    assert _claim(report, ClaimKind.P).status == "unavailable"
    for kind in (ClaimKind.R, ClaimKind.U, ClaimKind.C, ClaimKind.Y):
        assert _claim(report, kind).status == "unavailable"
    assert "component bounded" in _claim(report, ClaimKind.P).reasons


def test_continual_learning_does_not_require_unfamiliar_environment_evidence(
    tmp_path: Path,
) -> None:
    report = _result(tmp_path, CapabilityClaimSpec(
        kind=ClaimKind.C,
        availability="available",
        statement="continual learning across frozen longitudinal phases",
    ))

    result = _claim(report, ClaimKind.C)
    assert result.status == "insufficient"
    assert not any("novelty" in reason.lower() for reason in result.reasons)


def test_continual_phases_reject_copied_bytes_at_different_paths(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": 1,
        "phase_identity": "phase-a",
        "phase_index": 0,
        "observed_at": "2026-08-24T10:00:00Z",
    }
    first = _json_artifact(tmp_path, "phase-a", "longitudinal_phase", payload)
    second = _json_artifact(tmp_path, "phase-copy", "longitudinal_phase", payload)
    report = _result(tmp_path, CapabilityClaimSpec(
        kind=ClaimKind.C,
        availability="available",
        statement="continual learning",
        longitudinal_phases=[first, second],
    ))

    result = _claim(report, ClaimKind.C)
    assert result.status == "invalid"
    assert "C longitudinal phases must bind distinct bytes" in result.reasons


def test_continual_phases_reject_reversed_byte_bound_order(tmp_path: Path) -> None:
    first = _json_artifact(tmp_path, "phase-0", "longitudinal_phase", {
        "schema_version": 1,
        "phase_identity": "phase-0",
        "phase_index": 0,
        "observed_at": "2026-08-24T10:00:00Z",
    })
    second = _json_artifact(tmp_path, "phase-1", "longitudinal_phase", {
        "schema_version": 1,
        "phase_identity": "phase-1",
        "phase_index": 1,
        "observed_at": "2026-08-24T11:00:00Z",
    })
    report = _result(tmp_path, CapabilityClaimSpec(
        kind=ClaimKind.C,
        availability="available",
        statement="continual learning",
        longitudinal_phases=[second, first],
    ))

    result = _claim(report, ClaimKind.C)
    assert result.status == "invalid"
    assert any("strictly ordered" in reason for reason in result.reasons)


def test_invalid_report_is_refused_by_screen_policy_guard(tmp_path: Path) -> None:
    production = _artifact(tmp_path, "production", "production_report")
    policy = _policy(tmp_path, "screen")
    report = _result(tmp_path, CapabilityClaimSpec(
        kind=ClaimKind.Y,
        availability="available",
        statement="production reliability",
        evidence=[production],
        harness_policies=[policy],
        freeze=_freeze(tmp_path, [production, policy.artifact], late=True),
    ))
    with pytest.raises(ValueError, match="invalid capability contract"):
        validate_capability_admission(report)
