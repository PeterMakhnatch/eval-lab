from __future__ import annotations

import pytest
from pydantic import ValidationError

from evallab.benchmark_program_contracts import (
    CampaignCalibrationLedger,
    CampaignMeasurementLedger,
    SyntheticFamilyType,
    canonical_bytes,
    canonical_json,
    compute_prefixed_sha256,
    compute_sha256,
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
