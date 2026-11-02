"""Synthetic tests for equivalence analysis pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CERT_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert")
sys.path.insert(0, str(CERT_DIR))
import stats  # noqa: E402
from analyze_results import cohen_kappa  # noqa: E402
from run_judges import extract_json  # noqa: E402


def test_extract_json_valid() -> None:
    raw = '{"verdict": "behavior_changing", "reason": "Alters upper bound check."}'
    parsed = extract_json(raw)
    assert parsed is not None
    assert parsed["verdict"] == "behavior_changing"


def test_extract_json_markdown() -> None:
    raw = '```json\n{"verdict": "equivalent", "reason": "Dead code elimination."}\n```'
    parsed = extract_json(raw)
    assert parsed is not None
    assert parsed["verdict"] == "equivalent"


def test_cohen_kappa_perfect() -> None:
    table = [
        [50, 0, 0],
        [0, 20, 0],
        [0, 0, 4],
    ]
    kappa = cohen_kappa(table)
    assert kappa == 1.0


def test_stats_wilson() -> None:
    lo, hi = stats.wilson(51, 51)
    assert 0.90 < lo <= 1.0
    assert abs(hi - 1.0) < 1e-6


def test_definition_logic() -> None:
    survivors_j1 = {"task1": ["behavior_changing"], "task2": ["behavior_changing"]}
    survivors_j2 = {"task1": ["behavior_changing"], "task2": ["equivalent"]}

    weak_A = ["task1", "task2"]
    weak_B = [t for t in weak_A if any(v1 == "behavior_changing" and v2 == "behavior_changing" for v1, v2 in zip(survivors_j1[t], survivors_j2[t], strict=True))]
    weak_C = [t for t in weak_A if any(v1 == "behavior_changing" or v2 == "behavior_changing" for v1, v2 in zip(survivors_j1[t], survivors_j2[t], strict=True))]

    assert weak_B == ["task1"]
    assert weak_C == ["task1", "task2"]


def test_rogan_gladen_dual_correction() -> None:
    p_star = 1.0
    se = 1.0
    sp = 0.9545
    # Under point estimates
    p_hat = (p_star + sp - 1.0) / (se + sp - 1.0)
    assert abs(p_hat - 1.0) < 1e-6

    # Under conservative lower bounds
    p_star_lo = 0.8513
    se_hi = 1.0
    sp_lo = 0.8491
    p_hat_lo = (p_star_lo + sp_lo - 1.0) / (se_hi + sp_lo - 1.0)
    assert 0.80 < p_hat_lo < 0.85


def test_specificity_wilson_gate() -> None:
    k, n = 42, 44
    lo, hi = stats.wilson(k, n)
    rate = k / n
    assert rate >= 0.80
    assert lo >= 0.60
