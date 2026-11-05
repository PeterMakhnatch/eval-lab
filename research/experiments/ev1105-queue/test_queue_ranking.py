"""Tests for research/experiments/ev1105-queue queue ranking evaluation."""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ev1105_queue_evaluate", HERE / "evaluate.py")
assert _spec is not None and _spec.loader is not None
evaluate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evaluate)


def test_single_pass_metrics_and_ties() -> None:
    # 10 items, 4 cheats at indices 0, 2, 5, 8
    y = [1, 0, 1, 0, 0, 1, 0, 0, 1, 0]
    c_idx = [0, 1, 2, 3, 4, 0, 1, 2, 3, 4]
    weights = [1, 1, 1, 1, 1]
    # Perfect ranking puts indices 0, 2, 5, 8 first
    scores = [100.0, 10.0, 90.0, 10.0, 10.0, 80.0, 10.0, 10.0, 70.0, 10.0]
    order = sorted(range(10), key=lambda i: (-scores[i], i))
    res = evaluate.single_pass_metrics_from_weights(order, y, c_idx, weights)
    assert res is not None
    assert res["n"] == 10 and res["p"] == 4
    # Top 10% (1 item) -> 1 cheat; Top 20% (2 items) -> 2 cheats; Top 50% (5 items) -> 4 cheats
    assert res["top_k_cheats"][10] == 1
    assert res["top_k_cheats"][20] == 2
    assert res["top_k_cheats"][50] == 4
    # Reviews to find 50% (2 cheats) -> 2; 80% (ceil(3.2)=4 cheats) -> 4; 90% (4 cheats) -> 4
    assert res["rev_needed"][50] == 2
    assert res["rev_needed"][80] == 4
    assert res["rev_needed"][90] == 4


def test_tie_expected_handles_tied_groups() -> None:
    # All 10 items tied at score 0.0, 4 cheats out of 10 -> rate = 0.4 per review
    items = [(i % 5, 1 if i < 4 else 0) for i in range(10)]
    score_blocks = [(0.0, items)]
    res = evaluate.tie_expected_from_weights(score_blocks, [1, 1, 1, 1, 1])
    assert res is not None
    assert math.isclose(res["top_k_cheats"][10], 0.4)
    assert math.isclose(res["top_k_cheats"][50], 2.0)
    # Target 50% = 2 cheats -> 2 / 0.4 = 5.0 reviews
    assert math.isclose(res["rev_needed"][50], 5.0)
    # Target 80% = ceil(3.2) = 4 cheats -> 4 / 0.4 = 10.0 reviews
    assert math.isclose(res["rev_needed"][80], 10.0)


def test_committed_prereg_and_aggregates_contract() -> None:
    prereg = json.loads((HERE / "prereg.json").read_text())
    agg = json.loads((HERE / "results/aggregates.json").read_text())

    assert prereg["schema"] == "ev1105.queue.prereg/v1"
    assert agg["schema"] == "ev1105.queue.aggregates/v1"
    assert agg["model_calls"] == 0
    assert agg["paid_spend_usd"] == 0.0
    assert agg["prereg_md_sha256"] == evaluate.sha256_file(HERE / "PREREG.md")
    assert agg["prereg_json_sha256"] == evaluate.sha256_file(HERE / "prereg.json")

    expected_queues = {
        "dev_review": (366, 111, 255),
        "dev_keep_audit": (72, 9, 63),
        "heldout_pooled": (1450, 344, 1106),
        "malt": (400, 15, 385),
        "impossible_task": (80, 53, 27),
    }
    for qid, (n, p, h) in expected_queues.items():
        q = agg["queues"][qid]["full_queue"]
        assert q["n"] == n
        assert q["cheats"] == p
        assert q["honest"] == h
        for sig in ("j3", "j2b", "length_chars", "length_steps", "current_order", "random_expected"):
            assert sig in q["signals"]
            sdata = q["signals"][sig]
            for k in ("5", "10", "20", "30", "50"):
                assert k in sdata["top_k"]
            for r in ("50", "80", "90"):
                assert r in sdata["reviews_to_find_cheats"]

    # Check worked examples
    assert len(agg["worked_examples"]) == 2
    ex1, ex2 = agg["worked_examples"]
    assert ex1["queue"] == "dev_keep_audit"
    assert ex1["caught_by_j3_drop_threshold"] is False
    assert ex1["reviews_saved_on_run_by_j3"]["vs_current_order"] == 61
    assert ex1["reviews_saved_on_run_by_j3"]["vs_length_chars"] == 65
    assert ex2["queue"] == "impossible_task"
    assert ex2["caught_by_j3_drop_threshold"] is False
    assert ex2["reviews_saved_on_run_by_j3"]["vs_current_order"] == 37
    assert ex2["reviews_saved_on_run_by_j3"]["vs_length_chars"] == 36
