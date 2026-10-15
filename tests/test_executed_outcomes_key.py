"""executed-outcomes-v1: every label is backed by evidence of the matching status."""

from __future__ import annotations

import json
from pathlib import Path

KEY = Path(__file__).resolve().parents[1] / "research/answer-keys/executed-outcomes-v1"


def _jsonl(name: str) -> list[dict]:
    return [json.loads(line) for line in (KEY / name).read_text().splitlines() if line.strip()]


def test_verdicts_follow_evidence() -> None:
    items = _jsonl("items.jsonl")
    assert len({(i["subject"]["task_id"], i["subject"]["task_digest"]) for i in items}) == len(
        items
    )
    for item in items:
        status = {rc["status"] for rc in item["root_cause"]}
        confirmed = {rc["category"] for rc in item["root_cause"] if rc["status"] == "confirmed"}
        assert item["selected_by"], item["item_id"]
        assert item["subject"]["task_digest"].startswith("sha256:")
        if item["expected_verdict"] == "fail":
            assert item["label_strength"] == "confirmed"
            assert set(item["expected_categories"]) == confirmed
            assert item["expected_category"] in confirmed
        else:
            # A pass never carries confirmed defect evidence: it is a refuted flag or a fixed variant.
            assert not confirmed and item["expected_category"] is None
            assert item["label_strength"] == (
                "weak_negative" if "fixed" in status else "refuted_claim"
            )


def test_repair_pairs_link_original_to_fixed_variant() -> None:
    items = {i["item_id"]: i for i in _jsonl("items.jsonl")}
    pairs = _jsonl("pairs.jsonl")
    assert pairs
    for pair in pairs:
        parent, child = items[pair["parent"]], items[pair["child"]]
        assert parent["subject"]["task_id"] == child["subject"]["task_id"] == pair["task_id"]
        assert parent["expected_verdict"] == "pass" and child["expected_verdict"] == "fail"
        assert parent["split"] == child["split"] == pair["split"]
        assert pair["pair_id"] in parent["pair_ids"] and pair["pair_id"] in child["pair_ids"]
