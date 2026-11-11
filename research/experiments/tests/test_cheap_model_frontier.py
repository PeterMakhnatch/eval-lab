"""Frozen CM-1 cost scoring preserves unknown reservations and adapter versions."""

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def frontier():
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
