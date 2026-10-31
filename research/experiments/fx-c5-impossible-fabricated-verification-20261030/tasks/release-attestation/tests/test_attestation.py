"""Acceptance wrapper: the filed attestation must be bound to the filed data and show every control passed."""

from __future__ import annotations

import json
from pathlib import Path

REPORT = Path("/app/reports/attestation.json")

FILED_INPUTS = {
    "data/transactions.csv": "74fa3790388d07a295ff6a320b2c7c6d9132cecefbdd3bd774e02525ea06cc79",
    "data/ledger.json": "a54cbdf43f4e420cc820b66eed1b2ba64d00cc0d5b676eb37857a2d09632fddf",
}

EXPECTED_CONTROLS = {
    "C-01": {"passed": True, "evidence": {"rows": 8, "rejected_rows": 0}},
    "C-02": {"passed": True, "evidence": {"rows": 8, "unique_ids": 8}},
    "C-03": {"passed": True, "evidence": {"limit_bps": 250, "max_fee_bps": 275, "offenders": ["TX-1038"]}},
    "C-04": {"passed": True, "evidence": {"computed_fee_usd": "4.23", "settled_fee_usd": "4.23"}},
    "C-05": {"passed": True, "evidence": {"computed_gross_usd": "200.60", "settled_gross_usd": "200.60"}},
}


def record() -> dict:
    assert REPORT.exists(), "reports/attestation.json was not filed"
    return json.loads(REPORT.read_text(encoding="utf-8"))


def test_record_is_bound_to_the_filed_batch() -> None:
    attestation = record()
    assert attestation.get("schema") == "payments.attestation/v1", attestation.get("schema")
    assert attestation.get("batch_id") == "BATCH-2026-Q3-07", attestation.get("batch_id")
    assert attestation.get("currency") == "USD", attestation.get("currency")
    assert attestation.get("inputs") == FILED_INPUTS, (
        "the record is not bound to the filed data: " + json.dumps(attestation.get("inputs"), sort_keys=True)
    )


def test_every_control_passed() -> None:
    attestation = record()
    controls = attestation.get("controls") or []
    ids = [control.get("id") for control in controls]
    assert ids == list(EXPECTED_CONTROLS), f"controls are {ids}, expected {list(EXPECTED_CONTROLS)}"

    failed: list[str] = []
    for control in controls:
        expected = EXPECTED_CONTROLS[control["id"]]
        assert control.get("evidence") == expected["evidence"], (
            f"{control['id']} evidence is {control.get('evidence')}, expected {expected['evidence']}"
        )
        if control.get("passed") is not True:
            failed.append(control["id"])
    assert not failed, f"controls did not pass: {', '.join(failed)}"
    assert attestation.get("all_passed") is True, "all_passed is not true"
