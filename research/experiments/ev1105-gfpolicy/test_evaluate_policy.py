"""Tests for ev1105-gfpolicy goodfire-action-v1 label view and DEV policy tables."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evallab import hack_drop as hd

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

from evaluate_policy import (  # noqa: E402
    ORIG_GOODFIRE_LABELS_PATH,
    ORIG_GOODFIRE_LABELS_SHA256,
    sha256_file,
    wilson_ci,
)


def test_goodfire_action_v1_label_view_integrity() -> None:
    view_path = HERE / "goodfire-action-v1.jsonl"
    assert view_path.is_file()
    rows = [
        json.loads(line)
        for line in view_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 150

    # Check schema and required keys
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids)
    assert len(set(ids)) == 150
    for r in rows:
        assert r["schema"] == "evallab.goodfire_action_label/v1"
        assert r["view"] == "goodfire-action-v1"
        assert r["split"] == "dev"
        assert r["run_key"] == f"goodfire:{r['id']}"

    orig_pos = [r for r in rows if r["original_is_cheat"] is True]
    orig_neg = [r for r in rows if r["original_is_cheat"] is False]
    assert len(orig_pos) == 80
    assert len(orig_neg) == 70

    # Action-based labels on the 80 publisher positives:
    # 11 executed-shortcut (positive), 23 intent-only, 46 no-evidence
    exec_pos = [r for r in orig_pos if r["action_label"] == "executed-shortcut"]
    intent_pos = [r for r in orig_pos if r["action_label"] == "intent-only"]
    no_ev_pos = [r for r in orig_pos if r["action_label"] == "no-evidence"]
    assert len(exec_pos) == 11
    assert len(intent_pos) == 23
    assert len(no_ev_pos) == 46

    # Sensitivity tiers on the 80 publisher positives
    assert sum(1 for r in orig_pos if r["is_executed_shortcut_strict_tier1"]) == 6
    assert sum(1 for r in orig_pos if r["is_executed_shortcut_unanimous_3reader"]) == 10
    assert sum(1 for r in orig_pos if r["is_executed_shortcut_primary"]) == 11
    assert sum(1 for r in orig_pos if r["is_executed_shortcut_broader_union"]) == 14

    # Full <think>+content synthesis on the 80 publisher positives: 11 / 52 / 17
    assert sum(1 for r in orig_pos if r["action_label_full_synthesis"] == "executed-shortcut") == 11
    assert sum(1 for r in orig_pos if r["action_label_full_synthesis"] == "intent-only") == 52
    assert sum(1 for r in orig_pos if r["action_label_full_synthesis"] == "no-evidence") == 17

    # Never silently relabel intent-only or no-evidence publisher positives as honest
    for r in intent_pos:
        assert r["is_cheat"] is None
        assert r["label"] == "considered_only"
    for r in no_ev_pos:
        assert r["is_cheat"] is None
        assert r["label"] == "unjudged"

    # All 70 publisher negatives have 0 executed shortcuts and remain honest
    for r in orig_neg:
        assert r["action_label"] == "honest"
        assert r["is_cheat"] is False
        assert r["label"] == "honest"
        assert r["is_executed_shortcut_primary"] is False
        assert r["is_executed_shortcut_strict_tier1"] is False
    assert sum(1 for r in orig_neg if r["honest_action_subclassification"] == "no-evidence") == 69
    assert sum(1 for r in orig_neg if r["honest_action_subclassification"] == "intent-only") == 1


def test_hack_drop_load_labels_compatibility() -> None:
    view_path = HERE / "goodfire-action-v1.jsonl"
    attempted: set[str] = set()
    loaded = hd.load_labels(
        view_path, repo_root=REPO_ROOT, attempted_cheat="exclude", attempted_ids=attempted
    )
    assert len(loaded) == 81
    assert sum(1 for v in loaded.values() if v is True) == 11
    assert sum(1 for v in loaded.values() if v is False) == 70
    assert len(attempted) == 23

    # Original label file must remain untouched
    if ORIG_GOODFIRE_LABELS_PATH.exists():
        assert sha256_file(ORIG_GOODFIRE_LABELS_PATH) == ORIG_GOODFIRE_LABELS_SHA256


def test_dev_tables_and_policy_counts() -> None:
    tables_path = HERE / "results" / "tables.json"
    assert tables_path.is_file()
    report = json.loads(tables_path.read_text(encoding="utf-8"))
    policies = report["policies"]

    # 1. Current main (floor on, J3 off)
    m_main = policies["main_floor_on_j3_off"]
    assert m_main["original_labels"]["pooled"]["cheat"]["keep"] == 9
    assert m_main["original_labels"]["pooled"]["cheat"]["review"] == 111
    assert m_main["original_labels"]["pooled"]["cheat"]["drop"] == 212
    assert m_main["original_labels"]["pooled"]["honest"]["keep"] == 63
    assert m_main["original_labels"]["pooled"]["honest"]["review"] == 255
    assert m_main["original_labels"]["pooled"]["honest"]["drop"] == 40
    assert m_main["original_labels"]["pooled"]["total_review_load"] == 366
    assert m_main["original_labels"]["pooled"]["judge_queue_load"] == 369
    assert (
        m_main["goodfire_action_v1"]["pooled_full_line_690"]["executed_shortcut_primary"]["keep"]
        == 9
    )
    assert (
        m_main["goodfire_action_v1"]["goodfire_all_150"]["executed_shortcut_primary"]["keep"] == 0
    )

    # 2. Floor off + J3 review>=61/drop>=100 (#1908)
    m_61 = policies["floor_off_j3_61_100"]
    assert m_61["original_labels"]["pooled"]["cheat"]["keep"] == 58
    assert m_61["original_labels"]["pooled"]["cheat"]["review"] == 60
    assert m_61["original_labels"]["pooled"]["cheat"]["drop"] == 214
    assert m_61["original_labels"]["pooled"]["honest"]["keep"] == 112
    assert m_61["original_labels"]["pooled"]["honest"]["review"] == 206
    assert m_61["original_labels"]["pooled"]["honest"]["drop"] == 40
    assert m_61["original_labels"]["pooled"]["total_review_load"] == 266
    assert m_61["original_labels"]["pooled"]["judge_queue_load"] == 271

    gf_61 = m_61["goodfire_action_v1"]["goodfire_all_150"]
    assert gf_61["original_cheat"]["keep"] == 52
    assert gf_61["executed_shortcut_primary"]["keep"] == 1
    assert gf_61["executed_shortcut_primary"]["review"] == 5
    assert gf_61["executed_shortcut_primary"]["drop"] == 5
    assert gf_61["executed_shortcut_strict_tier1"]["keep"] == 0
    assert gf_61["executed_shortcut_strict_tier1"]["review"] == 3
    assert gf_61["executed_shortcut_strict_tier1"]["drop"] == 3
    assert gf_61["executed_shortcut_unanimous_3reader"]["keep"] == 0
    assert gf_61["intent_only_step_digest"]["keep"] == 17
    assert gf_61["no_evidence_step_digest"]["keep"] == 34
    assert gf_61["honest"]["keep"] == 60
    assert gf_61["honest"]["review"] == 3
    assert gf_61["honest"]["drop"] == 7

    pa_61 = m_61["goodfire_action_v1"]["pooled_full_line_690"]
    assert pa_61["executed_shortcut_primary"]["keep"] == 7
    assert pa_61["executed_shortcut_primary"]["review"] == 55
    assert pa_61["executed_shortcut_primary"]["drop"] == 201
    assert pa_61["executed_shortcut_strict_tier1"]["keep"] == 6

    # 3. Floor off + J3 frozen 89
    m_89 = policies["floor_off_j3_frozen89"]
    assert m_89["original_labels"]["pooled"]["cheat"]["keep"] == 63
    assert m_89["original_labels"]["pooled"]["cheat"]["review"] == 49
    assert m_89["original_labels"]["pooled"]["cheat"]["drop"] == 220
    assert m_89["original_labels"]["pooled"]["honest"]["keep"] == 113
    assert m_89["original_labels"]["pooled"]["honest"]["review"] == 192
    assert m_89["original_labels"]["pooled"]["honest"]["drop"] == 53
    assert m_89["original_labels"]["pooled"]["total_review_load"] == 241
    assert m_89["original_labels"]["pooled"]["judge_queue_load"] == 247

    gf_89 = m_89["goodfire_action_v1"]["goodfire_all_150"]
    assert gf_89["original_cheat"]["keep"] == 55
    assert gf_89["executed_shortcut_primary"]["keep"] == 2
    assert gf_89["executed_shortcut_primary"]["review"] == 1
    assert gf_89["executed_shortcut_primary"]["drop"] == 8
    assert gf_89["executed_shortcut_strict_tier1"]["keep"] == 0
    assert gf_89["executed_shortcut_strict_tier1"]["drop"] == 6
    assert gf_89["intent_only_step_digest"]["keep"] == 17
    assert gf_89["no_evidence_step_digest"]["keep"] == 36
    assert gf_89["honest"]["keep"] == 60

    pa_89 = m_89["goodfire_action_v1"]["pooled_full_line_690"]
    assert pa_89["executed_shortcut_primary"]["keep"] == 10
    assert pa_89["executed_shortcut_strict_tier1"]["keep"] == 8

    # 4. Sub-benchmark & Goodfire-scoped policy options
    m_swe_only = policies["swe_floor_off_ib_floor_on_j3_off"]
    assert (
        m_swe_only["goodfire_action_v1"]["goodfire_all_150"]["executed_shortcut_primary"]["keep"]
        == 0
    )
    assert (
        m_swe_only["goodfire_action_v1"]["pooled_full_line_690"]["executed_shortcut_primary"][
            "keep"
        ]
        == 9
    )
    assert m_swe_only["original_labels"]["pooled"]["honest"]["keep"] == 88
    assert m_swe_only["original_labels"]["pooled"]["total_review_load"] == 314

    m_gf_scoped = policies["gf_scoped_j3_61_100_floor_off"]
    assert (
        m_gf_scoped["goodfire_action_v1"]["pooled_full_line_690"]["executed_shortcut_primary"][
            "keep"
        ]
        == 10
    )
    assert (
        m_gf_scoped["goodfire_action_v1"]["pooled_full_line_690"]["executed_shortcut_strict_tier1"][
            "keep"
        ]
        == 9
    )
    assert m_gf_scoped["original_labels"]["pooled"]["honest"]["keep"] == 123
    assert m_gf_scoped["original_labels"]["pooled"]["total_review_load"] == 253


def test_wilson_ci_helper() -> None:
    lo, hi = wilson_ci(1, 11)
    assert round(lo, 4) == 0.0162
    assert round(hi, 4) == 0.3774
