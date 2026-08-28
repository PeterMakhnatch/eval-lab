from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.benchmark_program_contracts import (
    CampaignCalibrationLedger,
    CampaignMeasurementLedger,
    CellFactorsA,
    CellFactorsB,
    CellFactorsC,
    FaultClass,
    FaultInjectionRecord,
    SyntheticFamilySpec,
    SyntheticFamilyType,
    canonical_bytes,
    canonical_json,
    compute_prefixed_sha256,
    compute_sha256,
    safe_resolve_subpath,
    validate_safe_relative_path,
)


def test_canonical_json_and_sha256_stability():
    obj1 = {"b": 2, "a": 1, "nested": {"y": [1, 2], "x": "test"}}
    obj2 = {"nested": {"x": "test", "y": [1, 2]}, "a": 1, "b": 2}

    json1 = canonical_json(obj1)
    json2 = canonical_json(obj2)
    assert json1 == json2 == '{"a":1,"b":2,"nested":{"x":"test","y":[1,2]}}'
    assert canonical_bytes(obj1) == canonical_bytes(obj2)

    hash1 = compute_sha256(obj1)
    hash2 = compute_sha256(obj2)
    assert hash1 == hash2
    assert len(hash1) == 64

    prefixed = compute_prefixed_sha256(obj1)
    assert prefixed == f"sha256:{hash1}"


def test_path_validation_and_safety(tmp_path: Path):
    valid_paths = [
        "environment/Dockerfile",
        "tests/test.sh",
        "nested/deep/subpath/file.json",
        "simple.txt",
    ]
    for p in valid_paths:
        assert validate_safe_relative_path(p) == p
        resolved = safe_resolve_subpath(tmp_path, p)
        assert str(resolved).startswith(str(tmp_path.resolve()))

    invalid_paths = [
        "",
        "/absolute/path",
        "../escape/path",
        "nested/../../escape",
        "foo//bar",
        "/leading_slash",
        "trailing_slash/",
        r"windows\style\path",
    ]
    for p in invalid_paths:
        with pytest.raises(ValueError):
            validate_safe_relative_path(p)


def test_fault_injection_record_contracts():
    fault_hash = "a" * 64
    verifier_hash = "b" * 64

    record = FaultInjectionRecord(
        fault_id=fault_hash,
        task_id="task_123",
        twin_task_id="twin_123",
        target_tool="calculate_sum",
        fault_class=FaultClass.TRANSIENT_HTTP_5XX,
        target_canonical_event_ordinal=2,
        injection_payload={"message": "sidecar 500 error"},
        recovery_contract="retry_with_backoff",
        verifier_oracle_digest=verifier_hash,
    )

    digest = record.identity_digest()
    assert len(digest) == 64

    # Extra fields forbidden
    with pytest.raises(ValidationError):
        FaultInjectionRecord(
            fault_id=fault_hash,
            task_id="task_123",
            twin_task_id="twin_123",
            target_tool="calculate_sum",
            fault_class=FaultClass.TRANSIENT_HTTP_5XX,
            target_canonical_event_ordinal=2,
            injection_payload={},
            recovery_contract="retry",
            verifier_oracle_digest=verifier_hash,
            unknown_field="not_allowed",  # type: ignore
        )

    # Immutability
    with pytest.raises(ValidationError):
        record.target_tool = "other"  # type: ignore


def test_synthetic_family_specs_and_identity_stability():
    spec = SyntheticFamilySpec(
        family=SyntheticFamilyType.FAMILY_B_FUNCDAG_V2,
        variant_id="depth_3_width_2",
        critical_path_depth=3,
        parallel_width=2,
        distractor_count=1,
        hidden_contract_hash="c" * 64,
    )

    digest1 = spec.identity_digest()
    digest2 = spec.identity_digest()
    assert digest1 == digest2
    assert len(digest1) == 64

    # Extra forbid
    with pytest.raises(ValidationError):
        SyntheticFamilySpec(
            family=SyntheticFamilyType.FAMILY_B_FUNCDAG_V2,
            variant_id="depth_3_width_2",
            hidden_contract_hash="c" * 64,
            bogus_key="forbidden",  # type: ignore
        )


def test_cell_factors_models():
    cell_a = CellFactorsA(dilation_tokens=16384, forced_compaction=True, seed=42)
    assert cell_a.dilation_tokens == 16384
    assert cell_a.forced_compaction is True

    cell_b = CellFactorsB(critical_path_depth=4, parallel_width=3, distractor_count=2, seed=10)
    assert cell_b.critical_path_depth == 4

    cell_c = CellFactorsC(
        fault_class=FaultClass.PERSISTENT_SCHEMA_MISMATCH, fault_injection_count=4, seed=99
    )
    assert cell_c.fault_class == FaultClass.PERSISTENT_SCHEMA_MISMATCH

    with pytest.raises(ValidationError):
        CellFactorsB(
            critical_path_depth=0, parallel_width=1, distractor_count=0, seed=1
        )  # ge=1 violated


def test_campaign_ledgers_discriminated_contracts():
    ulid1 = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    ulid2 = "01ARZ3NDEKTSV4RRFFQ69G5FAW"

    calib = CampaignCalibrationLedger(
        ledger_id=ulid1,
        matrix_ref=ulid2,
        family=SyntheticFamilyType.FAMILY_A_STATE_INVERSION,
        status="pending",
    )
    assert calib.campaign_phase == "campaign_0_pilot"
    assert calib.reportable_rates is False

    # Setting reportable_rates=True in Campaign 0 is mechanically forbidden
    with pytest.raises(ValidationError):
        CampaignCalibrationLedger(
            ledger_id=ulid1,
            matrix_ref=ulid2,
            campaign_phase="campaign_0_pilot",
            reportable_rates=True,  # type: ignore
            family=SyntheticFamilyType.FAMILY_A_STATE_INVERSION,
            status="pending",
        )

    meas = CampaignMeasurementLedger(
        ledger_id=ulid1,
        matrix_ref=ulid2,
        campaign_phase="billable_cohort",
        family=SyntheticFamilyType.FAMILY_B_FUNCDAG_V2,
        status="active",
    )
    assert meas.reportable_rates is True

    with pytest.raises(ValidationError):
        CampaignMeasurementLedger(
            ledger_id=ulid1,
            matrix_ref=ulid2,
            campaign_phase="billable_cohort",
            reportable_rates=False,  # type: ignore
            family=SyntheticFamilyType.FAMILY_B_FUNCDAG_V2,
            status="active",
        )
