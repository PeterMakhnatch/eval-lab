"""Campaign identity and cumulative admission survive qualifier restarts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from evallab.regrade import SpendCapReached

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/test-repair-performance-20261017/qualify.py"
)
_spec = importlib.util.spec_from_file_location("envcheck_qualification_test", SCRIPT)
assert _spec is not None and _spec.loader is not None
qualify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qualify)


def test_reopened_gate_preserves_charges_and_refuses_another_approval(tmp_path: Path) -> None:
    path = tmp_path / "gate.json"
    original = qualify.load_gate(path, 0.18, 0.09, approval="approved campaign A")
    original.charge()
    qualify.write_json(path, original.to_json())
    prior_bytes = path.read_bytes()

    with pytest.raises(ValueError):
        qualify.load_gate(path, 0.18, 0.09, approval="new campaign B")
    assert path.read_bytes() == prior_bytes

    resumed = qualify.load_gate(path, 0.18, 0.09, approval="approved campaign A")
    resumed.charge()
    with pytest.raises(SpendCapReached):
        resumed.charge()
    assert resumed.executions == 2
    assert resumed.to_json()["reserved_usd"] == 0.18


@pytest.mark.parametrize("field,value", [("cap_usd", 0.27), ("reserve_usd", 0.03)])
def test_changed_reservation_terms_cannot_reopen_an_existing_gate(
    tmp_path: Path, field: str, value: float,
) -> None:
    path = tmp_path / "gate.json"
    original = qualify.load_gate(path, 0.18, 0.09, approval="same approval")
    original.charge()
    qualify.write_json(path, original.to_json())
    terms = {"cap_usd": 0.18, "reserve_usd": 0.09, "approval": "same approval"}
    terms[field] = value
    with pytest.raises(ValueError):
        qualify.load_gate(path, **terms)
    assert json.loads(path.read_text())["executions"] == 1
