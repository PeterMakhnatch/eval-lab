"""Unit tests for D7-STAGE2-EVERYWHERE trigger-policy experiment."""
from __future__ import annotations

import json
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
RESULTS_FILE = EXP_DIR / "stage2_everywhere_results.json"
RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/D7-STAGE2-EVERYWHERE/receipts")


def test_results_file_exists_and_valid() -> None:
    assert RESULTS_FILE.exists(), f"Results file missing: {RESULTS_FILE}"
    data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    assert "policy_evaluation" in data
    assert "pre_registered_decision_rules" in data
    assert "noise_floor_48_cases" in data

    policies = data["policy_evaluation"]
    for p in ("P0", "P1", "P2", "P3", "P4"):
        assert p in policies
        p_info = policies[p]
        assert "auroc" in p_info
        assert "recall" in p_info
        assert "false_alarms" in p_info
        assert "stage2_calls" in p_info
        assert "economics" in p_info


def test_pre_registered_decision_rules() -> None:
    data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    decisions = data["pre_registered_decision_rules"]
    assert decisions["verdict"] == "RETAIN PRODUCTION TRIGGER"

    r1 = decisions["rule_1_stage2_everywhere"]
    # P1 recall is 26.9% vs P0 recall 29.8% -> recall_criterion_met is False
    assert not r1["recall_criterion_met"]
    assert not r1["significance_criterion_met"]
    assert r1["cost_criterion_met"]
    assert not r1["overall_met"]

    r2 = decisions["rule_2_l0_only"]
    # P2 recall is 31.3% vs P0 recall 29.8% -> recall_criterion_80pct_met is True
    assert r2["recall_criterion_80pct_met"]
    # P2 calls are 16 vs P0 20 (80% >= 60%) -> call_criterion_60pct_met is False
    assert not r2["call_criterion_60pct_met"]
    assert not r2["overall_met"]


def test_receipts_attestation() -> None:
    p1_file = RECEIPTS_DIR / "all-272-p1-stage2-low.jsonl"
    assert p1_file.exists()
    recs = [json.loads(line) for line in p1_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(recs) == 272

    for r in recs:
        assert r["model"] == "google-antigravity/gemini-3.8-flash"
        assert r["thinking"] == "low"
        assert r["n_fallbacks"] == 0
        assert r["parse_ok"]
        assert "gemini-4-argon" not in str(r.get("served_models", []))


def test_noise_floor_metrics() -> None:
    data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    nf = data["noise_floor_48_cases"]
    assert nf["n_cases"] == 48
    assert nf["suspicion_mae"] <= 10.0
    assert nf["verdict_agreement_rate"] >= 0.90
