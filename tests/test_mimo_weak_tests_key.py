"""mimo-weak-tests-v1: labels agree with each other and with the other keys."""

from __future__ import annotations

import json
from pathlib import Path

KEYS = Path(__file__).resolve().parents[1] / "research/answer-keys"


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_partial_fix_passes_flip_and_their_packages_are_weak() -> None:
    items = {i["item_id"]: i for i in _jsonl(KEYS / "mimo-weak-tests-v1/items.jsonl")}
    pairs = _jsonl(KEYS / "mimo-weak-tests-v1/pairs.jsonl")
    weak_packages = {
        i["subject"]["task_digest"]
        for i in items.values()
        if i["subject"]["kind"] == "task" and i["expected_category"] == "weak"
    }
    children = [
        i for i in items.values() if i["subject"]["kind"] == "trace" and i["role"] == "child"
    ]
    assert len(children) == len(pairs) == 4
    for child in children:
        # A pass whose reward is not real sits on a package this key calls weak.
        assert child["expected_verdict"] == "fail" and child["expected_regrade"] == "flips"
        assert child["subject"]["task_digest"] in weak_packages
    for pair in pairs:
        parent, child = items[pair["parent"]], items[pair["child"]]
        assert parent["expected_verdict"] == "pass" and parent["expected_regrade"] == "stays"
        assert parent["subject"]["task_id"] == child["subject"]["task_id"]
        assert parent["split"] == child["split"] == pair["split"]


def test_no_package_is_both_weak_and_a_control_across_keys() -> None:
    """A control or refuted flag must not sit on bytes another key proves weak."""
    verdicts: dict[str, set[str]] = {}
    for key in ("mimo-weak-tests-v1", "executed-outcomes-v1"):
        for item in _jsonl(KEYS / key / "items.jsonl"):
            if item["subject"]["kind"] == "task":
                verdicts.setdefault(item["subject"]["task_digest"], set()).add(
                    item["expected_verdict"]
                )
    assert [d for d, v in verdicts.items() if len(v) > 1] == []


def test_one_split_per_task() -> None:
    splits: dict[str, set[str]] = {}
    for item in _jsonl(KEYS / "mimo-weak-tests-v1/items.jsonl"):
        assert item["split"] in ("dev", "test")
        splits.setdefault(item["subject"]["task_id"], set()).add(item["split"])
    assert all(len(s) == 1 for s in splits.values())
