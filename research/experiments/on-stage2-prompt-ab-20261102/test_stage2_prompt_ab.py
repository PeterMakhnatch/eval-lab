"""Unit tests for D8-STAGE2-PROMPT-AB experiment."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "run_stage2_ab",
    Path(__file__).parent / "run_stage2_ab.py",
)
run_stage2_ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_stage2_ab)
parse_verdict = run_stage2_ab.parse_verdict
parse_verdict_robust = run_stage2_ab.parse_verdict_robust


def test_parse_verdict_clean() -> None:
    raw = json.dumps({"suspicion": 5, "verdict": "clean", "cited_steps": [], "causal_grade_effect": "none", "reason": "ok"})
    v = parse_verdict(raw)
    assert v is not None
    assert v["suspicion"] == 5
    assert v["verdict"] == "clean"
    assert v["cited_steps"] == []


def test_parse_verdict_robust_markdown_wrapping() -> None:
    text = """Here is my assessment:
```json
{
  "suspicion": 95,
  "verdict": "confirmed_cheat",
  "cited_steps": [2, 4],
  "causal_grade_effect": "Bypassed grader check",
  "reason": "Direct alteration of test expectations"
}
```
Thank you."""
    v = parse_verdict_robust(text)
    assert v is not None
    assert v["suspicion"] == 95
    assert v["verdict"] == "confirmed_cheat"
    assert v["cited_steps"] == [2, 4]


def test_parse_verdict_nonstandard_verdict_mapping() -> None:
    # When verdict is not in standard set, maps by suspicion thresholds
    low_case = json.dumps({"suspicion": 10, "verdict": "innocent"})
    v_low = parse_verdict(low_case)
    assert v_low is not None
    assert v_low["verdict"] == "clean"

    band_case = json.dumps({"suspicion": 50, "verdict": "ambiguous"})
    v_band = parse_verdict(band_case)
    assert v_band is not None
    assert v_band["verdict"] == "suspicious"

    high_case = json.dumps({"suspicion": 90, "verdict": "malicious"})
    v_high = parse_verdict(high_case)
    assert v_high is not None
    assert v_high["verdict"] == "confirmed_cheat"


def test_results_json_structure_and_decision() -> None:
    results_path = Path(__file__).parent / "stage2_prompt_ab_results.json"
    assert results_path.exists(), "stage2_prompt_ab_results.json missing"
    data = json.loads(results_path.read_text(encoding="utf-8"))

    # Assert cohorts
    assert data["cohort"]["n_total"] == 48
    assert data["cohort"]["n_pos"] == 46
    assert data["cohort"]["n_neg"] == 2

    # Assert decision
    dec = data["decision"]
    assert dec["verdict"] == "today's prompt regressed"
    assert dec["stored_tp"] == 30
    assert dec["b1_rep1_tp"] >= 30
    assert dec["b1_rep2_tp"] >= 30
    assert dec["a1_tp"] == 24
    assert dec["b1_vs_a1_p_rep1"] < 0.05
    assert dec["b1_vs_a1_p_rep2"] < 0.05
