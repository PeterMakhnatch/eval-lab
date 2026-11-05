"""Unit tests for ev1105-goodfire audit script, route guard, and generated results."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_goodfire import (
    CURATED_EXEC_INFO,
    agreement_summary,
    cohens_kappa,
    metric_with_ci,
    wilson_ci,
)
from gateway import parse_first_json_object, verify_route_allowed

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def test_wilson_ci_bounds_and_endpoints() -> None:
    lo0, hi0 = wilson_ci(0, 70)
    assert lo0 == 0.0
    assert 0.05 < hi0 < 0.055

    lo80, hi80 = wilson_ci(80, 80)
    assert 0.95 < lo80 < 0.96
    assert hi80 == 1.0

    lo46, hi46 = wilson_ci(46, 80)
    assert round(lo46, 4) == 0.4657
    assert round(hi46, 4) == 0.6774


def test_cohens_kappa_and_agreement() -> None:
    y1 = ["executed-shortcut"] * 11 + ["no-evidence"] * 69
    y2 = ["executed-shortcut"] * 11 + ["no-evidence"] * 69
    summary = agreement_summary(y1, y2)
    assert summary["k"] == 80
    assert summary["n"] == 80
    assert summary["rate"] == 1.0
    assert summary["cohens_kappa"] == 1.0
    assert cohens_kappa(["a", "b", "a"], ["b", "a", "b"]) < 0.0


def test_route_guard_enforces_google_antigravity() -> None:
    verify_route_allowed("google-antigravity/gemini-3.8-flash")
    verify_route_allowed("google-antigravity/gemini-4-argon")
    with pytest.raises(ValueError, match="forbidden"):
        verify_route_allowed("openai-codex/gpt-6.1-terra")
    with pytest.raises(ValueError, match="forbidden"):
        verify_route_allowed("anthropic/claude-fable-5-1")


def test_parse_first_json_object_handles_fences() -> None:
    raw = (
        'Here is the result:\n```json\n{"classification": "no-evidence", "shortcut_steps": []}\n```'
    )
    parsed = parse_first_json_object(raw)
    assert parsed is not None
    assert parsed["classification"] == "no-evidence"


def test_generated_artifacts_integrity() -> None:
    agg_path = RESULTS_DIR / "aggregates.json"
    sheet_path = RESULTS_DIR / "label_sheet.jsonl"
    att_path = RESULTS_DIR / "attestation.json"
    assert agg_path.exists(), "aggregates.json must exist"
    assert sheet_path.exists(), "label_sheet.jsonl must exist"
    assert att_path.exists(), "attestation.json must exist"

    agg = json.loads(agg_path.read_text(encoding="utf-8"))
    assert agg["n_total"] == 150
    assert agg["n_positives"] == 80
    assert agg["n_honest"] == 70

    # Verify prior 46/80 passage-channel audit re-derivation
    pub = agg["publisher_passage_channel_audit"]
    assert pub["positives_with_zero_flagged_tool_call_passages"]["k"] == 46
    assert pub["positives_with_any_flagged_tool_call_passage"]["k"] == 34
    assert pub["honest_with_any_flagged_passage"]["k"] == 0

    # Verify primary corrected counts on 80 positives
    pos_cts = agg["positives_corrected_counts"]["primary_adjudicated_3class"]
    assert pos_cts["executed-shortcut"]["k"] == 11
    assert pos_cts["intent-or-narration-only"]["k"] == 23
    assert pos_cts["no-evidence"]["k"] == 46
    assert (
        pos_cts["executed-shortcut"]["k"]
        + pos_cts["intent-or-narration-only"]["k"]
        + pos_cts["no-evidence"]["k"]
        == 80
    )

    # Verify honest corrected counts on 70 honest runs
    neg_cts = agg["honest_corrected_counts"]["primary_adjudicated_3class"]
    assert neg_cts["executed-shortcut"]["k"] == 0
    assert (
        neg_cts["executed-shortcut"]["k"]
        + neg_cts["intent-or-narration-only"]["k"]
        + neg_cts["no-evidence"]["k"]
        == 70
    )

    # Verify R1 vs R2 binary agreement on 80 positives is 80/80 (100%)
    agree_pos = agg["inter_rater_agreement_r1_vs_r2"]["positives_all_80"]
    assert agree_pos["binary_executed_shortcut"]["k"] == 80
    assert agree_pos["binary_executed_shortcut"]["cohens_kappa"] == 1.0
    assert agree_pos["three_class"]["k"] == 73

    # Verify floor impact on primary n=11 executed shortcuts
    fl_11 = agg["goodfire_floor_impact"]["primary_executed_shortcuts_n11"]["policies"]
    assert fl_11["line_j3_floor_removed"]["drop"] == 8
    assert fl_11["line_j3_floor_removed"]["review"] == 1
    assert fl_11["line_j3_floor_removed"]["keep"] == 2
    assert fl_11["line_main_floor_removed"]["keep"] == 6

    # Verify label sheet rows and quotes
    rows = [
        json.loads(line)
        for line in sheet_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 150
    exec_rows = [r for r in rows if r["final_classification"] == "executed-shortcut"]
    assert len(exec_rows) == 11
    for r in exec_rows:
        assert r["run_key"] in CURATED_EXEC_INFO
        assert len(r["shortcut_steps"]) >= 1
        assert len(r["short_quote"]) >= 10

    # Verify attestation has 0 fallbacks and only google-antigravity/* routes
    att = json.loads(att_path.read_text(encoding="utf-8"))
    assert att["rejected_or_fallback"] == 0
    assert att["attested_ok"] == att["total_calls"] == 329
    for route in att["by_route"]:
        assert route.startswith("google-antigravity/")
    assert metric_with_ci(11, 80)["rate"] == 0.1375
