"""Targeted unit tests for D3-DISTILL-PLAN decision rules and arithmetic.

Tests test real statistical properties, boundary conditions, gate transitions,
and contract enforcement without trivial tautologies.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import stats  # noqa: E402
from distill_arithmetic import (  # noqa: E402
    compute_label_inventory,
    compute_recipes_and_economics,
    compute_sample_size_and_power,
    evaluate_decision_rule,
)


def test_mcnemar_exact_boundary() -> None:
    """Verify exact McNemar detects discordance significance boundary at n=6."""
    # Under H0: b and c are Binomial(b+c, 0.5)
    # When c=0, p = 2 * (1/2)^b
    # b=5 -> p = 2 * (1/32) = 0.0625 (> 0.05, not significant)
    assert stats.mcnemar_exact(5, 0) == pytest.approx(0.0625, abs=1e-5)
    # b=6 -> p = 2 * (1/64) = 0.03125 (< 0.05, statistically significant)
    assert stats.mcnemar_exact(6, 0) == pytest.approx(0.03125, abs=1e-5)
    # Equal discordance gives p = 1.0
    assert stats.mcnemar_exact(3, 3) == 1.0


def test_wilson_interval_invariants() -> None:
    """Verify Wilson score intervals adhere to probability boundaries."""
    # 0 successes
    lo0, hi0 = stats.wilson(0, 10)
    assert lo0 == 0.0
    assert 0.0 < hi0 < 0.35

    # 100% successes
    lo1, hi1 = stats.wilson(10, 10)
    assert 0.65 < lo1 < 1.0
    assert hi1 == pytest.approx(1.0, abs=1e-5)

    # Symmetric case
    lo_sym, hi_sym = stats.wilson(50, 100)
    assert lo_sym < 0.5 < hi_sym
    assert abs((0.5 - lo_sym) - (hi_sym - 0.5)) < 1e-4


def test_sample_size_and_power_invariants() -> None:
    """Verify pre-registered power bounds and minimum detectable gap."""
    res = compute_sample_size_and_power()
    # At N=71, minimum detectable gap is 6/71 = 8.45% -> 8.5%
    assert res["min_detectable_gap_percent"] == 8.5
    assert abs(res["min_detectable_gap_at_n71"] - (6 / 71)) < 1e-4
    # Wilson precision at 80% recall requires at least 62 samples
    assert res["n_pos_needed_per_family_wilson_precision_10pct"] == 62
    # McNemar 80% power requires > 100 samples per family
    assert res["n_pos_needed_per_family_mcnemar_80pct_power"] >= 130


def test_label_inventory_provenance_contract() -> None:
    """Verify label inventory respects Judge Station state and D4 provenance rules."""
    inv = compute_label_inventory()
    full = inv["full_benchmark"]

    # Invariant: 0 human labels currently exist (69 pending)
    for fam, d in full.items():
        assert d["human_labels"] == 0, f"Expected 0 human labels in {fam}"

    # Invariant: goodfire and deep_swe are flagged as cot_judged blind
    assert full["goodfire"]["is_cot_judged_blind"] is True
    assert full["deep_swe"]["is_cot_judged_blind"] is True
    assert full["goodfire"]["is_trustworthy_for_training"] is False

    # Invariant: container_verified only in c1c5 and sol_astra
    assert full["c1c5"]["container_verified"] == 16
    assert full["sol_astra"]["container_verified"] == 56
    assert full["cheatbench"]["container_verified"] == 0


def test_economics_serving_costs() -> None:
    """Verify serving economics arithmetic across model options."""
    econ = compute_recipes_and_economics()
    costs = econ["unit_costs_per_1k_usd"]

    # Ling-3.0-Flash must be cheaper than self-hosted Modal LoRA 7B
    assert costs["paid_ling_3_0_flash"] < costs["recipe_a_lora_7b_modal"]
    # Recipe B (CPU encoder) must have ultra-low serving cost
    assert costs["recipe_b_encoder_modal_cpu"] < 0.05
    # Gemini 3.1 Flash-Lite must have 0.0 marginal subscription cost
    assert costs["best_cheap_gemini_3_1_flash_lite_subscription"] == 0.0


def test_gate_evaluation_trips() -> None:
    """Verify decision rule correctly trips all gates when empirical criteria fail."""
    # Synthetic inventory with deficient sample sizes
    mock_inv = {
        "full_benchmark": {
            fam: {"pos": 5, "neg": 10} for fam in ["c1c5", "sol_astra", "cheatbench", "terminal_wrench", "goodfire", "deep_swe"]
        }
    }
    mock_ladder = {}
    mock_power = {"n_pos_needed_per_family_wilson_precision_10pct": 62}
    mock_econ = {}

    decision = evaluate_decision_rule(mock_inv, mock_ladder, mock_power, mock_econ)
    assert decision["verdict"] == "DO NOT DISTILL (DO X FIRST)"
    assert decision["gates"]["gate1_headroom"]["passed"] is False
    assert decision["gates"]["gate2_data_sufficiency"]["passed"] is False
    assert decision["gates"]["gate3_economic_breakeven"]["passed"] is False


def test_results_json_integrity() -> None:
    """Verify saved results.json exists and contains complete valid fields."""
    res_path = Path("research/experiments/on-distill-plan-20261102/results.json")
    assert res_path.exists(), "results.json must exist"
    data = json.loads(res_path.read_text(encoding="utf-8"))

    assert "label_inventory" in data
    assert "per_family_ladder" in data
    assert "sample_size_and_power" in data
    assert "recipes_and_economics" in data
    assert "decision_rule_evaluation" in data
    assert data["decision_rule_evaluation"]["verdict"] == "DO NOT DISTILL (DO X FIRST)"
