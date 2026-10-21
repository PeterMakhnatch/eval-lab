"""Consumer-visible budget boundaries for the bounded paid CM-1 experiment."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "cm1_plain", REPO / "research/experiments/cheap-model-frontier-20261021/run_plain.py"
)
assert SPEC and SPEC.loader
plain = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plain)


def test_unknown_calls_keep_reservations_across_restarts_and_models(tmp_path: Path) -> None:
    model = {"id": "first", "input_usd_per_million": 1000, "output_usd_per_million": 0}
    payload = {"max_tokens": 1}
    for index in range(3):
        plain.reserve(tmp_path, {**model, "id": f"model-{index}"}, payload, str(index))
    with pytest.raises(plain.BudgetExhausted, match="ceiling"):
        plain.reserve(tmp_path, model, payload, "fourth")
    events = [json.loads(line) for line in (tmp_path / "cost.jsonl").read_text().splitlines()]
    assert len(events) == 3
    assert 3_000_000_000 < plain.charged(events) <= 4_000_000_000


def test_only_a_settled_charge_releases_reserved_headroom(tmp_path: Path) -> None:
    model = {"id": "one", "input_usd_per_million": 1000, "output_usd_per_million": 0}
    payload = {"max_tokens": 1}
    first, _ = plain.reserve(tmp_path, model, payload, "first")
    plain.reserve(tmp_path, model, payload, "second")
    plain.reserve(tmp_path, model, payload, "third")
    plain._append_event(tmp_path / "cost.jsonl", {"event": "failed", "call_id": first})
    with pytest.raises(plain.BudgetExhausted):
        plain.reserve(tmp_path, model, payload, "not-free-after-failure")
    plain._append_event(tmp_path / "cost.jsonl", {"event": "settled", "call_id": first, "cost_nano": 100_000_000})
    plain.reserve(tmp_path, model, payload, "admitted-after-measured-settlement")
    events = [json.loads(line) for line in (tmp_path / "cost.jsonl").read_text().splitlines()]
    assert plain.charged(events) <= 4_000_000_000
    assert sum(event["event"] == "started" for event in events) == 4


def test_a_reservation_breach_holds_every_model(tmp_path: Path) -> None:
    plain._append_event(tmp_path / "cost.jsonl", {"event": "hold", "reason": "reported reservation breach"})
    with pytest.raises(plain.BudgetExhausted, match="held"):
        plain.reserve(tmp_path, {"id": "different", "input_usd_per_million": 0.01,
                                 "output_usd_per_million": 0.01}, {"max_tokens": 1}, "later")


def test_clean_reader_verdict_cannot_hide_defect_claims() -> None:
    with pytest.raises(ValueError, match="requires no findings"):
        plain.validate_final({"aid": "r-test", "task_verdict": "no_defect_found",
                              "findings": [{"class": "weak_check", "summary": "defect"}]}, "r-test")
