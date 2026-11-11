"""Tests for research/experiments/ev1105-queue queue ranking evaluation."""
from __future__ import annotations

import importlib.util
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


