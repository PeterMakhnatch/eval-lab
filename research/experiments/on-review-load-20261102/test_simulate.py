"""Unit tests for review load simulation and throttling policies."""

import hashlib
import sys
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent
if str(EXPERIMENT_DIR) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_DIR))

import stats  # noqa: E402
from simulate import (  # noqa: E402
    evaluate_selection,
    run_policy_family_caps,
    run_policy_top_k,
    run_policy_uncertainty_control,
)


def _make_toy_cases(n: int = 50) -> list[dict]:
    cases = []
    for i in range(n):
        cid = f"case-{i:03d}"
        lbl = (i % 2) == 0  # 50% positive
        prio = 2 if i < 15 else (1 if i < 35 else 0)
        fam = "fam_a" if i < 25 else "fam_b"
        susp = float(90 - i)
        cases.append({
            "case_id": cid,
            "family": fam,
            "label": lbl,
            "priority": prio,
            "ar_susp": susp,
            "uncertainty": abs(susp - 50.0),
            "hash": hashlib.sha256(f"toy:{cid}".encode()).hexdigest(),
        })
    return cases


def test_priority_ordering_invariant():
    cases = _make_toy_cases(50)
    selected = run_policy_top_k(cases, k=20)
    assert len(selected) == 20
    priorities = [c["priority"] for c in selected]
    # Check descending priority order
    for a, b in zip(priorities, priorities[1:], strict=False):
        assert a >= b


def test_selection_budget_bounds():
    cases = _make_toy_cases(30)
    for k in (5, 15, 30, 50):
        sel = run_policy_top_k(cases, k)
        assert len(sel) == min(k, len(cases))


def test_idempotency_and_deterministic_order():
    cases = _make_toy_cases(40)
    run1 = [c["case_id"] for c in run_policy_top_k(cases, k=15)]
    run2 = [c["case_id"] for c in run_policy_top_k(cases, k=15)]
    assert run1 == run2


def test_family_caps_allocation():
    cases = _make_toy_cases(50)  # 25 fam_a, 25 fam_b
    k = 10
    sel = run_policy_family_caps(cases, k=k)
    assert len(sel) == k
    by_fam = [c["family"] for c in sel]
    assert by_fam.count("fam_a") == 5
    assert by_fam.count("fam_b") == 5


def test_uncertainty_control_split():
    cases = _make_toy_cases(50)
    k = 20
    sel = run_policy_uncertainty_control(cases, k=k, seed="test_seed")
    assert len(sel) == k
    # 80% should be 16, 20% should be 4
    # All 16 unc cases must have priority >= 1
    prio_cases = sel[:16]
    for c in prio_cases:
        assert c["priority"] >= 1


def test_evaluate_selection_metrics():
    cases = _make_toy_cases(20)
    positives_total = sum(1 for c in cases if c["label"])
    selected = cases[:10]
    res = evaluate_selection(selected, positives_total, k=10)

    assert res["cards_budget"] == 10
    assert res["cards_selected"] == 10
    assert 0.0 <= res["precision"] <= 1.0
    assert 0.0 <= res["recall"] <= 1.0
    assert res["precision_wilson_95"][0] <= res["precision"] <= res["precision_wilson_95"][1]

    # Rates match precision scaling: Rate = Precision * (60 / tau)
    assert res["positives_per_judge_hour"]["at_2min_per_card"] == round(res["precision"] * 30.0, 2)
    assert res["positives_per_judge_hour"]["at_2_5min_per_card"] == round(res["precision"] * 24.0, 2)
    assert res["positives_per_judge_hour"]["at_3min_per_card"] == round(res["precision"] * 20.0, 2)


def test_mcnemar_paired_significance():
    a = [True, True, False, False, True]
    b = [True, False, False, False, True]
    sig = stats.paired(a, b)
    assert sig["b"] == 1
    assert sig["c"] == 0
    assert sig["both"] == 2
    assert sig["neither"] == 2
    assert sig["n"] == 5
    assert 0.0 <= sig["p"] <= 1.0
