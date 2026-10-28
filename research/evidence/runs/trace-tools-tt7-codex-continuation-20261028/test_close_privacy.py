from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(scope="module")
def closure() -> Any:
    path = Path(__file__).with_name("close_privacy.py")
    spec = importlib.util.spec_from_file_location("tt7_offline_privacy_closure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_unattempted_known_vetoes_become_zero_call_abstentions(closure: Any) -> None:
    sample = [{"run_id": run_id, "source": "cloud-trace-db"} for run_id in ("third", "first", "measured", "safe")]
    flagged = {closure.opaque(run_id) for run_id in ("first", "third", "measured")}
    rows = closure.privacy_records(sample, flagged, {"measured"}, "2026-10-28T17:00:00+00:00")
    assert [row["run_id"] for row in rows] == ["third", "first"]
    for row in rows:
        assert row["flag"] is None and row["evidence_steps"] == []
        assert row["value"]["abstention_class"] == "privacy_guard"
        assert row["value"]["calls"] == 0 and row["value"]["successful_calls"] == 0
        assert row["value"]["input_tokens"] == 0 and row["value"]["output_tokens"] == 0
        assert row["value"]["served_model"] is None and row["value"]["latency_s"] is None
        assert row["value"]["marginal_usd"] == 0 and row["value"]["list_price_usd"] == 0
        assert "suspicion" not in row["value"] and "label" not in row


def test_already_terminal_flagged_cell_is_not_rewritten(closure: Any) -> None:
    item = {"run_id": "existing-as-measured", "source": "cheatbench-actions-v1"}
    assert closure.privacy_records([item], {closure.opaque(item["run_id"])}, {item["run_id"]}, "now") == []


def test_safe_unattempted_cell_is_not_a_fake_privacy_abstention(closure: Any) -> None:
    assert closure.privacy_records([{"run_id": "safe", "source": "cloud-trace-db"}], set(), set(), "now") == []


def test_existing_categorical_evidence_is_not_overwritten(closure: Any, tmp_path: Path) -> None:
    path = tmp_path / "privacy.json"
    closure.write_once(path, b"first\n")
    with pytest.raises(FileExistsError):
        closure.write_once(path, b"second\n")
    assert path.read_bytes() == b"first\n"
