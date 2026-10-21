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


def test_a_reservation_breach_holds_every_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = {"id": "one", "input_usd_per_million": 0.01, "output_usd_per_million": 0.01}
    call_id, amount = plain.reserve(tmp_path, model, {"max_tokens": 1}, "first")
    monkeypatch.setattr(plain, "reported_generation", lambda *_: {"total_cost": 1.0})
    with pytest.raises(plain.BudgetExhausted, match="reservation breach"):
        plain.settle_call(tmp_path, model, None, call_id, "generation-one", amount)
    events = [json.loads(line) for line in (tmp_path / "cost.jsonl").read_text().splitlines()]
    assert plain.charged(events) == 1_000_000_000
    with pytest.raises(plain.BudgetExhausted, match="held"):
        plain.reserve(tmp_path, {"id": "different", "input_usd_per_million": 0.01,
                                 "output_usd_per_million": 0.01}, {"max_tokens": 1}, "later")


def test_clean_reader_verdict_cannot_hide_defect_claims() -> None:
    with pytest.raises(ValueError, match="requires no findings"):
        plain.validate_final({"aid": "r-test", "task_verdict": "no_defect_found",
                              "findings": [{"class": "weak_check", "summary": "defect"}]}, "r-test")


@pytest.mark.parametrize("value", [
    {},
    [],
    {"requests": [], "final": {}},
    {"requests": []},
    {"requests": [{"tool": "read", "args": {}}] * 9},
    {"final": []},
])
def test_action_ambiguity_and_batch_bounds_are_refused(value: object) -> None:
    with pytest.raises(ValueError):
        plain.parse_action(value)


def test_inert_batch_metadata_does_not_discard_authorized_work() -> None:
    assert plain.parse_action({"requests": [{"tool": "read", "args": {"path": "instruction.md"}}],
                               "requests_count": 1}) == (
        "requests", [{"tool": "read", "args": {"path": "instruction.md"}}])


@pytest.mark.parametrize("text", [
    '{"requests": [{"tool": "read", "args": {"path": "instruction.md"}}]}',
    '```json\n{"requests": [{"tool": "read", "args": {"path": "instruction.md"}}]}\n```',
    'Here is my next step:\n{"requests": [{"tool": "read", "args": {"path": "instruction.md"}}]}',
    '{"requests": [{"tool": "read", "args": {"path": "instruction.md",}}]}',
])
def test_extract_json_recovers_one_action_object(text: str) -> None:
    kind, action = plain.parse_action(plain.extract_json(text))
    assert kind == "requests"
    assert action == [{"tool": "read", "args": {"path": "instruction.md"}}]


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2] trailing"])
def test_extract_json_refuses_non_object_content(text: str) -> None:
    with pytest.raises(ValueError):
        plain.parse_action(plain.extract_json(text))
