"""Unit tests for A6-STATIC-VS-EXEC analysis pipeline."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_script_path = Path(__file__).parent / "run_static_qa.py"
_spec = importlib.util.spec_from_file_location("run_static_qa", _script_path)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

bootstrap_auroc = _mod.bootstrap_auroc
parse_llm_json = _mod.parse_llm_json
precision_at_k = _mod.precision_at_k
simulate_loo_triage = _mod.simulate_loo_triage

def test_parse_llm_json_clean() -> None:
    raw = json.dumps({
        "weakness_score": 75,
        "rationale": "Missing boundary test",
        "weakest_assertions": [{"assertion": "assert True", "reason": "no-op"}],
    })
    res = parse_llm_json(raw)
    assert res["weakness_score"] == 75
    assert len(res["weakest_assertions"]) == 1


def test_parse_llm_json_fenced() -> None:
    raw = '```json\n{"weakness_score": 42, "rationale": "ok", "weakest_assertions": []}\n```'
    res = parse_llm_json(raw)
    assert res["weakness_score"] == 42
    assert res["rationale"] == "ok"


def test_bootstrap_auroc_separation() -> None:
    pos = [90.0, 85.0, 80.0, 75.0]
    neg = [20.0, 15.0, 10.0, 5.0]
    lo, hi = bootstrap_auroc(pos, neg, n_boot=200, seed=42)
    assert lo == 1.0
    assert hi == 1.0


def test_precision_at_k() -> None:
    items = [
        {"scores": {"s": 90}, "is_weak": True},
        {"scores": {"s": 80}, "is_weak": False},
        {"scores": {"s": 70}, "is_weak": True},
        {"scores": {"s": 60}, "is_weak": False},
    ]
    p1 = precision_at_k(items, "s", 1)
    p2 = precision_at_k(items, "s", 2)
    assert p1 == 1.0
    assert p2 == 0.5


def test_simulate_loo_triage() -> None:
    # 10 items: 6 weak, 4 robust
    items = []
    for i in range(10):
        items.append({
            "task_id": f"task-{i}",
            "scores": {"m": float(i * 10)},
            "is_weak": i >= 4,  # tasks 4..9 are weak (6 tasks), 0..3 are robust (4 tasks)
            "total_cost_usd": 1.0,
        })
    res = simulate_loo_triage(items, "m", target_skip_frac=0.30, max_train_misses=1)
    assert res["n_total"] == 10
    assert 0.0 <= res["skip_rate"] <= 1.0
    assert res["n_missed"] >= 0
