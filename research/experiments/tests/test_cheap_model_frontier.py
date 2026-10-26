"""Consumer-visible budget boundaries for the bounded paid CM-1 experiment."""

from __future__ import annotations

import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
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
    plain._append_event(
        tmp_path / "cost.jsonl", {"event": "settled", "call_id": first, "cost_nano": 100_000_000}
    )
    plain.reserve(tmp_path, model, payload, "admitted-after-measured-settlement")
    events = [json.loads(line) for line in (tmp_path / "cost.jsonl").read_text().splitlines()]
    assert plain.charged(events) <= 4_000_000_000
    assert sum(event["event"] == "started" for event in events) == 4


def test_a_reservation_breach_holds_every_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = {"id": "one", "input_usd_per_million": 0.01, "output_usd_per_million": 0.01}
    call_id, amount = plain.reserve(tmp_path, model, {"max_tokens": 1}, "first")
    monkeypatch.setattr(plain, "reported_generation", lambda *_: {"total_cost": 1.0})
    with pytest.raises(plain.BudgetExhausted, match="reservation breach"):
        plain.settle_call(tmp_path, model, None, call_id, "generation-one", amount)
    events = [json.loads(line) for line in (tmp_path / "cost.jsonl").read_text().splitlines()]
    assert plain.charged(events) == 1_000_000_000
    with pytest.raises(plain.BudgetExhausted, match="held"):
        plain.reserve(
            tmp_path,
            {"id": "different", "input_usd_per_million": 0.01, "output_usd_per_million": 0.01},
            {"max_tokens": 1},
            "later",
        )


def test_clean_reader_verdict_cannot_hide_defect_claims() -> None:
    with pytest.raises(ValueError, match="requires no findings"):
        plain.validate_final(
            {
                "aid": "r-test",
                "task_verdict": "no_defect_found",
                "findings": [{"class": "weak_check", "summary": "defect"}],
            },
            "r-test",
        )


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {"requests": [], "final": {}},
        {"requests": []},
        {"requests": [{"tool": "read", "args": {}}] * 9},
        {"final": []},
    ],
)
def test_action_ambiguity_and_batch_bounds_are_refused(value: object) -> None:
    with pytest.raises(ValueError):
        plain.parse_action(value)


def test_inert_batch_metadata_does_not_discard_authorized_work() -> None:
    assert plain.parse_action(
        {"requests": [{"tool": "read", "args": {"path": "instruction.md"}}], "requests_count": 1}
    ) == ("requests", [{"tool": "read", "args": {"path": "instruction.md"}}])


@pytest.mark.parametrize(
    "text",
    [
        '{"requests": [{"tool": "read", "args": {"path": "instruction.md"}}]}',
        '```json\n{"requests": [{"tool": "read", "args": {"path": "instruction.md"}}]}\n```',
        'Here is my next step:\n{"requests": [{"tool": "read", "args": {"path": "instruction.md"}}]}',
        '{"requests": [{"tool": "read", "args": {"path": "instruction.md",}}]}',
    ],
)
def test_extract_json_recovers_one_action_object(text: str) -> None:
    kind, action = plain.parse_action(plain.extract_json(text))
    assert kind == "requests"
    assert action == [{"tool": "read", "args": {"path": "instruction.md"}}]


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2] trailing"])
def test_extract_json_refuses_non_object_content(text: str) -> None:
    with pytest.raises(ValueError):
        plain.parse_action(plain.extract_json(text))


@pytest.fixture
def frontier(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "run_plain", plain)
    spec = importlib.util.spec_from_file_location(
        "cm1_frontier",
        REPO / "research/experiments/cheap-model-frontier-20261021/score_frontier.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frontier_costs_keep_adapter_versions_and_unknown_reservations_separate(frontier) -> None:
    events = [
        {
            "event": "started",
            "identity": "plain/model/test/a",
            "call_id": "old",
            "adapter_version": "old",
            "reserved_nano": 50_000_000,
        },
        {"event": "settled", "call_id": "old", "cost_nano": 20_000_000},
        {
            "event": "started",
            "identity": "plain/model/test/b",
            "call_id": "new",
            "adapter_version": "new",
            "reserved_nano": 40_000_000,
        },
        {"event": "settled", "call_id": "new", "cost_nano": 30_000_000},
        {
            "event": "started",
            "identity": "plain/model/test/c",
            "call_id": "unknown",
            "adapter_version": "old",
            "reserved_nano": 70_000_000,
        },
        {
            "event": "started",
            "identity": "plain/model/dev/d",
            "call_id": "dev",
            "adapter_version": "old",
            "reserved_nano": 60_000_000,
        },
        {"event": "settled", "call_id": "dev", "cost_nano": 20_000_000},
    ]
    new = frontier.cost_for(events, "plain/model/test/", "new")
    assert new["model_calls"] == new["settled_calls"] == 1
    assert new["billed_usd"] == pytest.approx(0.03)
    old = frontier.cost_for(events, "plain/model/test/", "old")
    assert old["model_calls"] == 2 and old["unknown_cost_calls"] == 1
    assert old["billed_usd"] is None
    assert old["known_billed_usd"] == pytest.approx(0.02)
    assert old["retained_unknown_reservation_usd"] == pytest.approx(0.07)
    assert old["enforced_charge_usd"] == pytest.approx(0.09)
    assert frontier.cost_for(events, "")["model_calls"] == 4


@pytest.mark.parametrize("protocol, marker", [("plain", "circle"), ("v1", "rect"), ("v2", "path")])
def test_frontier_partial_points_show_escaped_version_and_coverage_labels(
    frontier, protocol: str, marker: str
) -> None:
    row = {
        "model": "synthetic-model",
        "slug": "partial<&>",
        "protocol": protocol,
        "adapter_version": "old&v1",
        "test_started": True,
        "measurement_complete": False,
        "coverage": {"tasks_with_status": 3},
        "score": {"category_1to1": {"recall": {"k": 2}}},
        "test_cost": {"billed_usd": 0.01},
    }
    complete = row | {
        "slug": "complete",
        "adapter_version": "new",
        "measurement_complete": True,
        "coverage": {"tasks_with_status": 41},
    }
    svg = ET.fromstring(frontier.make_plot({"rows": [row, complete]}))
    points = [
        element
        for element in svg.iter()
        if element.tag == f"{{http://www.w3.org/2000/svg}}{marker}"
        and element.get("stroke-width") == "2"
    ]
    assert len(points) == 2
    assert points[0].get("fill") == "white"
    assert points[1].get("fill") == points[1].get("stroke") != "white"
    labels = [element.text for element in svg.iter("{http://www.w3.org/2000/svg}text")]
    assert f"partial<&> / {protocol} old&v1 (3/41 tasks)" in labels
    assert f"complete / {protocol} new" in labels
